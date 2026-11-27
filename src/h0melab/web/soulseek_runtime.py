"""Installs, configures and supervises the Soulseek service process.

The service (``h0melab/soulseek_service/service.py``) is a separate GPL program
built on aioslsk. It is never imported here: this module gives it a private
virtual environment with the newest aioslsk, writes its config, starts it as a
child process and keeps it running. Everything else talks to it over HTTP on
127.0.0.1 with a random token, see soulseek.py.
"""

import atexit
import json
import os
import secrets
import socket
import string
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..logger import get_logger

logger = get_logger(__name__)

KEYS = {
    "enabled": "H0MELAB_SOULSEEK_ENABLED",
    "username": "H0MELAB_SOULSEEK_USERNAME",
    "password": "H0MELAB_SOULSEEK_PASSWORD",
    "listen_port": "H0MELAB_SOULSEEK_PORT",
    "share_downloads": "H0MELAB_SOULSEEK_SHARE",
}
DEFAULT_LISTEN_PORT = 50300
PACKAGES = ("aioslsk", "aiohttp")
UPDATE_EVERY = 24 * 60 * 60
SERVICE_SCRIPT = Path(__file__).resolve().parent.parent / "soulseek_service" / "service.py"
_RESTART_DELAYS = (5, 15, 30, 60, 120)

_lock = threading.RLock()
_state = {
    "phase": "idle",  # idle | installing | starting | running | error | disabled
    "message": "",
    "version": "",
}
_process = None
_supervisor = None
_stop = threading.Event()


class RuntimeUnavailable(RuntimeError):
    """The service is not (yet) available; the message says why."""


# ---------------------------------------------------------------------------
# Paths and settings
# ---------------------------------------------------------------------------
def base_dir():
    from ..config import H0MELAB_CONFIG_DIR

    return Path(H0MELAB_CONFIG_DIR) / "soulseek"


def _venv_python():
    venv = base_dir() / "venv"
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def enabled():
    return os.environ.get(KEYS["enabled"], "1") == "1"


def listen_port():
    try:
        port = int(os.environ.get(KEYS["listen_port"], "") or DEFAULT_LISTEN_PORT)
    except ValueError:
        return DEFAULT_LISTEN_PORT
    return port if 1024 <= port <= 65534 else DEFAULT_LISTEN_PORT


def share_downloads():
    return os.environ.get(KEYS["share_downloads"], "1") == "1"


def _generated_credentials():
    """A Soulseek account of our own; the server creates it on first login."""
    path = base_dir() / "account.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("username") and data.get("password"):
            return data["username"], data["password"]
    except (OSError, ValueError):
        pass
    alphabet = string.ascii_lowercase + string.digits
    username = "h0melab-" + "".join(secrets.choice(alphabet) for _ in range(8))
    password = secrets.token_urlsafe(18)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"username": username, "password": password}), encoding="utf-8")
    return username, password


def credentials():
    """(username, password, generated?) the service logs in with."""
    username = os.environ.get(KEYS["username"], "").strip()
    password = os.environ.get(KEYS["password"], "").strip()
    if username and password:
        return username, password, False
    generated_user, generated_password = _generated_credentials()
    return generated_user, generated_password, True


def settings():
    from . import music_convert, soulseek

    username, _, generated = credentials() if _configured_once() else ("", "", True)
    return {
        "enabled": enabled(),
        "username": os.environ.get(KEYS["username"], "").strip(),
        "password_set": bool(os.environ.get(KEYS["password"], "").strip()),
        "active_username": username,
        "generated_account": generated,
        "listen_port": listen_port(),
        "share_downloads": share_downloads(),
        "music_path": os.environ.get(soulseek.KEYS["music_path"], "").strip(),
        "music_path_effective": str(soulseek.music_path()),
        **music_convert.settings(),
        "status": status(),
    }


def _configured_once():
    return (base_dir() / "account.json").exists() or bool(
        os.environ.get(KEYS["username"], "").strip()
    )


def collect_settings(payload, updates):
    from .settings_store import SECRET_PLACEHOLDER, SettingsError

    if not isinstance(payload, dict):
        raise SettingsError("Soulseek settings must be an object")
    if "enabled" in payload:
        updates[KEYS["enabled"]] = "1" if payload["enabled"] else "0"
    if "share_downloads" in payload:
        updates[KEYS["share_downloads"]] = "1" if payload["share_downloads"] else "0"
    for name in ("username", "password"):
        if name not in payload:
            continue
        value = payload[name]
        if not isinstance(value, str):
            raise SettingsError("Invalid Soulseek setting")
        value = value.strip()
        if name == "password" and value == SECRET_PLACEHOLDER:
            continue
        if len(value) > 64 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise SettingsError(f"Invalid Soulseek {name}")
        updates[KEYS[name]] = value
    if "listen_port" in payload:
        raw = payload["listen_port"]
        try:
            port = int(raw)
        except (TypeError, ValueError):
            raise SettingsError("Soulseek port must be a number") from None
        if isinstance(raw, bool) or not 1024 <= port <= 65534:
            raise SettingsError("Soulseek port must be between 1024 and 65534")
        updates[KEYS["listen_port"]] = str(port)
    if "music_path" in payload:
        value = payload["music_path"]
        if not isinstance(value, str) or len(value) > 1024:
            raise SettingsError("Invalid music folder")
        from .soulseek import KEYS as SOULSEEK_KEYS

        updates[SOULSEEK_KEYS["music_path"]] = value.strip()
    from . import music_convert

    music_convert.collect_settings(payload, updates)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def _set(phase, message="", **extra):
    with _lock:
        _state["phase"] = phase
        _state["message"] = message
        _state.update(extra)


def status():
    with _lock:
        return dict(_state)


def connection():
    """(base_url, token) of the running service, or RuntimeUnavailable."""
    if not enabled():
        raise RuntimeUnavailable("Soulseek is switched off in the settings")
    ensure_started()
    current = status()
    if current["phase"] != "running":
        raise RuntimeUnavailable(current["message"] or "Soulseek is starting…")
    info = _read_runtime()
    if not info:
        raise RuntimeUnavailable("Soulseek is starting…")
    return f"http://127.0.0.1:{info['api_port']}", info["api_token"]


def _read_runtime():
    try:
        return json.loads((base_dir() / "runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Install / update
# ---------------------------------------------------------------------------
def _run(args, timeout):
    result = subprocess.run(
        [str(arg) for arg in args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(" / ".join(tail) or f"exit code {result.returncode}")
    return result.stdout


def _installed_version():
    try:
        out = _run([_venv_python(), "-c", "from importlib.metadata import version; print(version('aioslsk'))"], 60)
        return out.strip()
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return ""


def install(force_update=False):
    """Create the venv and install or update aioslsk. Returns the version."""
    if getattr(sys, "frozen", False):
        raise RuntimeError("The bundled desktop build cannot install the Soulseek client; use Docker or pip")
    base = base_dir()
    base.mkdir(parents=True, exist_ok=True)
    stamp = base / "last-update"
    python = _venv_python()
    if not python.exists():
        _set("installing", "Preparing the Soulseek client…")
        _run([sys.executable, "-m", "venv", base / "venv"], 300)
        force_update = True
    due = True
    try:
        due = time.time() - float(stamp.read_text()) > UPDATE_EVERY
    except (OSError, ValueError):
        pass
    if force_update or due or not _installed_version():
        _set("installing", "Downloading the newest Soulseek client (aioslsk)…")
        _run([python, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", *PACKAGES], 900)
        stamp.write_text(str(time.time()))
    version = _installed_version()
    if not version:
        raise RuntimeError("aioslsk could not be installed")
    return version


# ---------------------------------------------------------------------------
# Process
# ---------------------------------------------------------------------------
def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_config():
    from . import soulseek

    base = base_dir()
    username, password, _ = credentials()
    music = soulseek.music_path()
    music.mkdir(parents=True, exist_ok=True)
    runtime = {"api_port": _free_port(), "api_token": secrets.token_urlsafe(32)}
    liked = []
    try:
        liked = json.loads((base / "likes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    config = {
        **runtime,
        "username": username,
        "password": password,
        "listen_port": listen_port(),
        "upnp": True,
        "download_dir": str(music),
        "shared_dirs": [str(music)] if share_downloads() else [],
        "data_dir": str(base / "data"),
        "liked": liked if isinstance(liked, list) else [],
        "watch_parent": True,
    }
    # Advanced/testing: another Soulseek server as host:port
    server = os.environ.get("H0MELAB_SOULSEEK_SERVER", "").strip()
    if server:
        host, _, port = server.rpartition(":")
        config["server_host"] = host or server
        config["server_port"] = int(port) if port.isdigit() else 2416
    path = base / "service-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    (base / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
    return path, runtime


def _launch():
    global _process
    config_path, runtime = _write_config()
    log = open(base_dir() / "service.log", "a", encoding="utf-8")  # noqa: SIM115 - kept for the child
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _process = subprocess.Popen(
        [str(_venv_python()), "-I", str(SERVICE_SCRIPT), "--config", str(config_path)],
        stdout=log,
        stderr=subprocess.STDOUT,
        # Held open for the child's lifetime: it exits when this pipe closes
        stdin=subprocess.PIPE,
        **kwargs,
    )
    log.close()
    return runtime


def _wait_until_listening(runtime, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process is None or _process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", runtime["api_port"]), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _terminate():
    global _process
    process = _process
    _process = None
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _supervise():
    attempt = 0
    while not _stop.is_set():
        try:
            version = install()
            _set("starting", "Starting the Soulseek client…", version=version)
            runtime = _launch()
            if not _wait_until_listening(runtime):
                raise RuntimeError("the Soulseek service did not start, see soulseek/service.log")
            _set("running", "", version=version)
            attempt = 0
            while not _stop.is_set() and _process is not None and _process.poll() is None:
                _stop.wait(2)
            if _stop.is_set():
                break
            raise RuntimeError("the Soulseek service stopped unexpectedly, see soulseek/service.log")
        except Exception as exc:
            logger.warning("Soulseek service: %s", exc)
            _set("error", str(exc))
        _terminate()
        delay = _RESTART_DELAYS[min(attempt, len(_RESTART_DELAYS) - 1)]
        attempt += 1
        _stop.wait(delay)
    _terminate()


def ensure_started():
    """Start the supervisor thread once (installs on first use)."""
    global _supervisor
    if not enabled():
        _set("disabled", "Soulseek is switched off in the settings")
        return
    with _lock:
        if _supervisor is not None and _supervisor.is_alive():
            return
        _stop.clear()
        _set("starting", "Starting the Soulseek client…")
        _supervisor = threading.Thread(target=_supervise, name="h0melab-soulseek", daemon=True)
        _supervisor.start()
    from . import music_convert

    music_convert.start()


def start_if_used_before():
    """At app start: bring the service back only for installs that use it."""
    if enabled() and _venv_python().exists():
        ensure_started()


def stop():
    global _supervisor
    _stop.set()
    supervisor = _supervisor
    _supervisor = None
    if supervisor is not None:
        supervisor.join(timeout=15)
    _terminate()
    if not enabled():
        _set("disabled", "Soulseek is switched off in the settings")
    else:
        _set("idle")


def restart():
    stop()
    ensure_started()


def update_now():
    """Force an aioslsk update and restart."""
    stamp = base_dir() / "last-update"
    try:
        stamp.unlink()
    except OSError:
        pass
    restart()


def remember_like(item):
    """Keep liked interests across restarts (the server forgets them)."""
    path = base_dir() / "likes.json"
    try:
        likes = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(likes, list):
            likes = []
    except (OSError, ValueError):
        likes = []
    if item in likes:
        return
    likes = (likes + [item])[-200:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(likes), encoding="utf-8")


atexit.register(_terminate)
