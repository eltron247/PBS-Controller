"""Tests do not require a running PBS or systemd."""
import subprocess
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from controller.core import BusyError, Controller, START_GRACE_SECONDS, iso


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.log = base / "job.log"
        self.log.write_bytes(b"previous run\n")
        self.now = 1700000000.123456
        self.clock = patch("controller.core.time.time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.controller = Controller(log_path=str(self.log), db_path=str(base / "history.sqlite3"))
        self.active = "inactive"
        self.result = "success"
        self.exit_code = 0
        self.started = False
        self.invocation_id = ""
        self.invocation_started = None
        self.finished = None
        self.main_code = 1
        self.auto_activate = True
        self.stop_error = None
        self.start_error = None
        self.calls = []

        def command(argv):
            self.calls.append(argv)
            if "show" in argv:
                output = (
                    "LoadState=loaded\n"
                    f"ActiveState={self.active}\n"
                    "SubState=dead\n"
                    f"Result={self.result}\n"
                    f"ExecMainStatus={self.exit_code}\n"
                    f"ExecMainCode={self.main_code}\n"
                    f"InvocationID={self.invocation_id}\n"
                    f"ExecMainStartTimestamp={self.stamp(self.invocation_started)}\n"
                    f"ExecMainExitTimestamp={self.stamp(self.finished)}\n"
                    f"ExecMainStartTimestampMonotonic={1000000 if self.started else 0}\n"
                    "MainPID=0\n"
                )
                return subprocess.CompletedProcess(argv, 0, stdout=output)
            if "start" in argv:
                if self.start_error:
                    raise RuntimeError(self.start_error)
                if self.auto_activate:
                    self.active = "active"
                    self.started = True
                    self.invocation_id = "controller-invocation"
                    self.invocation_started = self.now + 0.25
            if "stop" in argv:
                self.assertEqual(self.controller._last()["status"], "stopping")
                self.assertEqual(argv, ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", "--no-block", self.controller.unit])
                if self.stop_error:
                    raise RuntimeError(self.stop_error)
                self.active = "inactive"
                self.result = "success"
                self.main_code = 2
                self.exit_code = 15
                self.finished = self.now + 5.125
            return subprocess.CompletedProcess(argv, 0, stdout="")

        self.controller._command = command

    @staticmethod
    def stamp(ts):
        return "Tue " + iso(ts).replace("T", " ").replace("+00:00", " UTC") if ts else ""

    def test_start_reuse_and_history(self):
        result = self.controller.start()
        self.assertEqual(result["log_offset"], len("previous run\n"))
        self.assertEqual(self.controller.status()["state"], "running")
        with self.assertRaises(BusyError):
            self.controller.start()
        self.active = "inactive"
        self.finished = self.now + 5.125
        self.assertEqual(self.controller.status()["run"]["status"], "completed")
        self.assertEqual(self.controller.history()[0]["exit_code"], 0)

    def test_stop_marks_interrupted(self):
        self.controller.start()
        self.controller.status()
        self.controller.stop()
        run = self.controller.status()["run"]
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(run["signal_name"], "SIGTERM")
        self.assertIsNone(run["exit_code"])

    def test_stop_command_failure_restores_running_and_surfaces_error(self):
        self.controller.start()
        self.stop_error = "sudo denied"
        with self.assertRaisesRegex(RuntimeError, "sudo denied"):
            self.controller.stop()
        run = self.controller.status()["run"]
        self.assertEqual(run["status"], "running")
        self.assertIn("Stop request failed: sudo denied", run["message"])
        self.assertIsNone(run["finished_at"])

    def test_never_activated_fails_after_grace(self):
        self.auto_activate = False
        started = self.now
        self.controller.start()
        self.now += 5
        self.assertEqual(self.controller.status()["run"]["status"], "starting")
        self.now += START_GRACE_SECONDS
        run = self.controller.status()["run"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["message"], "Unit did not start")
        self.assertIsNone(run["exit_code"])
        self.assertEqual(run["finished_at"], iso(started + START_GRACE_SECONDS))

    def test_previous_success_not_attributed_to_new_start(self):
        self.auto_activate = False
        self.started = True
        self.invocation_id = "previous"
        self.invocation_started = self.now - 0.05
        self.finished = self.now - 0.01
        self.controller.start()
        self.now += 5
        run = self.controller.status()["run"]
        self.assertEqual(run["status"], "starting")
        self.assertIsNone(run["exit_code"])
        self.now += START_GRACE_SECONDS
        self.assertEqual(self.controller.status()["run"]["status"], "failed")

    def test_delayed_activation_within_grace_is_bound(self):
        self.auto_activate = False
        self.controller.start()
        self.now += 15
        self.active = "active"
        self.invocation_id = "delayed"
        self.invocation_started = self.now
        run = self.controller.status()["run"]
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["invocation_id"], "delayed")

    def test_signal_death_reports_signal_not_exit_code(self):
        for code in (2, 3):
            with self.subTest(main_code=code):
                self.controller.start()
                self.controller.status()
                self.active = "failed"
                self.result = "signal"
                self.main_code, self.exit_code = code, 15
                self.finished = self.now + 5
                status = self.controller.status()
                for item in (status["systemd"], status["run"], self.controller.history()[0]):
                    self.assertIsNone(item["exit_code"])
                    self.assertEqual(item["signal"], 15)
                    self.assertEqual(item["signal_name"], "SIGTERM")
                self.now += 10

    def test_finished_is_systemd_timestamp_even_when_poll_is_hours_late(self):
        self.controller.start()
        self.controller.status()
        self.active = "inactive"
        self.finished = self.now + 10.654321
        self.now += 7200
        run = self.controller.status()["run"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["finished_at"], iso(self.finished))

    def test_fast_run_finishes_without_observing_active(self):
        self.controller.start()
        self.active = "inactive"
        self.finished = self.now + 1
        self.assertEqual(self.controller.status()["run"]["status"], "completed")

    def test_manual_invocation_cannot_replace_tracked_outcome(self):
        self.controller.start()
        self.controller.status()
        self.now += 100
        self.active = "inactive"
        self.invocation_id = "manual"
        self.invocation_started = self.now
        self.finished = self.now + 10
        run = self.controller.status()["run"]
        self.assertEqual(run["status"], "failed")
        self.assertIn("outcome unavailable", run["message"])
        self.assertIsNone(run["exit_code"])
        self.assertIsNone(run["finished_at"])

    def test_cleared_invocation_id_uses_same_start_timestamp(self):
        self.controller.start()
        self.controller.status()
        self.active = "inactive"
        self.invocation_id = ""
        self.finished = self.now + 5
        self.assertEqual(self.controller.status()["run"]["status"], "completed")

    def test_changed_id_is_rejected_even_with_same_start_timestamp(self):
        self.controller.start()
        self.controller.status()
        self.active = "inactive"
        self.invocation_id = "different-invocation"
        self.finished = self.now + 5
        run = self.controller.status()["run"]
        self.assertEqual(run["status"], "failed")
        self.assertIsNone(run["exit_code"])
        self.assertIsNone(run["finished_at"])

    def test_invocation_id_is_saved_when_it_becomes_available(self):
        self.controller.start()
        self.invocation_id = ""
        self.controller.status()
        self.invocation_id = "now-available"
        self.assertEqual(self.controller.status()["run"]["invocation_id"], "now-available")

    def test_unobserved_start_does_not_adopt_later_manual_success(self):
        self.auto_activate = False
        self.controller.start()
        self.now += START_GRACE_SECONDS + 120
        self.invocation_started = self.now
        self.invocation_id = "manual"
        self.finished = self.now + 5
        run = self.controller.status()["run"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["message"], "Unit did not start")
        self.assertIsNone(run["exit_code"])

    def test_reconcile_without_status_or_history_poll(self):
        self.controller.start()
        self.controller.reconcile()
        self.active = "inactive"
        self.finished = self.now + 5
        self.controller.reconcile()
        self.assertEqual(self.controller._last()["status"], "completed")

    def test_start_command_failure_records_error(self):
        self.start_error = "start denied"
        with self.assertRaisesRegex(RuntimeError, "start denied"):
            self.controller.start()
        self.assertEqual(self.controller._last()["status"], "failed")
        self.assertEqual(self.controller._last()["message"], "start denied")

    def test_database_context_closes_and_rolls_back(self):
        with self.controller._db() as db:
            db.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.controller._db() as failed:
                failed.execute("INSERT INTO runs (started, status, log_offset) VALUES (1, 'starting', 0)")
                raise RuntimeError("rollback")
        self.assertIsNone(self.controller._last())
        with self.assertRaises(sqlite3.ProgrammingError):
            failed.execute("SELECT 1")

    def test_existing_history_is_migrated_in_place(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL NOT NULL, "
                       "finished REAL, status TEXT NOT NULL, log_offset INTEGER NOT NULL, "
                       "log_file_id TEXT, exit_code INTEGER, message TEXT)")
            db.execute("INSERT INTO runs (id, started, finished, status, log_offset, exit_code) "
                       "VALUES (7, 1, 2, 'completed', 42, 0)")
            db.commit()
        finally:
            db.close()
        for _ in range(2):
            migrated = Controller(db_path=str(path))
            row = migrated._last()
            self.assertEqual((row["id"], row["status"], row["log_offset"], row["exit_code"]), (7, "completed", 42, 0))
            self.assertIsNone(row["invocation_id"])

    def test_failed_exit_not_marked_completed(self):
        self.controller.start()
        self.controller.status()
        self.active = "failed"
        self.result = "exit-code"
        self.exit_code = 42
        self.finished = self.now + 5
        run = self.controller.status()["run"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["exit_code"], 42)

    def test_log_incremental_and_rotated(self):
        first = self.controller.log_chunk(0)
        self.assertEqual(first["text"], "previous run\n")
        self.assertEqual(first["offset"], len("previous run\n"))
        with self.log.open("ab") as f:
            f.write(b"next line\n")
        second = self.controller.log_chunk(first["offset"], first["file_id"])
        self.assertEqual(second["text"], "next line\n")
        self.log.unlink()
        self.log.write_bytes(b"fresh\n")
        rotated = self.controller.log_chunk(second["offset"], first["file_id"])
        self.assertTrue(rotated["reset"])
        self.assertEqual(rotated["text"], "fresh\n")

    def test_recent_log_only(self):
        self.controller.start()
        self.log.write_bytes(b"previous run\nnew run\n")
        data = self.controller.log_chunk(None)
        self.assertEqual(data["text"], "new run\n")


if __name__ == "__main__":
    unittest.main()
