#!/usr/bin/env python3
import uvicorn
import os
import secrets
import signal
import subprocess
import time
import urllib.request
from pathlib import Path
from alembic import command
from alembic.config import Config

if __name__ == "__main__":
    from app.config import settings
    # A fresh private transport secret shared only by these two local processes.
    secret = settings.whatsapp_worker_secret or secrets.token_urlsafe(32)
    os.environ["WHATSAPP_WORKER_SECRET"] = secret
    settings.whatsapp_worker_secret = secret
    data_root = Path(os.getenv("WHATSAPP_DATA_DIR", str(Path(os.getenv("UPLOAD_DIR", "uploads")).resolve().parent / "whatsapp-data")))
    environment = {**os.environ, "WHATSAPP_STORAGE_KEY": settings.token_encryption_key or settings.secret_key,
                   "WHATSAPP_DATA_DIR": str(data_root)}
    command.upgrade(Config("alembic.ini"), "head")
    worker = subprocess.Popen(["node", "server.mjs"], cwd=Path(__file__).parent / "whatsapp-worker", env=environment, start_new_session=True)
    import threading
    stopping = threading.Event()
    try:
        for attempt in range(60):
            if worker.poll() is not None:
                raise RuntimeError("Built-in WhatsApp service could not start")
            try:
                request = urllib.request.Request(settings.whatsapp_worker_url + "/health", headers={"Authorization": "Bearer " + secret})
                with urllib.request.urlopen(request, timeout=1):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("Built-in WhatsApp service startup timed out")
        # Terminate the app if its WhatsApp service dies; Fly restarts both together.
        def supervise():
            worker.wait()
            if not stopping.is_set():
                os.kill(os.getpid(), signal.SIGTERM)
        threading.Thread(target=supervise, daemon=True).start()
        uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
    finally:
        stopping.set()
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
