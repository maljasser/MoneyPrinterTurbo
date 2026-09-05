"""Run the WebUI and API behind one authenticated HTTPS-edge gateway.

Use the container from deploy/Dockerfile. The hosting platform must terminate
HTTPS and mount persistent storage at MPT_DATA_DIR (normally /data).
"""

import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import toml

ROOT = Path(__file__).resolve().parents[1]
PROBES = ("http://127.0.0.1:8081/ping", "http://127.0.0.1:8501/_stcore/health")


def validate_environment(env):
    """Fail closed before importing the app or opening any listening socket."""
    user = env.get("MPT_AUTH_USER", "")
    password_hash = env.get("MPT_AUTH_PASSWORD_HASH", "")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", user):
        raise ValueError("Set MPT_AUTH_USER to a simple login name (1-64 characters).")
    if not re.fullmatch(r"\$2[aby]\$(?:1[0-6])\$[./A-Za-z0-9]{53}", password_hash):
        raise ValueError("Set MPT_AUTH_PASSWORD_HASH using caddy hash-password (bcrypt cost 10-16).")
    port = int(env.get("PORT", "8080"))
    if not 1024 <= port <= 65535 or port in (8081, 8090, 8501):
        raise ValueError("PORT must be 1024-65535 and distinct from internal ports.")
    data_dir = Path(env.get("MPT_DATA_DIR", "/data")).resolve()
    if env.get("RAILWAY_ENVIRONMENT_ID"):
        mounted = env.get("RAILWAY_VOLUME_MOUNT_PATH", "")
        if not mounted or Path(mounted).resolve() != data_dir:
            raise ValueError("Attach a Railway volume at MPT_DATA_DIR before deploying.")
    return data_dir


def prepare_config(data_dir):
    """Seed once; keep saved settings and keys on the volume on every restart."""
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in ("storage", "models", "huggingface"):
        (data_dir / name).mkdir(exist_ok=True)
    os.environ["MPT_CONFIG_FILE"] = str(data_dir / "config.toml")
    os.environ["HF_HOME"] = str(data_dir / "huggingface")
    first_start = not (data_dir / "config.toml").exists()

    # Import only after the persistent config location has been selected.
    from app.config import config

    if first_start:
        config.app["max_concurrent_tasks"] = 1
        config.app["llm_provider"] = "openai"
        config.ui.update({
            "language": "en",  # Upstream does not yet ship an Arabic UI.
            "video_language": "ar-SA",
            "voice_name": "ar-SA-HamedNeural-Male",
            "font_name": "NotoNaskhArabic-Regular.ttf",
            "open_task_folder_on_completion": False,
        })

    # CLI host overrides and the gateway keep the API/WebUI on loopback.
    config._cfg["listen_host"] = "127.0.0.1"
    config._cfg["listen_port"] = 8081
    config._cfg["log_level"] = "INFO"
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if domain:
        if not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
            raise ValueError("RAILWAY_PUBLIC_DOMAIN must contain a hostname only.")
        config.app["endpoint"] = f"https://{domain}"
    config.save_config()
    os.chmod(config.config_file, 0o600)


def services_ready():
    # Loopback probes must not use an outbound HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in PROBES:
        try:
            with opener.open(url, timeout=2) as response:
                if response.status != 200:
                    return False
        except (OSError, ValueError):
            return False
    return True


class ReadinessHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/healthz":
            self.send_error(404)
            return
        ready = services_ready()
        body = b"ok\n" if ready else b"not ready\n"
        self.send_response(200 if ready else 503)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    os.umask(0o077)
    data_dir = validate_environment(os.environ)
    prepare_config(data_dir)
    commands = [
        [sys.executable, "-m", "uvicorn", "app.asgi:app", "--host", "127.0.0.1", "--port", "8081"],
        [sys.executable, "-m", "streamlit", "run", "webui/Main.py",
         "--server.address=127.0.0.1", "--server.port=8501",
         "--server.headless=true", "--server.enableCORS=true",
         "--server.enableXsrfProtection=true", "--browser.gatherUsageStats=false",
         "--client.toolbarMode=minimal", "--logger.hideWelcomeMessage=true",
         "--server.showEmailPrompt=false"],
        ["caddy", "run", "--config", str(ROOT / "deploy/Caddyfile"), "--adapter", "caddyfile"],
    ]
    children = []
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    health = ThreadingHTTPServer(("127.0.0.1", 8090), ReadinessHandler)
    threading.Thread(target=health.serve_forever, daemon=True).start()
    result = 0
    try:
        for command in commands:
            children.append(subprocess.Popen(command, cwd=ROOT, start_new_session=True))
        while not stop.wait(0.5):
            if any(child.poll() is not None for child in children):
                print("A service exited; stopping the runtime for a clean host restart.", file=sys.stderr)
                result = 1
                break
    finally:
        health.shutdown()
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + 20
        for child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    return result


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, toml.TomlDecodeError) as exc:
        print(f"Cloud runtime could not start: {exc}", file=sys.stderr)
        sys.exit(1)
