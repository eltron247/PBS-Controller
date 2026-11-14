"""REST authorization and start behavior through ASGI."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("PBS_CONTROLLER_TOKEN", "a" * 48)
os.environ.setdefault("PBS_CONTROLLER_DB", str(Path(tempfile.gettempdir()) / "pbs-controller-import-test.sqlite3"))

from fastapi.testclient import TestClient
from controller.api import create_app
from controller.core import Controller


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        log = base / "log"
        log.write_text("abc\n", encoding="utf-8")
        controller = Controller(log_path=str(log), db_path=str(base / "runs.sqlite3"))
        self.active = {"state": "inactive", "started": False}

        def command(argv):
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=(
                    "LoadState=loaded\n"
                    "ActiveState=" + self.active["state"] + "\n"
                    "SubState=dead\nResult=success\nExecMainStatus=0\nExecMainCode=1\n"
                    "ExecMainStartTimestampMonotonic=" + ("1000000" if self.active["started"] else "0") + "\nMainPID=0\n"
                ))
            if "start" in argv:
                self.active["state"] = "active"
                self.active["started"] = True
            return subprocess.CompletedProcess(argv, 0, stdout="")

        controller._command = command
        self.app = TestClient(create_app(controller=controller, token="S" * 48))
        self.auth = {"Authorization": "Bearer " + "S" * 48}

    def test_unauthorized(self):
        self.assertEqual(self.app.get("/api/status").status_code, 401)
        self.assertEqual(self.app.post("/api/start").status_code, 401)
        self.assertEqual(self.app.get("/").status_code, 200)

    def test_start_log_history_stop_confirmation(self):
        started = self.app.post("/api/start", headers=self.auth)
        self.assertEqual(started.status_code, 200)
        self.assertEqual(self.app.post("/api/start", headers=self.auth).status_code, 409)
        log = self.app.get("/api/log", headers=self.auth)
        self.assertEqual(log.status_code, 200)
        self.assertEqual(log.json()["text"], "")
        self.assertEqual(self.app.post("/api/stop", headers=self.auth, json={}).status_code, 400)
        self.assertEqual(self.app.get("/api/history", headers=self.auth).status_code, 200)


if __name__ == "__main__":
    unittest.main()
