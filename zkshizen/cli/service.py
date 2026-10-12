"""`zks service install|uninstall|status`: run `zks serve` at boot.

Linux    systemd unit /etc/systemd/system/zkshizen.service (needs sudo)
macOS    launchd agent ~/Library/LaunchAgents/com.zkshizen.harness.plist
Windows  Task Scheduler task "zkshizen-harness" at logon (current user)

`--print` only shows the file/command that would be installed.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

from zkshizen.config import get_settings

UNIT = "/etc/systemd/system/zkshizen.service"
PLIST = Path.home() / "Library" / "LaunchAgents" / "com.zkshizen.harness.plist"
TASK = "zkshizen-harness"


def _zks_cmd() -> list[str]:
    exe = shutil.which("zks")
    return [exe, "serve"] if exe else [sys.executable, "-m", "zkshizen", "serve"]


def systemd_unit(user: str) -> str:
    s = get_settings()
    cmd = " ".join(_zks_cmd())
    return f"""[Unit]
Description=zkshizen harness
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Environment=PYTHONUNBUFFERED=1
Environment=ZKS_DATA_DIR={s.data_dir}
EnvironmentFile=-{s.data_dir}/zkshizen.env
ExecStart={cmd}
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={s.data_dir}

[Install]
WantedBy=multi-user.target
"""


def launchd_plist() -> str:
    s = get_settings()
    args = "".join(f"<string>{a}</string>" for a in _zks_cmd())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.zkshizen.harness</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>EnvironmentVariables</key><dict><key>ZKS_DATA_DIR</key><string>{s.data_dir}</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{s.data_dir}/server.log</string>
  <key>StandardErrorPath</key><string>{s.data_dir}/server.log</string>
</dict></plist>
"""


def windows_task_cmd() -> list[str]:
    s = get_settings()
    inner = " ".join(f'"{a}"' for a in _zks_cmd())
    action = f'cmd /c "set ZKS_DATA_DIR={s.data_dir}&& {inner} >> "{s.data_dir}\\server.log" 2>&1"'
    return ["schtasks", "/Create", "/F", "/TN", TASK, "/SC", "ONLOGON", "/RL", "LIMITED", "/TR", action]


def _run(cmd: list[str]) -> int:
    print("$ " + " ".join(cmd))
    return subprocess.run(cmd).returncode


def cmd_install(ns: argparse.Namespace) -> int:
    get_settings().data_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("linux"):
        unit = systemd_unit(ns.user or getpass.getuser())
        if ns.print:
            print(unit)
            return 0
        if os.geteuid() != 0:
            raise SystemExit(f"writing {UNIT} needs root. Run: sudo env ZKS_DATA_DIR=$ZKS_DATA_DIR zks service "
                             "install --user $USER   (or --print and install it yourself)")
        Path(UNIT).write_text(unit, encoding="utf-8")
        return _run(["systemctl", "daemon-reload"]) or _run(["systemctl", "enable", "--now", "zkshizen"])
    if sys.platform == "darwin":
        plist = launchd_plist()
        if ns.print:
            print(plist)
            return 0
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_text(plist, encoding="utf-8")
        _run(["launchctl", "unload", str(PLIST)])
        return _run(["launchctl", "load", "-w", str(PLIST)])
    if sys.platform == "win32":
        cmd = windows_task_cmd()
        if ns.print:
            print(" ".join(cmd))
            return 0
        rc = _run(cmd)
        if rc == 0:
            _run(["schtasks", "/Run", "/TN", TASK])
        return rc
    raise SystemExit(f"unsupported platform {sys.platform}")


def cmd_uninstall(_: argparse.Namespace) -> int:
    if sys.platform.startswith("linux"):
        _run(["systemctl", "disable", "--now", "zkshizen"])
        Path(UNIT).unlink(missing_ok=True)
        return _run(["systemctl", "daemon-reload"])
    if sys.platform == "darwin":
        _run(["launchctl", "unload", str(PLIST)])
        PLIST.unlink(missing_ok=True)
        return 0
    if sys.platform == "win32":
        _run(["schtasks", "/End", "/TN", TASK])
        return _run(["schtasks", "/Delete", "/F", "/TN", TASK])
    raise SystemExit(f"unsupported platform {sys.platform}")


def cmd_status(_: argparse.Namespace) -> int:
    if sys.platform.startswith("linux"):
        return _run(["systemctl", "status", "--no-pager", "zkshizen"])
    if sys.platform == "darwin":
        return _run(["launchctl", "list", "com.zkshizen.harness"])
    if sys.platform == "win32":
        return _run(["schtasks", "/Query", "/TN", TASK, "/V", "/FO", "LIST"])
    raise SystemExit(f"unsupported platform {sys.platform}")
