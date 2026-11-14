"""REST authorization and start behavior through ASGI."""
import os
import subprocess
import tempfile
import unittest
import time
import threading
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("PBS_CONTROLLER_TOKEN", "a" * 48)
os.environ.setdefault("PBS_CONTROLLER_DB", str(Path(tempfile.gettempdir()) / "pbs-controller-import-test.sqlite3"))

from fastapi.testclient import TestClient
from controller.api import create_app, SECURITY_HEADERS
from controller.core import Controller, iso


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        log = base / "log"
        log.write_bytes(b"abc\n")
        controller = Controller(log_path=str(log), db_path=str(base / "runs.sqlite3"))
        self.controller = controller
        self.active = {"state": "inactive", "started": False}
        self.started_at = time.time()

        def command(argv):
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=(
                    "LoadState=loaded\n"
                    "ActiveState=" + self.active["state"] + "\n"
                    "SubState=dead\nResult=success\nExecMainStatus=0\nExecMainCode=1\n"
                    "InvocationID=api-test-run\n"
                    "ExecMainStartTimestamp=" + ("Tue " + iso(self.started_at).replace("T", " ").replace("+00:00", " UTC") if self.active["started"] else "") + "\n"
                    "ExecMainStartTimestampMonotonic=" + ("1000000" if self.active["started"] else "0") + "\nMainPID=0\n"
                ))
            if "start" in argv:
                self.active["state"] = "active"
                self.active["started"] = True
                self.started_at = time.time()
            return subprocess.CompletedProcess(argv, 0, stdout="")

        controller._command = command
        self.app = TestClient(create_app(controller=controller, token="S" * 48))
        self.addCleanup(self.app.close)
        self.auth = {"Authorization": "Bearer " + "S" * 48}

    def test_unauthorized(self):
        self.assertEqual(self.app.get("/api/status").status_code, 401)
        self.assertEqual(self.app.post("/api/start").status_code, 401)
        self.assertEqual(self.app.get("/").status_code, 200)

    def test_non_ascii_bearer_is_unauthorized(self):
        # httpx text headers are ASCII-only; raw header bytes exercise the ASGI path.
        response = self.app.get("/api/status", headers=[(b"authorization", "Bearer café 🔒".encode("utf-8"))])
        self.assertEqual(response.status_code, 401)

    def test_security_headers_on_ui_and_api(self):
        for path, headers in (("/", {}), ("/api/status", self.auth), ("/api/status", {}), ("/assets/app.js", {})):
            with self.subTest(path=path, authenticated=bool(headers)):
                response = self.app.get(path, headers=headers)
                for name, value in SECURITY_HEADERS.items():
                    self.assertEqual(response.headers[name], value)

    def test_stop_failure_returns_503_and_keeps_running(self):
        self.app.post("/api/start", headers=self.auth)
        original = self.controller._command

        def fail_stop(argv):
            if "stop" in argv:
                self.assertEqual(self.controller._last()["status"], "stopping")
                raise RuntimeError("sudo denied")
            return original(argv)

        self.controller._command = fail_stop
        response = self.app.post("/api/stop", headers=self.auth, json={"confirm": True})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "sudo denied")
        self.assertEqual(self.controller._last()["status"], "running")
        self.assertIn("sudo denied", self.controller._last()["message"])

    def test_lifespan_reconciles_without_client_polling_and_stops_worker(self):
        engine = Mock(spec=Controller)
        refreshed = threading.Event()
        engine.reconcile.side_effect = refreshed.set
        application = create_app(controller=engine, token="S" * 48)
        engine.reconcile.assert_not_called()
        with TestClient(application):
            self.assertTrue(refreshed.wait(timeout=2))
            worker = next(t for t in threading.enumerate() if t.name == "history-reconcile")
        self.assertFalse(worker.is_alive())

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
