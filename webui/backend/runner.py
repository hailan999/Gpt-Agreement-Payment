"""单 active-run 的 pipeline 进程控制器。

封装 `python pipeline.py [args]` 子进程：spawn / 流式收 stdout
到环形日志缓冲 / SIGTERM-优先 stop / 暴露 status + log 给路由层。

GoPay 模式下额外支持 OTP 中转：gopay.py 在 stdout 打印
`GOPAY_OTP_REQUEST path=<file>` 标记后阻塞，runner 记下 file 路径，
等前端 POST /run/otp 提交 OTP 后写入 file，gopay.py 读取继续。

如果启用了自动 WhatsApp relay，gopay.py 不会走旧的 `--gopay-otp-file`
手动 FIFO，而是打印 `[gopay] waiting WhatsApp OTP from file: ...` 并轮询
配置里的 `output/wa_otp.txt`。GoPay/WhatsApp 的 OTP 模板在 linked
device 上经常只显示“只能在主要设备查看”，所以 runner 也会识别这条
waiting 日志，把同一个文件路径注册成可手动补写的 fallback。
"""
import json
import os
import platform
import shutil
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from . import settings as s


_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_started_at: Optional[float] = None
_ended_at: Optional[float] = None
_exit_code: Optional[int] = None
_cmd: Optional[list[str]] = None
_mode: Optional[str] = None
_log_lines: list[dict] = []  # {seq, ts, line}
_seq_counter = 0
_otp_file: Optional[Path] = None       # path passed via --gopay-otp-file or parsed from auto-provider wait log
_otp_pending: bool = False             # set when gopay.py asks/waits for OTP
_otp_file_is_temp: bool = False


def _gopay_auto_otp_enabled() -> bool:
    """Return True when config has a non-manual gopay.otp provider.

    WebUI historically always passed --gopay-otp-file, which forces the manual
    modal path. When a relay/file/command provider is configured, skip that
    fifo so gopay.py can consume the configured auto provider directly.
    """
    try:
        cfg = json.loads(s.PAY_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    gp = cfg.get("gopay") or {}
    if not isinstance(gp, dict):
        return False
    otp = gp.get("otp") or gp.get("otp_provider") or {}
    if not isinstance(otp, dict):
        return False
    source = str(otp.get("source") or otp.get("type") or "auto").strip().lower()
    if source in ("", "manual", "cli", "stdin"):
        return False
    has_url = bool((otp.get("url") or otp.get("relay_url") or "").strip())
    has_path = bool((otp.get("path") or otp.get("state_file") or otp.get("log_file") or "").strip())
    has_command = bool(otp.get("command") or otp.get("cmd"))
    if source in ("http", "https", "relay", "whatsapp_http", "wa_http"):
        return has_url
    if source in ("file", "state_file", "log", "whatsapp_file", "wa_file"):
        return has_path
    if source in ("command", "cmd"):
        return has_command
    if source == "auto":
        return has_url or has_path or has_command
    return False


def build_cmd(mode: str, paypal: bool, batch: int, workers: int, self_dealer: int,
              register_only: bool, pay_only: bool, gopay: bool = False,
              gopay_otp_file: str = "", count: int = 0) -> list[str]:
    """根据参数拼出最终命令行。"""
    cmd: list[str] = []
    if platform.system() != "Windows" and shutil.which("xvfb-run"):
        cmd.extend(["xvfb-run", "-a"])
    cmd.extend([sys.executable, "-u", "pipeline.py", "--config", str(s.PAY_CONFIG_PATH)])
    # free_only 两个子模式不需要 paypal / gopay 支付段
    if mode in ("free_register", "free_backfill_rt"):
        if mode == "free_register":
            cmd.append("--free-register")
            if count > 0:
                cmd.extend(["--count", str(count)])
        else:
            cmd.append("--free-backfill-rt")
        return cmd
    if gopay:
        cmd.append("--gopay")
        if gopay_otp_file:
            cmd.extend(["--gopay-otp-file", gopay_otp_file])
    elif paypal:
        cmd.append("--paypal")
    if register_only:
        cmd.append("--register-only")
    elif pay_only:
        cmd.append("--pay-only")
    elif mode == "daemon":
        cmd.append("--daemon")
    elif mode == "self_dealer":
        cmd.extend(["--self-dealer", str(self_dealer)])
    elif mode == "batch":
        cmd.extend(["--batch", str(batch), "--workers", str(workers)])
    # mode == "single" → no extra flags
    return cmd


def status() -> dict:
    global _proc
    is_running = _proc is not None and _proc.poll() is None
    return {
        "running": is_running,
        "started_at": _started_at,
        "ended_at": _ended_at,
        "exit_code": _exit_code if not is_running else None,
        "cmd": _cmd,
        "mode": _mode,
        "pid": _proc.pid if is_running and _proc else None,
        "log_count": _seq_counter,
        "otp_pending": _otp_pending,
    }


def start(*, mode: str, paypal: bool = True, batch: int = 0, workers: int = 3,
          self_dealer: int = 0, register_only: bool = False, pay_only: bool = False,
          gopay: bool = False, count: int = 0) -> dict:
    global _proc, _started_at, _ended_at, _exit_code, _cmd, _mode
    global _log_lines, _seq_counter, _otp_file, _otp_pending, _otp_file_is_temp
    with _lock:
        if _proc is not None and _proc.poll() is None:
            raise RuntimeError("a pipeline is already running")

        # Allocate OTP fifo path (file deleted by gopay.py after read)
        otp_path = ""
        otp_p: Optional[Path] = None
        if gopay and not _gopay_auto_otp_enabled():
            tmp = tempfile.NamedTemporaryFile(
                prefix="gopay_otp_", suffix=".txt", delete=False,
            )
            tmp.close()
            otp_p = Path(tmp.name)
            otp_p.unlink(missing_ok=True)  # gopay.py polls for existence
            otp_path = str(otp_p)

        cmd = build_cmd(mode, paypal, batch, workers, self_dealer,
                        register_only, pay_only, gopay=gopay,
                        gopay_otp_file=otp_path, count=count)

        # Reset
        _log_lines = []
        _seq_counter = 0
        _started_at = time.time()
        _ended_at = None
        _exit_code = None
        _cmd = cmd
        _mode = mode
        _otp_file = otp_p
        _otp_file_is_temp = otp_p is not None
        _otp_pending = False

        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(s.ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
        except FileNotFoundError as e:
            _ended_at = time.time()
            _exit_code = -1
            raise RuntimeError(f"failed to spawn: {e}") from e
        _proc = proc

        threading.Thread(target=_drain, args=(proc,), daemon=True).start()
    return status()


def _detect_otp_wait_path(line: str) -> Optional[Path]:
    """Return file path from GoPay OTP wait markers, if present."""
    if "GOPAY_OTP_REQUEST" in line:
        m = re.search(r"\bpath=(.+?)\s*$", line)
        if m:
            return Path(m.group(1).strip().strip("'\""))
        return _otp_file

    # Configured auto provider path, e.g.
    # [gopay] waiting WhatsApp OTP from file: /.../output/wa_otp.txt
    m = re.search(r"\[gopay\]\s+waiting WhatsApp OTP from file:\s*(.+?)\s*$", line)
    if m:
        return Path(m.group(1).strip().strip("'\""))
    return None


def _drain(proc: subprocess.Popen) -> None:
    global _ended_at, _exit_code, _seq_counter, _log_lines, _otp_pending, _otp_file, _otp_file_is_temp
    try:
        if proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip()
            if not line:
                continue
            with _lock:
                _seq_counter += 1
                _log_lines.append({"seq": _seq_counter, "ts": time.time(), "line": line})
                if len(_log_lines) > 3000:
                    _log_lines = _log_lines[-2000:]
                # Detect GoPay OTP request/wait markers.  The second form is
                # used by the configured WhatsApp relay provider; making it
                # pending lets the existing WebUI OTP modal act as a fallback
                # when WhatsApp hides OTP bodies from linked devices.
                wait_path = _detect_otp_wait_path(line)
                if wait_path is not None:
                    _otp_file = wait_path
                    _otp_file_is_temp = _otp_file_is_temp or "GOPAY_OTP_REQUEST" in line
                    _otp_pending = True
    finally:
        proc.wait()
        with _lock:
            _ended_at = time.time()
            _exit_code = proc.returncode
            _otp_pending = False
            # Cleanup OTP file.  For the auto relay path this intentionally
            # removes stale OTPs too; future waits use mtime checks, but an
            # empty/clean file is easier to reason about.
            if _otp_file is not None:
                try:
                    _otp_file.unlink(missing_ok=True)
                except Exception:
                    pass


def stop() -> dict:
    global _proc
    with _lock:
        proc = _proc
        if proc is None or proc.poll() is not None:
            return status()
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return status()


def submit_otp(value: str) -> dict:
    """Front-end calls this with the OTP user typed. Writes to fifo path."""
    global _otp_pending
    with _lock:
        if not _otp_pending or _otp_file is None:
            raise RuntimeError("no OTP currently requested")
        path = _otp_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.strip(), encoding="utf-8")
    with _lock:
        _otp_pending = False
    return status()


def get_lines_since(since_seq: int = 0, limit: int = 1000) -> list[dict]:
    with _lock:
        return [e for e in _log_lines if e["seq"] > since_seq][:limit]


def get_tail(n: int = 200) -> list[dict]:
    with _lock:
        return _log_lines[-n:]
