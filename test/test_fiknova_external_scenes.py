"""Review gates, provider handoff and native audio preservation for Fiknova."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fiknova import external_scenes as scenes


class ExternalScenesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / "episode.json"
        self.data = {"id": "ep02-review", "status": "draft", "fictional": True,
                     "aspect": "9:16", "continuity_anchor": "Same fictional coworkers",
                     "scenes": [{"id": "S01", "duration": 8, "prompt": "Speak Arabic"}]}
        self.manifest.write_text(json.dumps(self.data))
        self.config = self.root / "config.toml"
        self.config.write_text('[app]\nupload_post_auto_upload = false\n')
        for context in (patch.object(scenes, "ROOT", self.root),
                        patch.dict("os.environ", {"MPT_CONFIG_FILE": str(self.config), "OFOX_API_KEY": "", "METASO_MINIMAX_API_KEY": ""})):
            context.start()
            self.addCleanup(context.stop)

    def test_missing_key_never_calls_a_provider(self):
        report = scenes.plan(self.manifest, "ofox")
        self.assertFalse(report["provider_connection_verified"])
        self.assertFalse(report["generation_prerequisites_present"])
        with patch.object(scenes, "_generate") as generate:
            with self.assertRaisesRegex(scenes.SceneError, "No configured credential"):
                scenes.generate(self.manifest, "ofox", "S01", True)
            generate.assert_not_called()
        self.assertFalse((self.root / "storage").exists())

    def test_charge_confirmation_and_no_upload_setting_are_required(self):
        with self.assertRaisesRegex(scenes.SceneError, "confirm-paid-generation"):
            scenes.generate(self.manifest, "ofox", "S01", False)
        self.config.write_text('[app]\nupload_post_auto_upload = true\n')
        with self.assertRaisesRegex(scenes.SceneError, "auto_upload"):
            scenes.plan(self.manifest, "ofox")

    def test_uncertain_submission_is_not_repeated(self):
        error = RuntimeError("provider failure; possible charged job")
        error.task_id = "request-123"
        with patch.object(scenes, "credential_available", return_value=True), \
             patch.object(scenes, "plan", return_value={"generation_prerequisites_present": True}), \
             patch.object(scenes, "_generate", side_effect=error) as generate:
            with self.assertRaisesRegex(scenes.SceneError, "stopped"):
                scenes.generate(self.manifest, "ofox", "S01", True)
            with self.assertRaisesRegex(scenes.SceneError, "Existing request"):
                scenes.generate(self.manifest, "ofox", "S01", True)
            self.assertEqual(generate.call_count, 1)
        receipt = json.loads((self.root / 'storage/fiknova/ep02-review/S01.receipt.json').read_text())
        self.assertEqual(receipt["request_id"], "request-123")
        self.assertEqual(receipt["episode_status"], "draft")

    def test_successful_result_is_cached_without_another_paid_request(self):
        result = SimpleNamespace(url="https://example.invalid/clip.mp4", source_info={"asset_id": "job-1"})
        with patch.object(scenes, "credential_available", return_value=True), \
             patch.object(scenes, "plan", return_value={"generation_prerequisites_present": True}), \
             patch.object(scenes, "_generate", return_value=[result]) as generate, \
             patch.object(scenes, "_download", side_effect=lambda url, path: path.write_bytes(b"clip")), \
             patch.object(scenes, "probe", return_value={"audio_streams": 1}):
            first = scenes.generate(self.manifest, "ofox", "S01", True)
            self.assertEqual(first, scenes.generate(self.manifest, "ofox", "S01", True))
            self.assertEqual(generate.call_count, 1)
        self.assertEqual(json.loads(self.manifest.read_text())["status"], "draft")

    def test_provider_adapter_receives_exact_prompt_duration_and_aspect(self):
        adapter = SimpleNamespace(generate_videos=lambda **kwargs: kwargs)
        with patch.object(scenes.importlib, "import_module", return_value=adapter):
            result = scenes._generate("ofox", "النص المحدد", 8)
        self.assertEqual(result, {"search_term": "النص المحدد", "minimum_duration": 8, "video_aspect": "9:16"})

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_real_audio_is_required_and_survives_review_assembly(self):
        from PIL import features
        if not features.check("raqm"):
            self.skipTest("Arabic disclosure requires RAQM")
        font_candidates = [
            Path(__file__).resolve().parents[1] / "resource/fonts/NotoNaskhArabic-Regular.ttf",
            Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"),
        ]
        font = next((path for path in font_candidates if path.is_file()), None)
        if font is None:
            self.skipTest("Arabic font unavailable")
        destination = self.root / "resource/fonts" / font.name
        destination.parent.mkdir(parents=True)
        shutil.copyfile(font, destination)
        source = self.root / "tone.mp4"
        silent = self.root / "silent.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=navy:s=180x320:r=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-an", "-c:v", "copy", str(silent)], check=True)
        with self.assertRaisesRegex(scenes.SceneError, "original dialogue"):
            scenes.import_scene(self.manifest, "S01", silent)
        scenes.import_scene(self.manifest, "S01", source)
        output = scenes.assemble(self.manifest)
        self.assertEqual(scenes.probe(output)["audio_streams"], 1)
        pcm = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(output), "-map", "0:a:0", "-f", "s16le", "-ac", "1", "-"])
        import array
        samples = array.array("h", pcm)
        self.assertGreater(max(abs(value) for value in samples), 100)
        self.assertEqual(json.loads(self.manifest.read_text())["status"], "draft")
        # Changing the script must invalidate the previous generated material.
        self.data["scenes"][0]["prompt"] = "Changed dialogue"
        self.manifest.write_text(json.dumps(self.data))
        with self.assertRaisesRegex(scenes.SceneError, "manifest changed"):
            scenes.assemble(self.manifest)


if __name__ == "__main__":
    unittest.main()
