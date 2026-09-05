"""Check a locally built mpt-cloud Docker image without paid provider calls."""

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout.strip()


def main():
    name = "mpt-smoke-" + uuid.uuid4().hex[:10]
    volume = name + "-data"
    password = "temporary-test-" + uuid.uuid4().hex
    password_hash = command(
        "docker", "run", "--rm", "--entrypoint", "caddy", "mpt-cloud",
        "hash-password", "--plaintext", password,
    )
    env = {**os.environ, "MPT_AUTH_USER": "test-owner", "MPT_AUTH_PASSWORD_HASH": password_hash}
    token = base64.b64encode(f"test-owner:{password}".encode()).decode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, authenticated=False):
        headers = {"Authorization": f"Basic {token}"} if authenticated else {}
        req = urllib.request.Request("http://127.0.0.1:18080" + path, headers=headers)
        try:
            with opener.open(req, timeout=6) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def wait_ready():
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if request("/healthz")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(1)
        raise AssertionError("Runtime did not become ready")

    def execute(*args):
        return command("docker", "exec", name, *args)

    command("docker", "volume", "create", volume)
    try:
        command(
            "docker", "run", "-d", "--name", name,
            "-p", "127.0.0.1:18080:8080", "-v", f"{volume}:/data",
            "-e", "MPT_AUTH_USER", "-e", "MPT_AUTH_PASSWORD_HASH", "mpt-cloud", env=env,
        )
        wait_ready()
        for path in ("/", "/docs", "/openapi.json", "/api/v1/tasks", "/tasks/test.mp4", "/_stcore/stream"):
            assert request(path)[0] == 401, f"Unprotected path: {path}"
        for path in ("/", "/docs", "/openapi.json", "/ping"):
            assert request(path, True)[0] == 200, f"Authenticated path failed: {path}"
        assert "paths" in json.loads(request("/openapi.json", True)[1])

        execute("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                "-i", "testsrc2=size=1280x720:rate=24", "-t", "2", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "/data/storage/smoke-input.mp4")
        cli_output = execute(
            "python", "cli.py", "--video-script", "اختبار الفيديو",
            "--video-source", "local", "--video-materials", "/data/storage/smoke-input.mp4",
            "--voice-name", "no-voice", "--subtitle-enabled", "--font-name", "NotoNaskhArabic-Regular.ttf",
            "--video-aspect", "16:9", "--bgm-type", "random", "--n-threads", "2",
        )
        # The CLI writes terminal logs before its final one-line JSON result.
        output = json.loads(cli_output.splitlines()[-1])["result"]
        assert output["videos"], output
        media = json.loads(execute("ffprobe", "-v", "error", "-show_streams", "-of", "json", output["videos"][0]))
        assert {stream["codec_type"] for stream in media["streams"]} >= {"audio", "video"}

        execute("python", "-c", "from app.config import config; config.app['cloud_smoke_sentinel']='saved'; config.save_config()")
        command("docker", "restart", "--time", "25", name)
        wait_ready()
        assert execute("python", "-c", "from app.config import config; print(config.app['cloud_smoke_sentinel'])").endswith("saved")
        execute("test", "-s", output["videos"][0])
        print("PASS: private gateway, API, WebUI, Arabic subtitles, MP4/audio render and persistent restart.")
    except Exception:
        logs = subprocess.run(["docker", "logs", "--tail", "80", name], capture_output=True, text=True)
        print(logs.stdout)
        print(logs.stderr)
        raise
    finally:
        # Only the disposable test container and its unique test volume are removed.
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True)


if __name__ == "__main__":
    main()
