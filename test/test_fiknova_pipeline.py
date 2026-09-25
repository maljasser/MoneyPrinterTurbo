"""Checks the Fiknova production gate without media providers or project imports."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fiknova import pipeline


class FiknovaPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.episode_dir = self.root / "fiknova" / "episodes"
        self.episode_dir.mkdir(parents=True)
        self.asset = self.root / "demo.mp4"
        self.asset.write_bytes(b"test video")
        self.font = self.root / "resource" / "fonts" / pipeline.DEFAULT_FONT
        self.font.parent.mkdir(parents=True)
        self.font.write_bytes(b"test font")
        self.config = self.root / "config.toml"
        self.config.write_text("[app]\nupload_post_auto_upload = false\n")
        self.episode_path = self.episode_dir / "episode.json"
        self.episode = {
            "id": "episode-02",
            "status": "draft",
            "title_ar": "مثال تجريبي",
            "script_ar": "نص تجريبي",
            "title_en": "Demo",
            "script_en": "Demo script",
            "materials": [str(self.asset)],
        }
        self.save()
        self.root_patch = patch.object(pipeline, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.config_patch = patch.dict("os.environ", {"MPT_CONFIG_FILE": str(self.config)})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def save(self):
        self.episode_path.write_text(json.dumps(self.episode, ensure_ascii=False))

    def test_draft_cannot_render_even_when_assets_exist(self):
        self.assertFalse(pipeline.plan(self.episode_path)["render_ready"])
        with self.assertRaisesRegex(pipeline.EpisodeError, "approved"):
            pipeline.render(self.episode_path)

    def test_auto_upload_setting_blocks_render(self):
        self.episode["status"] = "approved"
        self.save()
        self.config.write_text("[app]\nupload_post_auto_upload = true\n")
        self.assertFalse(pipeline.plan(self.episode_path)["render_ready"])
        with self.assertRaisesRegex(pipeline.EpisodeError, "upload_post_auto_upload"):
            pipeline.render(self.episode_path)

    def test_successful_cli_result_creates_review_package(self):
        self.episode["status"] = "approved"
        self.save()
        fake_result = {"succeeded": 1, "failed": 0, "tasks": [
            {"task_id": "mock-id", "result": {"videos": ["/fake/clip.mp4"]}}
        ]}
        with patch.object(pipeline.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps(fake_result)
            run.return_value.stderr = "render logs"
            review_path = pipeline.render(self.episode_path)
        command = run.call_args.args[0]
        self.assertIn("--batch-file", command)
        self.assertNotIn("upload", " ".join(command))
        review = json.loads(review_path.read_text())
        self.assertEqual(review["publication"], "not published; review required")
        task = json.loads((review_path.parent / "mpt-task.json").read_text())[0]
        self.assertEqual(task["video_source"], "local")
        self.assertEqual(task["voice_name"], "ar-SA-HamedNeural")
        self.assertEqual(task["video_concat_mode"], "sequential")


if __name__ == "__main__":
    unittest.main()
