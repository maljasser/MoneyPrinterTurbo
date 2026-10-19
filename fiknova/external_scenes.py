"""Review-only external scene generation using existing MPT provider adapters.

This does not call pipeline.render, change approval, replace scene dialogue with
TTS, or publish. A credential check is not an authenticated connection test.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PROVIDERS = {
    "ofox": ("ofox_api_key", "OFOX_API_KEY"),
    "metaso_minimax": ("metaso_minimax_api_key", "METASO_MINIMAX_API_KEY"),
}


class SceneError(ValueError):
    pass


def load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not re.fullmatch(r"[a-zA-Z0-9_-]{2,60}", manifest.get("id", "")):
        raise SceneError("Invalid manifest id")
    if manifest.get("status") != "draft" or manifest.get("fictional") is not True:
        raise SceneError("This preparatory workflow requires a fictional draft")
    if manifest.get("aspect") != "9:16":
        raise SceneError("The cinematic review requires 9:16")
    scenes = manifest.get("scenes")
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 12:
        raise SceneError("Provide 1 to 12 scenes")
    ids = set()
    for scene in scenes:
        identifier = scene.get("id", "")
        if not re.fullmatch(r"S[0-9]{2}", identifier) or identifier in ids:
            raise SceneError("Scene ids must be unique S01-style identifiers")
        ids.add(identifier)
        if type(scene.get("duration")) is not int or not 4 <= scene["duration"] <= 15:
            raise SceneError("Scene durations must be integers from 4 to 15 seconds")
        if not isinstance(scene.get("prompt"), str) or not scene["prompt"].strip():
            raise SceneError("Each scene needs a prompt")
    if not isinstance(manifest.get("continuity_anchor"), str) or not manifest["continuity_anchor"].strip():
        raise SceneError("Provide a continuity anchor")
    return manifest


def settings() -> dict:
    path = Path(os.environ.get("MPT_CONFIG_FILE") or ROOT / "config.toml")
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SceneError("Provide a valid MPT_CONFIG_FILE runtime configuration") from exc
    if config.get("app", {}).get("upload_post_auto_upload") is not False:
        raise SceneError("Set [app].upload_post_auto_upload = false")
    return config["app"]


def credential_available(provider: str, app: dict) -> bool:
    if provider not in PROVIDERS:
        raise SceneError("Choose an explicitly supported external provider")
    config_key, environment_key = PROVIDERS[provider]
    return bool(str(app.get(config_key) or "").strip() or os.environ.get(environment_key, "").strip())


def plan(manifest_path: Path, provider: str) -> dict:
    manifest, app = load_manifest(manifest_path), settings()
    available = {name: credential_available(name, app) for name in PROVIDERS}
    dependencies = {name: importlib.util.find_spec(name) is not None for name in ("requests", "loguru", "toml", "pydantic")}
    tools = {name: shutil.which(name) is not None for name in ("ffmpeg", "ffprobe")}
    return {
        "id": manifest["id"], "status": "draft", "provider": provider,
        "scenes": len(manifest["scenes"]),
        "requested_total_seconds": sum(scene["duration"] for scene in manifest["scenes"]),
        "credentials_available": available, "dependencies_available": dependencies,
        "tools_available": tools, "auto_upload_disabled": True,
        "provider_connection_verified": False,
        "generation_prerequisites_present": credential_available(provider, app) and all(dependencies.values()) and all(tools.values()),
        "approval_changed": False, "publication_enabled": False,
        "note": "Prerequisites do not verify balance, native audio, Arabic dialogue, face continuity, or successful generation.",
    }


def probe(path: Path) -> dict:
    result = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ]))
    videos = [stream for stream in result["streams"] if stream["codec_type"] == "video"]
    audio = [stream for stream in result["streams"] if stream["codec_type"] == "audio"]
    if not videos or not audio:
        raise SceneError("Scene must contain both video and original dialogue audio; silent footage is not accepted")
    return {"duration": float(result["format"]["duration"]), "width": videos[0]["width"],
            "height": videos[0]["height"], "audio_streams": len(audio)}


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _prompt(manifest: dict, scene: dict) -> str:
    return (manifest["continuity_anchor"] + "\n" + scene["prompt"] +
            "\nPhotorealistic fictional AI dramatization, vertical 9:16. Native Arabic dialogue and natural office ambience. "
            "Speak only the provided lines with natural timing. No music, logos, subtitles or readable software UI. "
            "Do not invent extra dialogue, dates, tasks or approvals. Do not portray any real individual.")


def _fingerprint(manifest: dict, scene: dict, provider: str) -> str:
    value = {"provider": provider, "prompt": _prompt(manifest, scene), "duration": scene["duration"]}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _generate(provider: str, prompt: str, duration: int):
    module = importlib.import_module(f"app.services.{provider}")
    return module.generate_videos(search_term=prompt, minimum_duration=duration, video_aspect="9:16")


def _download(url: str, path: Path) -> None:
    if not url.startswith("https://"):
        raise SceneError("Provider result must be an HTTPS media URL")
    temporary = path.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as target:
        shutil.copyfileobj(response, target)
    temporary.replace(path)


def generate(manifest_path: Path, provider: str, scene_id: str, confirmed: bool) -> Path:
    if not confirmed:
        raise SceneError("External generation can incur charges; pass --confirm-paid-generation when authorized")
    manifest, app = load_manifest(manifest_path), settings()
    if not credential_available(provider, app):
        raise SceneError(f"No configured credential for {provider}; no external request submitted")
    checks = plan(manifest_path, provider)
    if not checks["generation_prerequisites_present"]:
        raise SceneError("Install the locked project dependencies and FFmpeg/FFprobe before generation")
    scene = next((item for item in manifest["scenes"] if item["id"] == scene_id), None)
    if scene is None:
        raise SceneError("Unknown scene id")
    prompt = _prompt(manifest, scene)
    fingerprint = _fingerprint(manifest, scene, provider)
    directory = ROOT / "storage/fiknova" / manifest["id"]
    directory.mkdir(parents=True, exist_ok=True)
    receipt_path = directory / f"{scene_id}.receipt.json"
    clip_path = directory / f"{scene_id}.mp4"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get("fingerprint") == fingerprint and receipt.get("state") == "downloaded" and clip_path.is_file():
            if hashlib.sha256(clip_path.read_bytes()).hexdigest() != receipt.get("sha256"):
                raise SceneError("Existing scene changed; inspect the receipt before reuse")
            probe(clip_path)
            return clip_path
        raise SceneError("Existing request receipt found; inspect/recover that request before any new paid submission")
    receipt = {"scene_id": scene_id, "provider": provider, "fingerprint": fingerprint,
               "state": "submitting", "episode_status": "draft", "published": False,
               "dialogue_accuracy": "human review required", "face_continuity": "human review required"}
    _write(receipt_path, receipt)
    try:
        results = _generate(provider, prompt, scene["duration"])
        if not results:
            raise SceneError("Provider returned no video")
        result = results[0]
        receipt["request_id"] = (result.source_info or {}).get("asset_id", "")
        receipt["state"] = "generated"
        _write(receipt_path, receipt)
        _download(result.url, clip_path)
        receipt["media"] = probe(clip_path)
        receipt["sha256"] = hashlib.sha256(clip_path.read_bytes()).hexdigest()
        receipt["state"] = "downloaded"
        _write(receipt_path, receipt)
    except Exception as exc:
        receipt["state"] = "needs_inspection"
        receipt["error_type"] = type(exc).__name__
        receipt["request_id"] = getattr(exc, "task_id", "") or receipt.get("request_id", "")
        _write(receipt_path, receipt)
        # Provider exception messages can contain credentials or expiring URLs.
        raise SceneError(f"Scene {scene_id} stopped ({type(exc).__name__}); inspect its receipt, do not resubmit blindly") from None
    return clip_path


def import_scene(manifest_path: Path, scene_id: str, source: Path) -> Path:
    """Import an existing externally generated clip without a new paid request."""
    manifest = load_manifest(manifest_path)
    settings()
    scene = next((item for item in manifest["scenes"] if item["id"] == scene_id), None)
    if scene is None:
        raise SceneError("Unknown scene id")
    media = probe(source)
    directory = ROOT / "storage/fiknova" / manifest["id"]
    directory.mkdir(parents=True, exist_ok=True)
    receipt_path = directory / f"{scene_id}.receipt.json"
    if receipt_path.exists():
        raise SceneError("Existing receipt must be inspected before importing a replacement")
    target = directory / f"{scene_id}.mp4"
    shutil.copyfile(source, target)
    _write(receipt_path, {"scene_id": scene_id, "provider": "external_import",
        "fingerprint": _fingerprint(manifest, scene, "external_import"),
        "state": "downloaded", "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "media": media, "episode_status": "draft", "published": False,
        "dialogue_accuracy": "human review required", "face_continuity": "human review required"})
    return target


def assemble(manifest_path: Path) -> Path:
    """Create an independent review cut with native scene audio, never TTS."""
    manifest = load_manifest(manifest_path)
    settings()
    directory = ROOT / "storage/fiknova" / manifest["id"]
    sources = []
    for scene in manifest["scenes"]:
        path = directory / f"{scene['id']}.mp4"
        receipt_path = directory / f"{scene['id']}.receipt.json"
        if not path.is_file() or not receipt_path.is_file():
            raise SceneError(f"Missing generated scene: {scene['id']}")
        receipt = json.loads(receipt_path.read_text())
        if receipt.get("state") != "downloaded" or hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get("sha256"):
            raise SceneError("All scene receipts must match downloaded media")
        if receipt.get("fingerprint") != _fingerprint(manifest, scene, receipt.get("provider")):
            raise SceneError("Scene manifest changed since generation; inspect continuity before assembly")
        probe(path)
        sources.append(path)
    from PIL import Image, ImageDraw, ImageFont, features
    if not features.check("raqm"):
        raise SceneError("Pillow RAQM is required for the Arabic disclosure")
    font = ROOT / "resource/fonts/NotoNaskhArabic-Regular.ttf"
    overlay = Image.new("RGBA", (1080, 170), (7, 27, 41, 235))
    draw = ImageDraw.Draw(overlay)
    draw.text((1020, 12), "مشهد تمثيلي مولد بالذكاء الاصطناعي", font=ImageFont.truetype(str(font), 40), fill="white", anchor="ra", direction="rtl")
    draw.text((1020, 78), "بيانات افتراضية • مسودة للمراجعة", font=ImageFont.truetype(str(font), 36), fill="#55E0C1", anchor="ra", direction="rtl")
    overlay_path = directory / "review-disclosure.png"
    overlay.save(overlay_path)
    normalized = []
    for index, source in enumerate(sources):
        target = directory / f"{index:02d}.review-segment.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-i", str(overlay_path),
            "-filter_complex", "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1[base];[base][1:v]overlay=0:0[v]",
            "-map", "[v]", "-map", "0:a:0", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "24",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(target)
        ], check=True)
        normalized.append(target)
    listing = directory / "review-concat.txt"
    listing.write_text("".join("file '" + str(path).replace("'", "'\\''") + "'\n" for path in normalized))
    output = directory / "Fiknova_EP02_Cinematic_Review.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(output)], check=True)
    _write(directory / "cinematic-review.json", {"status": "draft", "published": False,
            "type": "independent_native_audio_review", "audio_replaced_with_tts": False,
            "media": probe(output), "human_review_required": ["Arabic dialogue", "lip sync", "face continuity", "source-note accuracy"]})
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "generate", "import", "assemble"])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--provider", choices=PROVIDERS, default="ofox")
    parser.add_argument("--scene", default="S01")
    parser.add_argument("--clip", type=Path)
    parser.add_argument("--confirm-paid-generation", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "plan":
            print(json.dumps(plan(args.manifest, args.provider), ensure_ascii=False, indent=2))
        elif args.action == "generate":
            print(generate(args.manifest, args.provider, args.scene, args.confirm_paid_generation))
        elif args.action == "import":
            if args.clip is None:
                raise SceneError("Provide --clip with the existing scene file")
            print(import_scene(args.manifest, args.scene, args.clip))
        else:
            print(assemble(args.manifest))
        return 0
    except (SceneError, OSError, json.JSONDecodeError) as exc:
        print(f"Fiknova: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
