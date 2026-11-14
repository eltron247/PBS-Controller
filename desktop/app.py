"""Windows desktop wrapper: WebView2 + a host-key-verified SSH tunnel to PBS."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import webview

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "PBS-Controller"
CONFIG = CONFIG_DIR / "config.json"
DEFAULTS = {
    "host": "pbs",
    "user": "pbs-tunnel",
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
    user = simpledialog.askstring("PBS Controller", "SSH username:", initialvalue=DEFAULTS["user"], parent=root)
    if not user:
        raise SystemExit(0)
    identity = simpledialog.askstring(
        "PBS Controller", "Optional SSH private key path (leave blank to use SSH agent/default keys):",
        initialvalue="", parent=root
    )
    root.destroy()
    settings = {**DEFAULTS, "host": host.strip(), "user": user.strip(), "identity_file": (identity or "").strip()}
    validate_config(settings)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings


def load_config() -> dict:
    if not CONFIG.exists():
        return ask_config()
    settings = {**DEFAULTS, **json.loads(CONFIG.read_text(encoding="utf-8"))}
    validate_config(settings)
    return settings


def validate_config(settings: dict) -> None:
    host, user = settings.get("host", ""), settings.get("user", "")
    if (not isinstance(host, str) or not isinstance(user, str) or not host or not user
            or host.startswith("-") or user.startswith("-")
            or "@" in host + user or any(c.isspace() or ord(c) < 32 or c == "\x7f" for c in host + user)):
        raise ValueError("Invalid host or user in " + str(CONFIG))
    for port_name in ("ssh_port", "remote_port"):
        port = int(settings[port_name])
        if port < 1 or port > 65535:
            raise ValueError("Invalid " + port_name + " in " + str(CONFIG))
        settings[port_name] = port


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Tunnel:
    """Continuously drain SSH diagnostics, retaining at most 64 KiB."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._tail = deque(maxlen=16)
        self._tail_lock = threading.Lock()
        self._reader = threading.Thread(target=self._drain, name="ssh-stderr", daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        while chunk := self.proc.stderr.read1(4096):
            with self._tail_lock:
                self._tail.append(chunk)

    def error_tail(self) -> str:
        with self._tail_lock:
            return b"".join(self._tail).decode("utf-8", errors="replace")[-1200:]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=4)
        self._reader.join(timeout=1)
        self.proc.stderr.close()


def start_tunnel(settings: dict, local_port: int, stopping: threading.Event | None = None) -> Tunnel:
    validate_config(settings)
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
    args.extend(["--", dest])
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, creationflags=flags,
    )
    tunnel = Tunnel(proc)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if stopping is not None and stopping.is_set():
            break
        if proc.poll() is not None:
            tunnel.close()
            raise RuntimeError("SSH tunnel failed.\n" + tunnel.error_tail())
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=0.3):
                return tunnel
        except OSError:
            time.sleep(0.15)
    tunnel.close()
    raise RuntimeError("Timed out connecting to PBS over SSH. Verify the host, SSH key and controller service.\n" + tunnel.error_tail())


class TunnelSupervisor:
    def __init__(self, settings: dict, port: int):
        self.settings, self.port = settings, port
        self.stopping = threading.Event()
        self.tunnel = start_tunnel(settings, port)
        self.worker = threading.Thread(target=self._supervise, name="ssh-supervisor", daemon=True)
        self.worker.start()

    def _supervise(self) -> None:
        backoff = 1
        connected_at = time.monotonic()
        try:
            while not self.stopping.wait(0.5):
                if self.tunnel is not None:
                    if self.tunnel.proc.poll() is None:
                        if time.monotonic() - connected_at >= 30:
                            backoff = 1
                        continue
                    self.tunnel.close()
                    self.tunnel = None
                if self.stopping.wait(backoff):
                    break
                try:
                    self.tunnel = start_tunnel(self.settings, self.port, self.stopping)
                    connected_at = time.monotonic()
                except (RuntimeError, OSError):
                    pass  # Existing page polling recovers when SSH/controller returns.
                backoff = min(30, backoff * 2)
        finally:
            if self.tunnel is not None:
                self.tunnel.close()

    def close(self) -> None:
        self.stopping.set()
        self.worker.join()


def main() -> None:
    supervisor = None
    try:
        config = load_config()
        port = free_local_port()
        supervisor = TunnelSupervisor(config, port)
        webview.create_window(
            "PBS Controller", "http://127.0.0.1:" + str(port),
            width=1270, height=900, min_size=(800, 560),
        )
        webview.start(gui="edgechromium", debug=False)
    except Exception as exc:
        message("PBS Controller", str(exc) + "\n\nRun 'ssh -N -T -L 8765:127.0.0.1:8765 -- " +
                (config["user"] + "@" + config["host"] if "config" in locals() else "pbs-tunnel@pbs") +
                "' in a terminal first to verify SSH and the host fingerprint.\nConfig: " + str(CONFIG))
    finally:
        if supervisor is not None:
            supervisor.close()


if __name__ == "__main__":
    main()
