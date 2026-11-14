"""Narrow, testable adapter for one systemd-managed PBS ingest job."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UNIT = "pbs-truenas-backup.service"
SYSTEMCTL = "/usr/bin/systemctl"
MAX_LOG_CHUNK = 65536
INITIAL_TAIL = 131072
ACTIVE_STATES = {"active", "activating", "deactivating"}


class BusyError(Exception):
    """The backup is already active or a start is in flight."""


class NotRunningError(Exception):
    """The backup service is not running."""


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts is not None else None


class Controller:
    def __init__(
        self,
        log_path: str = "/var/log/run-pbs-job.log",
        db_path: str = "/var/lib/pbs-controller/history.sqlite3",
        unit: str = UNIT,
        systemctl: str = SYSTEMCTL,
        sudo: str = "/usr/bin/sudo",
    ) -> None:
        self.log_path = Path(log_path)
        self.db_path = Path(db_path)
        self.unit = unit
        self.systemctl = systemctl
        self.sudo = sudo
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started REAL NOT NULL,
                finished REAL,
                status TEXT NOT NULL,
                log_offset INTEGER NOT NULL,
                log_file_id TEXT,
                exit_code INTEGER,
                message TEXT
            )""")

    def _db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _last(self) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def _update(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"status", "finished", "exit_code", "message"}
        if set(fields) - allowed:
            raise ValueError("Invalid history update")
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self._db() as db:
            db.execute(
                f"UPDATE runs SET {columns} WHERE id = ?",
                (*fields.values(), run_id),
            )

    def _command(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=12, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Could not contact systemd: {exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[:500]
            raise RuntimeError(detail or f"Command failed with status {result.returncode}")
        return result

    def _unit_state(self) -> dict[str, Any]:
        keys = (
            "LoadState,ActiveState,SubState,Result,ExecMainStatus,ExecMainCode,"
            "ExecMainStartTimestampMonotonic,MainPID"
        )
        output = self._command(
            [self.systemctl, "show", "--no-pager", f"--property={keys}", self.unit]
        ).stdout
        props = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if props.get("LoadState") != "loaded":
            raise RuntimeError(f"systemd unit {self.unit} is not loaded")
        active_state = props.get("ActiveState", "unknown")
        running = active_state in ACTIVE_STATES
        try:
            exit_code = int(props.get("ExecMainStatus", "") or "-1")
        except ValueError:
            exit_code = -1
        try:
            start_microseconds = int(props.get("ExecMainStartTimestampMonotonic", "0"))
        except ValueError:
            start_microseconds = 0
        elapsed = (
            max(0, int(time.monotonic() - start_microseconds / 1_000_000))
            if running and start_microseconds > 0
            else None
        )
        return {
            "active_state": active_state,
            "sub_state": props.get("SubState", ""),
            "result": props.get("Result", ""),
            "exit_code": exit_code if exit_code >= 0 else None,
            "running": running,
            "elapsed_seconds": elapsed,
            "main_pid": int(props.get("MainPID", "0") or "0"),
            "started_by_systemd": start_microseconds > 0,
        }

    def _reconcile(self, state: dict[str, Any]) -> dict[str, Any] | None:
        last = self._last()
        if not last or last["status"] not in {"starting", "running", "stopping"}:
            return last
        if state["running"]:
            if last["status"] == "starting":
                self._update(last["id"], status="running")
                last["status"] = "running"
            return last

        # systemctl start --no-block can return before systemd has run ExecStart.
        if last["status"] == "starting" and time.time() - last["started"] < 4:
            return last

        if last["status"] == "stopping":
            outcome = "interrupted"
        elif (
            state["result"] == "success"
            and state["exit_code"] == 0
            and state["started_by_systemd"]
        ):
            outcome = "completed"
        else:
            outcome = "failed"
        self._update(
            last["id"], status=outcome, finished=time.time(),
            exit_code=state["exit_code"],
        )
        last = self._last()
        return last

    @staticmethod
    def _public_run(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "id": row["id"],
            "status": row["status"],
            "started_at": iso(row["started"]),
            "finished_at": iso(row["finished"]),
            "log_offset": row["log_offset"],
            "log_file_id": row["log_file_id"],
            "exit_code": row["exit_code"],
            "message": row["message"],
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._unit_state()
            last = self._reconcile(state)
            display = "running" if state["running"] else (
                "failed" if state["active_state"] == "failed" else "idle"
            )
            if not state["running"] and last and last["status"] == "starting":
                display = "starting"
            return {
                "state": display,
                "unit": self.unit,
                "systemd": state,
                "run": self._public_run(last),
                "log_path": str(self.log_path),
            }

    def start(self) -> dict[str, Any]:
        with self._lock:
            state = self._unit_state()
            last = self._reconcile(state)
            if state["running"] or (last and last["status"] in {"starting", "running", "stopping"}):
                raise BusyError("A backup is already running or starting")
            log_offset, file_id = self._log_position()
            now = time.time()
            with self._db() as db:
                cursor = db.execute(
                    "INSERT INTO runs (started, status, log_offset, log_file_id) VALUES (?, ?, ?, ?)",
                    (now, "starting", log_offset, file_id),
                )
                run_id = cursor.lastrowid
            try:
                self._command(
                    [self.sudo, "-n", self.systemctl, "start", "--no-block", self.unit]
                )
            except RuntimeError as exc:
                self._update(
                    run_id, status="failed", finished=time.time(), message=str(exc)
                )
                raise
            return {"id": run_id, "status": "starting", "log_offset": log_offset, "log_file_id": file_id}

    def stop(self) -> dict[str, str]:
        with self._lock:
            state = self._unit_state()
            if not state["running"]:
                raise NotRunningError("No running backup to stop")
            self._command([self.sudo, "-n", self.systemctl, "stop", self.unit])
            last = self._last()
            if last and last["status"] in {"starting", "running"}:
                self._update(last["id"], status="stopping")
            return {"status": "stopping"}

    def history(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            self._reconcile(self._unit_state())
            with self._db() as db:
                rows = db.execute(
                    "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (min(100, max(1, limit)),)
                ).fetchall()
            return [self._public_run(dict(row)) for row in rows]

    def _log_position(self) -> tuple[int, str | None]:
        try:
            stat = self.log_path.stat()
        except FileNotFoundError:
            return 0, None
        return stat.st_size, f"{stat.st_dev}:{stat.st_ino}"

    def log_chunk(self, offset: int | None, file_id: str | None = None) -> dict[str, Any]:
        try:
            with self.log_path.open("rb") as file:
                st = os.fstat(file.fileno())
                actual_id = f"{st.st_dev}:{st.st_ino}"
                size = st.st_size
                reset = (file_id is not None and file_id != actual_id) or (
                    offset is not None and offset > size
                )
                if offset is None or reset:
                    last = self._last()
                    if not reset and last and last["log_file_id"] == actual_id:
                        offset = min(last["log_offset"], size)
                    else:
                        offset = max(0, size - INITIAL_TAIL)
                offset = max(0, offset)
                file.seek(offset)
                data = file.read(MAX_LOG_CHUNK)
                return {
                    "text": data.decode("utf-8", errors="replace"),
                    "offset": offset + len(data),
                    "file_id": actual_id,
                    "size": size,
                    "reset": reset,
                    "more": offset + len(data) < size,
                }
        except FileNotFoundError:
            return {"text": "", "offset": 0, "file_id": None, "size": 0, "reset": True, "more": False}

    def latest_log_start(self) -> int:
        last = self._last()
        size, file_id = self._log_position()
        if last and last["log_file_id"] == file_id:
            return min(last["log_offset"], size)
        return max(0, size - INITIAL_TAIL)
