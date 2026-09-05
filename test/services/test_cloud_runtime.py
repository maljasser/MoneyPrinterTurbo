"""Regression tests for private cloud configuration and readiness."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import toml

from app.config import config
from deploy import run


def auth_env(tmp_path):
    return {
        "MPT_AUTH_USER": "owner",
        # Syntactically valid test fixture; no account uses this value.
        "MPT_AUTH_PASSWORD_HASH": "$2a$14$" + "a" * 53,
        "MPT_DATA_DIR": str(tmp_path),
    }


@pytest.mark.parametrize("missing", ["MPT_AUTH_USER", "MPT_AUTH_PASSWORD_HASH"])
def test_runtime_refuses_missing_login(tmp_path, missing):
    env = auth_env(tmp_path)
    del env[missing]
    with pytest.raises(ValueError):
        run.validate_environment(env)


@pytest.mark.parametrize("port", ["80", "8081", "8090", "8501", "65536"])
def test_runtime_refuses_internal_or_invalid_port(tmp_path, port):
    with pytest.raises(ValueError):
        run.validate_environment({**auth_env(tmp_path), "PORT": port})


def test_runtime_refuses_caddy_configuration_injection(tmp_path):
    with pytest.raises(ValueError):
        run.validate_environment({**auth_env(tmp_path), "MPT_AUTH_USER": "owner\n}"})


def test_railway_requires_the_persistent_volume(tmp_path):
    env = {**auth_env(tmp_path), "RAILWAY_ENVIRONMENT_ID": "test"}
    with pytest.raises(ValueError, match="volume"):
        run.validate_environment(env)
    assert run.validate_environment({**env, "RAILWAY_VOLUME_MOUNT_PATH": str(tmp_path)}) == tmp_path


def test_config_directory_is_preserved(tmp_path):
    path = tmp_path / "config.toml"
    path.mkdir()
    sentinel = path / "do-not-delete"
    sentinel.write_text("preserve")
    with patch.object(config, "config_file", str(path)):
        with pytest.raises(IsADirectoryError):
            config.load_config()
    assert sentinel.read_text() == "preserve"


def test_atomic_save_stays_on_the_configuration_volume(tmp_path):
    original_cfg = dict(config._cfg)
    config_path = tmp_path / "volume" / "config.toml"
    config_path.parent.mkdir()
    try:
        with (
            patch.object(config, "config_file", str(config_path)),
            patch.object(config.tempfile, "mkstemp", wraps=config.tempfile.mkstemp) as create,
        ):
            config.save_config()
        assert Path(create.call_args.kwargs["dir"]) == config_path.parent
        assert "app" in toml.load(config_path)
    finally:
        config._cfg.clear()
        config._cfg.update(original_cfg)


def test_restart_preserves_saved_provider_and_voice_settings(tmp_path):
    # Separate processes reproduce real restarts and config re-imports.
    code = "from pathlib import Path; from deploy.run import prepare_config; prepare_config(Path(__import__('os').environ['MPT_DATA_DIR']))"
    env = {**os.environ, "MPT_DATA_DIR": str(tmp_path)}
    subprocess.run([sys.executable, "-c", code], env=env, cwd=run.ROOT, check=True, capture_output=True)
    path = tmp_path / "config.toml"
    saved = toml.load(path)
    saved["app"]["llm_provider"] = "gemini"
    saved["ui"]["voice_name"] = "ar-SA-ZariyahNeural-Female"
    path.write_text(toml.dumps(saved), encoding="utf-8")
    subprocess.run([sys.executable, "-c", code], env=env, cwd=run.ROOT, check=True, capture_output=True)
    restored = toml.load(path)
    assert restored["app"]["llm_provider"] == "gemini"
    assert restored["ui"]["voice_name"] == "ar-SA-ZariyahNeural-Female"
    assert path.stat().st_mode & 0o777 == 0o600


def test_readiness_requires_both_services():
    response = MagicMock()
    response.__enter__.return_value.status = 200
    opener = MagicMock()
    opener.open.side_effect = [response, OSError("API/GUI down")]
    with patch.object(run.urllib.request, "build_opener", return_value=opener):
        assert not run.services_ready()
    opener.open.side_effect = [response, response]
    with patch.object(run.urllib.request, "build_opener", return_value=opener):
        assert run.services_ready()
