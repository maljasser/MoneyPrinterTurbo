"""Single-owner Codespaces runtime; GitHub private port forwarding supplies auth."""

import fcntl
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare():
    if os.environ.get("CODESPACES") != "true":
        raise RuntimeError("Use this launcher inside GitHub Codespaces only.")
    fresh = not (ROOT / "config.toml").exists()
    from app.config import config

    source = Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf")
    if source.is_file():
        shutil.copyfile(source, ROOT / "resource/fonts" / source.name)
    if fresh:
        config.app["llm_provider"] = "openai"
        config.app["max_concurrent_tasks"] = 1
        config.ui.update({
            "language": "en", "video_language": "ar-SA",
            "voice_name": "ar-SA-HamedNeural-Male",
            "font_name": source.name,
            "open_task_folder_on_completion": False,
        })
    config._cfg.update({"listen_host": "127.0.0.1", "listen_port": 8080, "log_level": "INFO"})
    config.save_config()
    os.chmod(config.config_file, 0o600)
    (ROOT / "storage").mkdir(exist_ok=True)


def main():
    os.chdir(ROOT)
    os.umask(0o077)
    prepare()
    if "--prepare" in sys.argv:
        return 0
    # A kernel lock avoids duplicate services after reconnecting to a codespace.
    with (ROOT / "storage/codespaces.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("MoneyPrinterTurbo is already running.")
            return 0
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        commands = [
            [sys.executable, "-m", "uvicorn", "app.asgi:app", "--host", "127.0.0.1", "--port", "8080"],
            [sys.executable, "-m", "streamlit", "run", "webui/Main.py",
             "--server.address=127.0.0.1", "--server.port=8501", "--server.headless=true",
             "--server.enableCORS=true", "--server.enableXsrfProtection=true",
             "--browser.gatherUsageStats=false", "--client.toolbarMode=minimal",
             "--logger.hideWelcomeMessage=true", "--server.showEmailPrompt=false"],
        ]
        children = []
        try:
            for cmd in commands:
                children.append(subprocess.Popen(cmd, start_new_session=True))
            while not stop.wait(0.5):
                if any(p.poll() is not None for p in children):
                    raise RuntimeError("A service exited. Check storage/codespaces.log.")
        finally:
            for child in children:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
            for child in children:
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
