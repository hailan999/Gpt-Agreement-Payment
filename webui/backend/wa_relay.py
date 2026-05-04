"""WhatsApp Web sidecar lifecycle + OTP state reader.

The WebUI exposes one user-facing "WhatsApp 登录" entry. Behind it, this module
manages a single Node sidecar (`webui/whatsapp_relay/index.js`) that logs in to
WhatsApp Web, watches incoming messages, extracts GoPay OTPs, and writes them to
`output/wa_otp.txt` for `gopay.py` to consume via the configured file provider.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import settings as s


_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_mode: str = ""
_started_at: Optional[float] = None


def _data_dir() -> Path:
    d = s.get_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path() -> Path:
    return _data_dir() / "wa_state.json"


def _otp_path() -> Path:
    return _data_dir() / "wa_otp.txt"


def _session_dir() -> Path:
    p = _data_dir() / "wa_session"
    p.mkdir(parents=True, exist_ok=True)
    return p


def otp_path() -> Path:
    """Path that gopay.py should poll for relay-fed WhatsApp OTPs."""
    return _otp_path()


def is_running() -> bool:
    return _proc is not None and _proc.poll() is None


def status() -> dict:
    """Read state file written by the Node sidecar.

    The state file may be stale (e.g. WebUI restarted, relay died). When the
    sidecar is not running, force status to `stopped` so callers do not treat a
    stale `connected` state as live.
    """
    running = is_running()
    base = {
        "running": running,
        "pid": _proc.pid if running and _proc else None,
        "mode": _mode,
        "started_at": _started_at,
        "otp_path": str(_otp_path()),
        "state_path": str(_state_path()),
    }
    sp = _state_path()
    if sp.exists():
        try:
            loaded = json.loads(sp.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                base.update(loaded)
        except Exception as e:
            base["state_read_error"] = str(e)

    if not running:
        base["status"] = "stopped"
        for key in ("qr", "qr_data_url", "qr_ascii", "code"):
            base.pop(key, None)
    elif "status" not in base:
        base["status"] = "starting"
    return base


def start(mode: str = "qr", pairing_phone: str = "") -> dict:
    """Spawn the Node sidecar in QR mode.

    `pairing` mode is kept at the API level for compatibility, but the WebUI now
    exposes only the QR WhatsApp login entry.
    """
    global _proc, _mode, _started_at

    mode = (mode or "qr").lower()
    if mode not in ("qr", "pairing"):
        raise ValueError(f"mode must be qr or pairing, got {mode!r}")
    if mode == "pairing":
        digits = "".join(ch for ch in (pairing_phone or "") if ch.isdigit())
        if len(digits) < 10:
            raise ValueError("pairing 模式需要 pairing_phone（含国家码，10+ 位数字）")
        pairing_phone = digits

    with _lock:
        if is_running() and _mode == mode:
            return status()

        _stop_locked()
        _purge_stale_chrome(_session_dir())

        relay_dir = s.WA_RELAY_DIR
        index_js = relay_dir / "index.js"
        if not index_js.exists():
            raise RuntimeError(f"relay sidecar 缺失: {index_js}")
        node_modules = relay_dir / "node_modules"
        if not node_modules.exists():
            raise RuntimeError(f"未安装 sidecar 依赖；先跑 `cd {relay_dir} && npm install`")

        for path in (_state_path(), _otp_path()):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

        env = {
            **os.environ,
            # Baileys listens on the raw WhatsApp multi-device socket and is
            # more suitable for OTP capture than DOM scraping via Chromium.
            # Set WEBUI_WA_ENGINE=wwebjs to fall back to whatsapp-web.js.
            "WA_ENGINE": os.environ.get("WEBUI_WA_ENGINE", "baileys"),
            "WA_LOGIN_MODE": mode,
            "WA_STATE_FILE": str(_state_path()),
            "WA_OTP_FILE": str(_otp_path()),
            "WA_SESSION_DIR": str(_session_dir()),
            "WA_HEADLESS": "1",
        }
        if mode == "pairing":
            env["WA_PAIRING_PHONE"] = pairing_phone

        log_path = _data_dir() / "wa_relay.log"
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            proc = subprocess.Popen(
                ["node", str(index_js)],
                cwd=str(relay_dir),
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        finally:
            os.close(log_fd)

        _proc = proc
        _mode = mode
        _started_at = time.time()

        # Give the sidecar a short window to fail fast (missing browser, bad
        # dependency, etc.) so the UI gets a useful error instead of spinning.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if proc.poll() is not None:
                _proc = None
                _mode = ""
                _started_at = None
                detail = ""
                try:
                    detail = log_path.read_text(encoding="utf-8", errors="replace")[-1200:]
                except Exception:
                    pass
                raise RuntimeError(f"WhatsApp relay 启动后退出: rc={proc.returncode} {detail}")
            if _state_path().exists():
                break
            time.sleep(0.1)
        return status()


def _purge_stale_chrome(session_dir: Path) -> None:
    """Kill orphan Chromium processes using our WhatsApp session dir."""
    pat = str(session_dir.resolve())
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"chrome.*user-data-dir={pat}"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        out = ""
    for line in out.splitlines():
        try:
            os.kill(int(line.strip()), signal.SIGKILL)
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        for p in session_dir.rglob(name):
            try:
                p.unlink()
            except (FileNotFoundError, IsADirectoryError):
                pass

    for d in glob.glob("/tmp/org.chromium.Chromium.*"):
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def _stop_locked() -> None:
    global _proc, _mode, _started_at

    proc = _proc
    if proc is None:
        return
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait()
    _proc = None
    _mode = ""
    _started_at = None


def stop() -> dict:
    with _lock:
        _stop_locked()
    return status()


def logout() -> dict:
    """Stop sidecar and remove WhatsApp session so the next start shows QR."""
    with _lock:
        _stop_locked()
        sd = _session_dir()
        _purge_stale_chrome(sd)
        if sd.exists():
            shutil.rmtree(sd, ignore_errors=True)
        sd.mkdir(parents=True, exist_ok=True)
        for path in (_state_path(), _otp_path()):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return {"status": "logged_out"}
