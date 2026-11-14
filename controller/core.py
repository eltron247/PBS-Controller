"""Narrow, testable adapter for one systemd-managed PBS ingest job."""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

UNIT = "pbs-truenas-backup.service"
SYSTEMCTL = "/usr/bin/systemctl"
MAX_LOG_CHUNK = 65536
INITIAL_TAIL = 131072
ACTIVE_STATES = {"active", "activating", "deactivating"}
START_GRACE_SECONDS = 60


def signal_name(number: int | None) -> str | None:
    if number is None:
        return None
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"SIG{number}"


def systemd_timestamp(value: str) -> float | None:
    """Parse systemctl --timestamp=us+utc without losing subsecond precision."""
    try:
        return datetime.fromisoformat(value.split(" ", 1)[1].replace(" UTC", "+00:00")).timestamp()
    except (ValueError, IndexError):
        return None


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
            columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
            for name, kind in (("invocation_id", "TEXT"), ("invocation_started", "REAL"), ("signal", "INTEGER")):
                if name not in columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(str(self.db_path), timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection

    def _last(self) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def _update(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"status", "finished", "exit_code", "message", "invocation_id", "invocation_started", "signal"}
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
            "InvocationID,ExecMainStartTimestamp,ExecMainExitTimestamp,"
            "ExecMainStartTimestampMonotonic,MainPID"
        )
        output = self._command(
            [self.systemctl, "show", "--no-pager", "--timestamp=us+utc", f"--property={keys}", self.unit]
        ).stdout
        props = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if props.get("LoadState") != "loaded":
            raise RuntimeError(f"systemd unit {self.unit} is not loaded")
        active_state = props.get("ActiveState", "unknown")
        running = active_state in ACTIVE_STATES
        try:
            main_status = int(props.get("ExecMainStatus", "") or "-1")
            main_code = int(props.get("ExecMainCode", "0") or "0")
        except ValueError:
            main_status, main_code = -1, 0
        killed_signal = main_status if main_code in {2, 3} and main_status > 0 else None
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
            "exit_code": main_status if main_code == 1 and main_status >= 0 else None,
            "signal": killed_signal,
            "signal_name": signal_name(killed_signal),
            "running": running,
            "elapsed_seconds": elapsed,
            "main_pid": int(props.get("MainPID", "0") or "0"),
            "invocation_id": props.get("InvocationID") or None,
            "started_at": systemd_timestamp(props.get("ExecMainStartTimestamp", "")),
            "finished_at": systemd_timestamp(props.get("ExecMainExitTimestamp", "")),
        }

    def _reconcile(self, state: dict[str, Any]) -> dict[str, Any] | None:
        last = self._last()
        if not last or last["status"] not in {"starting", "running", "stopping"}:
            return last
        started = state["started_at"]
        if last["invocation_started"] is None:
            # A previous invocation's retained success is never evidence of this start.
            if started is None or not last["started"] <= started <= last["started"] + START_GRACE_SECONDS:
                if time.time() >= last["started"] + START_GRACE_SECONDS:
                    self._update(last["id"], status="failed", finished=last["started"] + START_GRACE_SECONDS,
                                 message="Unit did not start")
                return self._last()
            self._update(last["id"], invocation_id=state["invocation_id"], invocation_started=started)
            last = self._last()
        elif (started != last["invocation_started"] or
              (state["invocation_id"] and last["invocation_id"] and
               state["invocation_id"] != last["invocation_id"])):
            # systemd only retains the latest invocation. Do not borrow a manual run's result.
            self._update(last["id"], status="failed", message="Systemd invocation changed; outcome unavailable")
            return self._last()
        elif state["invocation_id"] and not last["invocation_id"]:
            self._update(last["id"], invocation_id=state["invocation_id"])
            last["invocation_id"] = state["invocation_id"]

        if state["running"]:
            if last["status"] == "starting":
                self._update(last["id"], status="running")
                last["status"] = "running"
            return last

        finished = state["finished_at"]
        if finished is not None and finished < started:
            finished = None
        if last["status"] == "stopping":
            outcome = "interrupted"
        elif (
            state["result"] == "success"
            and state["exit_code"] == 0
            and finished is not None
        ):
            outcome = "completed"
        else:
            outcome = "failed"
        self._update(
            last["id"], status=outcome, finished=finished,
            exit_code=state["exit_code"], signal=state["signal"],
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
            "signal": row["signal"],
            "signal_name": signal_name(row["signal"]),
            "invocation_id": row["invocation_id"],
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
            last = self._reconcile(state)
            if not state["running"]:
                raise NotRunningError("No running backup to stop")
            if last and last["status"] in {"starting", "running"}:
                self._update(last["id"], status="stopping", message=None)
            try:
                self._command([self.sudo, "-n", self.systemctl, "stop", "--no-block", self.unit])
            except RuntimeError as exc:
                if last and last["status"] in {"starting", "running"}:
                    self._update(last["id"], status=last["status"], message=f"Stop request failed: {exc}")
                raise
            return {"status": "stopping"}

    def reconcile(self) -> None:
        """Refresh history independently of clients; never starts or stops a backup."""
        with self._lock:
            self._reconcile(self._unit_state())

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
