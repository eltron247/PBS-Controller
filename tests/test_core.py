"""Tests do not require a running PBS or systemd."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from controller.core import BusyError, Controller


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.log = base / "job.log"
        self.log.write_text("previous run\n", encoding="utf-8")
        self.controller = Controller(log_path=str(self.log), db_path=str(base / "history.sqlite3"))
        self.active = "inactive"
        self.result = "success"
        self.exit_code = 0
        self.started = False
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
                    "ExecMainCode=1\n"
                    f"ExecMainStartTimestampMonotonic={1000000 if self.started else 0}\n"
                    "MainPID=0\n"
                )
                return subprocess.CompletedProcess(argv, 0, stdout=output)
            if "start" in argv:
                self.active = "active"
                self.started = True
            if "stop" in argv:
                self.active = "inactive"
                self.result = "success"
            return subprocess.CompletedProcess(argv, 0, stdout="")

        self.controller._command = command

    def test_start_reuse_and_history(self):
        result = self.controller.start()
        self.assertEqual(result["log_offset"], len("previous run\n"))
        self.assertEqual(self.controller.status()["state"], "running")
        with self.assertRaises(BusyError):
            self.controller.start()
        self.active = "inactive"
        self.assertEqual(self.controller.status()["run"]["status"], "completed")
        self.assertEqual(self.controller.history()[0]["exit_code"], 0)

    def test_stop_marks_interrupted(self):
        self.controller.start()
        self.controller.status()
        self.controller.stop()
        self.assertEqual(self.controller.status()["run"]["status"], "interrupted")

    def test_failed_exit_not_marked_completed(self):
        self.controller.start()
        self.controller.status()
        self.active = "failed"
        self.result = "exit-code"
        self.exit_code = 42
        run = self.controller.status()["run"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["exit_code"], 42)

    def test_log_incremental_and_rotated(self):
        first = self.controller.log_chunk(0)
        self.assertEqual(first["text"], "previous run\n")
        self.assertEqual(first["offset"], len("previous run\n"))
        with self.log.open("a", encoding="utf-8") as f:
            f.write("next line\n")
        second = self.controller.log_chunk(first["offset"], first["file_id"])
        self.assertEqual(second["text"], "next line\n")
        self.log.unlink()
        self.log.write_text("fresh\n", encoding="utf-8")
        rotated = self.controller.log_chunk(second["offset"], first["file_id"])
        self.assertTrue(rotated["reset"])
        self.assertEqual(rotated["text"], "fresh\n")

    def test_recent_log_only(self):
        self.controller.start()
        self.log.write_text("previous run\nnew run\n", encoding="utf-8")
        data = self.controller.log_chunk(None)
        self.assertEqual(data["text"], "new run\n")


if __name__ == "__main__":
    unittest.main()
