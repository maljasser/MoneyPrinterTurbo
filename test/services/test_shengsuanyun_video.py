from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from app.services import shengsuanyun_video as service


def _settings(**overrides):
    values = {
        "base_url": "https://example.test/api/v1",
        "api_token": "test-token",
        "request_timeout_seconds": 1,
        "poll_interval_seconds": 1,
        "run_timeout_seconds": 10,
    }
    values.update(overrides)
    return service.ShengsuanVideoSettings(**values)


@pytest.mark.parametrize(
    ("model_id", "expected_keys"),
    [
        ("google/veo3.1-fast-preview", {"prompt", "aspect_ratio"}),
        ("bytedance/doubao-seedance-2-0-fast", {"content", "ratio"}),
        ("openai/sora2", {"prompt", "seconds", "size"}),
        ("minimax/t2v-01-director", {"prompt", "duration", "resolution"}),
    ],
)
def test_supported_models_build_minimal_payloads(model_id, expected_keys):
    payload = service.get_video_model(model_id).build_payload("city sunrise", "9:16")

    assert payload["model"] == model_id
    assert expected_keys <= payload.keys()


def test_unknown_model_falls_back_to_default():
    assert service.get_video_model("unknown").model_id == service.DEFAULT_MODEL_ID


def test_confirmed_request_rejects_unknown_model():
    request = service.ShengsuanConfirmedVideoRequest(
        settings=_settings(),
        batch=service.ShengsuanVideoBatch(
            model_id="unknown",
            prompts=("city sunrise",),
            aspect_ratio="16:9",
        ),
    )

    with pytest.raises(
        service.ShengsuanVideoConfigurationError,
        match="unsupported Shengsuan video model",
    ):
        request.validate()


def test_backend_rejects_unknown_model_before_paid_submit(tmp_path):
    """绕过 WebUI 的调用也不能把未知模型静默替换成默认付费模型。"""
    session = MagicMock()
    backend = service.ShengsuanVideoBackend(_settings(), session=session)
    batch = service.ShengsuanVideoBatch(
        model_id="provider/model-typo",
        prompts=("city sunrise",),
        aspect_ratio="16:9",
    )

    with pytest.raises(
        service.ShengsuanVideoConfigurationError,
        match="unsupported Shengsuan video model",
    ):
        backend.generate_and_download(batch, str(tmp_path))

    session.request.assert_not_called()


def test_settings_reuse_existing_shengsuanyun_api_key():
    settings = service.ShengsuanVideoSettings.from_mapping(
        {"loomloom_api_token": "shared-token"}
    )

    assert settings.api_token == "shared-token"
    assert settings.base_url == service.DEFAULT_BASE_URL


def test_prepare_batch_normalizes_model_and_scene_prompts():
    backend = service.ShengsuanVideoBackend(_settings())

    batch = backend.prepare_batch(
        subject="人工智能如何改变生活",
        scene_prompts=["smart home", "healthcare"],
        aspect_ratio="16:9",
        model_id="bytedance/doubao-seedance-2-0-fast",
    )

    assert batch.model_id == "bytedance/doubao-seedance-2-0-fast"
    assert batch.aspect_ratio == "16:9"
    assert len(batch.prompts) == 2
    assert all("No text" in prompt for prompt in batch.prompts)


def test_submit_network_failure_is_not_retried():
    session = MagicMock()
    session.request.side_effect = requests.ConnectionError("lost")
    backend = service.ShengsuanVideoBackend(_settings(), session=session)

    with pytest.raises(service.ShengsuanVideoAPIError) as error:
        backend._submit({"model": service.DEFAULT_MODEL_ID})

    assert error.value.retryable is False
    session.request.assert_called_once()


def test_wait_for_result_retries_transient_get_failure():
    backend = service.ShengsuanVideoBackend(
        _settings(),
        sleep=lambda _seconds: None,
        clock=iter([0, 0, 1, 2, 3, 4]).__next__,
    )
    backend._request_json = MagicMock(
        side_effect=[
            service.ShengsuanVideoAPIError("busy", retryable=True),
            {
                "code": "success",
                "data": {
                    "status": "COMPLETED",
                    "progress": "100",
                    "data": {"video_urls": ["https://cdn.test/video.mp4"]},
                },
            },
        ]
    )

    result = backend._wait_for_result("request-1")

    assert result["status"] == "COMPLETED"
    assert backend._request_json.call_count == 2


def test_generate_and_download_records_request_and_output(tmp_path, monkeypatch):
    backend = service.ShengsuanVideoBackend(_settings())
    monkeypatch.setattr(backend, "_submit", lambda _payload: "request-1")
    monkeypatch.setattr(
        backend,
        "_wait_for_result",
        lambda _request_id: {"data": {"video_urls": ["https://cdn.test/video.mp4"]}},
    )

    def write_video(_url, destination):
        Path(destination).write_bytes(b"video")

    monkeypatch.setattr(backend, "_download", write_video)
    request_ids = []
    batch = service.ShengsuanVideoBatch(
        model_id=service.DEFAULT_MODEL_ID,
        prompts=("city sunrise",),
        aspect_ratio="16:9",
    )

    outputs = backend.generate_and_download(
        batch,
        str(tmp_path),
        on_request_submitted=request_ids.append,
    )

    assert request_ids == ["request-1"]
    assert len(outputs) == 1
    assert Path(outputs[0]).read_bytes() == b"video"
