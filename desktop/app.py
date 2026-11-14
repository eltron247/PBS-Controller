"""Windows desktop wrapper: WebView2 + a host-key-verified SSH tunnel to PBS."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import webview

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "PBS-Controller"
CONFIG = CONFIG_DIR / "config.json"
DEFAULTS = {
    "host": "pbs",
    "user": "root",
    "ssh_port": 22,
    "remote_port": 8765,
    "identity_file": "",
}


def message(title: str, msg: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, msg, parent=root)
        root.destroy()
    except Exception:
        print(title + ": " + msg, file=sys.stderr)


def ask_config() -> dict:
    import tkinter as tk
    from tkinter import simpledialog
    root = tk.Tk()
    root.withdraw()
    host = simpledialog.askstring("PBS Controller", "PBS SSH hostname or IP:", initialvalue="pbs", parent=root)
    if not host:
        raise SystemExit(0)
    user = simpledialog.askstring("PBS Controller", "SSH username:", initialvalue="root", parent=root)
    if not user:
        raise SystemExit(0)
    identity = simpledialog.askstring(
        "PBS Controller", "Optional SSH private key path (leave blank to use SSH agent/default keys):",
        initialvalue="", parent=root
    )
    root.destroy()
    settings = {**DEFAULTS, "host": host.strip(), "user": user.strip(), "identity_file": (identity or "").strip()}
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings


def load_config() -> dict:
    if not CONFIG.exists():
        return ask_config()
    settings = {**DEFAULTS, **json.loads(CONFIG.read_text(encoding="utf-8"))}
    host, user = settings.get("host", ""), settings.get("user", "")
    if not host or not user or any(c in host + user for c in "\r\n\t"):
        raise ValueError("Invalid host or user in " + str(CONFIG))
    for port_name in ("ssh_port", "remote_port"):
        port = int(settings[port_name])
        if port < 1 or port > 65535:
            raise ValueError("Invalid " + port_name + " in " + str(CONFIG))
        settings[port_name] = port
    return settings


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_tunnel(settings: dict, local_port: int) -> subprocess.Popen:
    ssh = shutil.which("ssh.exe") or shutil.which("ssh")
    if not ssh:
        raise RuntimeError("Windows OpenSSH client not found on PATH. Install the Windows OpenSSH Client.")
    dest = settings["user"] + "@" + settings["host"]
    args = [
        ssh, "-N", "-T", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-p", str(settings["ssh_port"]),
        "-L", f"127.0.0.1:{local_port}:127.0.0.1:{settings['remote_port']}",
    ]
    if settings.get("identity_file"):
        args.extend(["-i", settings["identity_file"]])
    args.append(dest)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, creationflags=flags,
    )
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            detail = (proc.stderr.read() or b"").decode("utf-8", errors="replace")
            raise RuntimeError("SSH tunnel failed.\n" + detail[:1200])
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=0.3):
                return proc
        except OSError:
            time.sleep(0.15)
    proc.terminate()
    raise RuntimeError("Timed out connecting to PBS over SSH. Verify the host, SSH key and controller service.")


def main() -> None:
    proc = None
    try:
        config = load_config()
        port = free_local_port()
        proc = start_tunnel(config, port)
        webview.create_window(
            "PBS Controller", "http://127.0.0.1:" + str(port),
            width=1270, height=900, min_size=(800, 560),
        )
        webview.start(gui="edgechromium", debug=False)
    except Exception as exc:
        message("PBS Controller", str(exc) + "\n\nRun 'ssh " +
                (config["user"] + "@" + config["host"] if "config" in locals() else "root@pbs") +
                "' in a terminal first to verify SSH and the host fingerprint.\nConfig: " + str(CONFIG))
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
