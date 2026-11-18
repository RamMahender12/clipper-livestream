"""Run `clipper run` unattended on macOS: a per-user launchd agent that starts
at login, restarts after a crash, and keeps the Mac awake while it runs.

  clipper service install    write the agent and start it
  clipper service status     is it running, pid, last exit, log path
  clipper service restart    pick up config.toml / secrets changes
  clipper service stop       stop it (stays stopped until start or next install)
  clipper service start
  clipper service uninstall  stop it and remove the agent
"""
from __future__ import annotations

import os
import plistlib
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import config as config_mod

LABEL = "com.gta6clipper.run"
SERVICE_ENV = "CLIPPER_SERVICE"
ENV_FILE = "secrets/clipper.env"
ENV_TEMPLATE = """# Secrets for clipper. This file is git-ignored and readable only by you.
# One KEY=value per line. Variables already set in your shell win.
# After editing, run: .venv/bin/clipper service restart
#
# ANTHROPIC_API_KEY=
# IG_ACCESS_TOKEN=
# TIKTOK_CLIENT_KEY=
# TIKTOK_CLIENT_SECRET=
"""


# ------------------------------------------------------------- secrets file
def parse_env(text: str) -> dict[str, str]:
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key.isidentifier() and value:
            out[key] = value
    return out


def load_env_file(path: Path | None = None) -> list[str]:
    """Set variables from secrets/clipper.env that aren't already set.
    Returns the names loaded (never the values)."""
    path = path or config_mod.ROOT / ENV_FILE
    if not path.exists():
        return []
    loaded = []
    for key, value in parse_env(path.read_text(encoding="utf-8")).items():
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def ensure_env_template() -> Path:
    path = config_mod.ROOT / ENV_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ENV_TEMPLATE, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


# ------------------------------------------------------------- keep awake
def keep_awake() -> subprocess.Popen | None:
    """Hold a no-idle-sleep assertion for as long as this process lives.
    caffeinate -w exits by itself when we do. -s also blocks system sleep on
    AC power. Closing a laptop lid still sleeps the Mac."""
    exe = shutil.which("caffeinate")
    if sys.platform != "darwin" or not exe:
        return None
    return subprocess.Popen([exe, "-i", "-s", "-w", str(os.getpid())],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


# ------------------------------------------------------------- launchd
def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path(cfg: dict) -> Path:
    d = config_mod.data_dir(cfg) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "service.log"


def build_plist(root: Path, python: str, log_file: Path, config_path: str | None = None) -> dict:
    args = [python, "-m", "clipper"]
    if config_path:
        args += ["--config", config_path]
    args.append("run")
    return {
        "Label": LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,  # restart whenever it exits, crash or not; `service stop` unloads it
        "ThrottleInterval": 60,  # at most one restart a minute
        "ProcessType": "Interactive",  # don't let macOS throttle the recorder in the background
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
            SERVICE_ENV: "1",
        },
        "StandardOutPath": str(log_file),
        "StandardErrorPath": str(log_file),
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def is_loaded() -> bool:
    return _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0


def install(cfg: dict) -> str:
    if sys.platform != "darwin":
        raise SystemExit("clipper service is for macOS (launchd).")
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    config_path = cfg.get("_path")
    default_cfg = str(config_mod.ROOT / "config.toml")
    plist = build_plist(config_mod.ROOT, sys.executable, log_path(cfg),
                        None if config_path == default_cfg else config_path)
    with open(path, "wb") as fh:
        plistlib.dump(plist, fh)
    ensure_env_template()
    if is_loaded():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    res = _launchctl("bootstrap", _domain(), str(path))
    if res.returncode != 0:
        raise SystemExit(f"launchctl bootstrap failed: {res.stderr.strip() or res.stdout.strip()}")
    return str(path)


def start() -> None:
    if not plist_path().exists():
        raise SystemExit("Not installed. Run: clipper service install")
    if not is_loaded():
        res = _launchctl("bootstrap", _domain(), str(plist_path()))
        if res.returncode != 0:
            raise SystemExit(f"launchctl bootstrap failed: {res.stderr.strip()}")
    else:
        _launchctl("kickstart", f"{_domain()}/{LABEL}")


def stop() -> None:
    if is_loaded():
        _launchctl("bootout", f"{_domain()}/{LABEL}")


def restart() -> None:
    if not is_loaded():
        start()
        return
    _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}")


def uninstall() -> None:
    stop()
    plist_path().unlink(missing_ok=True)


def status(cfg: dict) -> dict:
    info = {"installed": plist_path().exists(), "loaded": False, "state": "not loaded",
            "pid": None, "last_exit": None, "log": str(log_path(cfg))}
    res = _launchctl("print", f"{_domain()}/{LABEL}")
    if res.returncode != 0:
        return info
    info["loaded"] = True
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("state = "):
            info["state"] = line.split("=", 1)[1].strip()
        elif line.startswith("pid = "):
            info["pid"] = line.split("=", 1)[1].strip()
        elif line.startswith("last exit code = "):
            info["last_exit"] = line.split("=", 1)[1].strip()
    return info
