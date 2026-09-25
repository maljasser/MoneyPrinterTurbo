"""Prepare and render review-only Fiknova videos using MoneyPrinterTurbo's CLI.

The editorial manifest is intentionally separate from the engine's VideoParams.
This keeps channel copy, an English localization draft, and the review gate together.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FONT = "NotoNaskhArabic-Regular.ttf"
DEFAULT_VOICE = "ar-SA-HamedNeural"
ALLOWED_ASPECTS = {"9:16", "16:9"}


class EpisodeError(ValueError):
    pass


def _read_episode(path: Path) -> dict:
    try:
        episode = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EpisodeError(f"Cannot read episode JSON: {exc}") from exc
    if not isinstance(episode, dict):
        raise EpisodeError("Episode must be a JSON object")
    for key in ("id", "title_ar", "script_ar", "title_en", "script_en"):
        if not isinstance(episode.get(key), str) or not episode[key].strip():
            raise EpisodeError(f"Required non-empty field: {key}")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{2,60}", episode["id"]):
        raise EpisodeError("id must contain only letters, digits, _ or -")
    if episode.get("aspect", "9:16") not in ALLOWED_ASPECTS:
        raise EpisodeError("aspect must be 9:16 or 16:9")
    if episode.get("status", "draft") not in {"draft", "approved"}:
        raise EpisodeError("status must be draft or approved")
    materials = episode.get("materials", [])
    if not isinstance(materials, list) or not all(
        isinstance(item, str) and item.strip() for item in materials
    ):
        raise EpisodeError("materials must be a list of local file paths")
    if len(materials) != len(set(materials)):
        raise EpisodeError("materials must not contain duplicates")
    if not isinstance(episode.get("description_ar", ""), str) or not isinstance(
        episode.get("description_en", ""), str
    ):
        raise EpisodeError("descriptions must be strings")
    return episode


def _resolve_assets(episode: dict, episode_path: Path) -> tuple[list[Path], list[str]]:
    assets: list[Path] = []
    missing: list[str] = []
    for item in episode["materials"]:
        asset = (episode_path.parent / item).resolve()
        if not asset.is_file() or asset.suffix.lower() not in {
            ".mp4", ".mov", ".webm", ".mkv", ".png", ".jpg", ".jpeg"
        }:
            missing.append(item)
        else:
            assets.append(asset)
    return assets, missing


def _config_path() -> Path:
    return Path(os.environ.get("MPT_CONFIG_FILE") or ROOT / "config.toml")


def _check_no_auto_upload(config_path: Path) -> None:
    try:
        with config_path.open("rb") as config_file:
            settings = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise EpisodeError(f"Cannot read runtime config {config_path}: {exc}") from exc
    if settings.get("app", {}).get("upload_post_auto_upload") is not False:
        raise EpisodeError(
            "Set [app].upload_post_auto_upload = false in the runtime config "
            "before rendering a Fiknova review draft"
        )


def _engine_task(episode: dict, assets: list[Path]) -> dict:
    return {
        "video_subject": episode["title_ar"],
        "video_script": episode["script_ar"],
        "video_terms": ["Fiknova"],
        "video_language": "ar-SA",
        "video_source": "local",
        "video_materials": [
            {"provider": "local", "url": str(asset)} for asset in assets
        ],
        "video_aspect": episode.get("aspect", "9:16"),
        "video_fit_mode": "contain",
        "video_concat_mode": "sequential",
        "video_clip_duration": 5,
        "video_count": 1,
        "voice_name": episode.get("voice_name", DEFAULT_VOICE),
        "voice_rate": 1.0,
        "bgm_type": "",
        "bgm_volume": 0.0,
        "subtitle_enabled": True,
        "subtitle_display_mode": "sentence",
        "subtitle_position": "bottom",
        "font_name": episode.get("font_name", DEFAULT_FONT),
        "font_size": 54 if episode.get("aspect", "9:16") == "9:16" else 60,
        "text_fore_color": "#FFFFFF",
        "stroke_color": "#002F3A",
        "stroke_width": 2.0,
    }


def plan(episode_path: Path) -> dict:
    episode_path = episode_path.resolve()
    episode = _read_episode(episode_path)
    assets, missing = _resolve_assets(episode, episode_path)
    font = ROOT / "resource" / "fonts" / episode.get("font_name", DEFAULT_FONT)
    config_safe = False
    if _config_path().is_file():
        try:
            _check_no_auto_upload(_config_path())
            config_safe = True
        except EpisodeError:
            pass
    return {
        "id": episode["id"],
        "status": episode.get("status", "draft"),
        "aspect": episode.get("aspect", "9:16"),
        "title_ar": episode["title_ar"],
        "local_assets_found": len(assets),
        "missing_assets": missing,
        "font_ready": font.is_file(),
        "config_ready": _config_path().is_file(),
        "auto_upload_disabled": config_safe,
        "render_ready": (
            episode.get("status") == "approved"
            and bool(assets)
            and not missing
            and font.is_file()
            and config_safe
        ),
    }


def render(episode_path: Path) -> Path:
    episode_path = episode_path.resolve()
    episode = _read_episode(episode_path)
    if episode.get("status") != "approved":
        raise EpisodeError("Review the script and change status to approved first")
    assets, missing = _resolve_assets(episode, episode_path)
    if missing or not assets:
        raise EpisodeError(f"Local demo footage is missing: {missing or 'no materials'}")
    task = _engine_task(episode, assets)
    if not (ROOT / "resource" / "fonts" / task["font_name"]).is_file():
        raise EpisodeError(f"Arabic font is missing: {task['font_name']}")
    _check_no_auto_upload(_config_path())

    output_dir = ROOT / "storage" / "fiknova" / episode["id"]
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_file = output_dir / "mpt-task.json"
    batch_file.write_text(json.dumps([task], ensure_ascii=False, indent=2), encoding="utf-8")
    command = [sys.executable, str(ROOT / "cli.py"), "--batch-file", str(batch_file)]
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    (output_dir / "render.log").write_text(completed.stderr, encoding="utf-8")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        result = {"error": "MoneyPrinterTurbo returned no valid JSON result"}

    record = {
        "episode_id": episode["id"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "title_ar": episode["title_ar"],
        "description_ar": episode.get("description_ar", ""),
        "title_en": episode["title_en"],
        "description_en": episode.get("description_en", ""),
        "script_en_for_localization_review": episode["script_en"],
        "publication": "not published; review required",
        "source_assets": [str(asset) for asset in assets],
        "engine_result": result,
    }
    record_path = output_dir / "review.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    if completed.returncode or result.get("failed") or result.get("succeeded") != 1:
        raise EpisodeError(f"Render failed; inspect {output_dir / 'render.log'}")
    return record_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Fiknova review-only video pipeline")
    parser.add_argument("action", choices=["plan", "render"])
    parser.add_argument("episode", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "plan":
            print(json.dumps(plan(args.episode), ensure_ascii=False, indent=2))
        else:
            print(render(args.episode))
        return 0
    except EpisodeError as exc:
        print(f"Fiknova: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
