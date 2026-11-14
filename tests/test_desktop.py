"""SSH lifecycle tests use stdlib fakes and never open a desktop window."""
import io
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

with patch.dict(sys.modules, {"webview": Mock()}):
    from desktop import app as desktop


class DesktopTests(unittest.TestCase):
    def test_stderr_is_drained_past_pipe_capacity_and_bounded(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stderr.write('x' * 300000 + 'TAIL'); sys.stderr.flush()"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        tunnel = desktop.Tunnel(proc)
        self.addCleanup(tunnel.close)
        self.assertEqual(proc.wait(timeout=5), 0)
        tunnel.close()
        self.assertTrue(tunnel.error_tail().endswith("TAIL"))
        self.assertLessEqual(len(tunnel.error_tail()), 1200)
        self.assertLessEqual(sum(map(len, tunnel._tail)), 65536)

    def test_ssh_destination_is_after_option_terminator(self):
        proc = Mock(stderr=io.BufferedReader(io.BytesIO(b"")))
        proc.poll.return_value = None
        with patch.object(desktop.shutil, "which", return_value="ssh.exe"), \
                patch.object(desktop.subprocess, "Popen", return_value=proc) as popen, \
                patch.object(desktop.socket, "create_connection", return_value=MagicMock()):
            tunnel = desktop.start_tunnel(dict(desktop.DEFAULTS), 12345)
        self.addCleanup(tunnel.close)
        args = popen.call_args.args[0]
        self.assertEqual(args[-2:], ["--", "pbs-tunnel@pbs"])
        self.assertIn("127.0.0.1:12345:127.0.0.1:8765", args)

    def test_startup_failure_includes_drained_stderr_tail(self):
        proc = Mock(stderr=io.BufferedReader(io.BytesIO(b"host key verification failed")))
        proc.poll.return_value = 255
        with patch.object(desktop.shutil, "which", return_value="ssh.exe"), \
                patch.object(desktop.subprocess, "Popen", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "host key verification failed"):
                desktop.start_tunnel(dict(desktop.DEFAULTS), 12345)
        self.assertTrue(proc.stderr.closed)

    def test_supervision_retries_same_port_with_backoff_and_closes(self):
        supervisor = desktop.TunnelSupervisor.__new__(desktop.TunnelSupervisor)
        supervisor.settings = dict(desktop.DEFAULTS)
        supervisor.port = 12345
        old, recovered = Mock(), Mock()
        old.proc.poll.return_value = 255
        supervisor.tunnel = old
        supervisor.stopping = Mock()
        waits = []

        def wait(timeout):
            waits.append(timeout)
            return len(waits) == 7

        supervisor.stopping.wait.side_effect = wait
        with patch.object(desktop, "start_tunnel", side_effect=[RuntimeError("down"), OSError("down"), recovered]) as start:
            supervisor._supervise()
        self.assertEqual(waits, [0.5, 1, 0.5, 2, 0.5, 4, 0.5])
        self.assertEqual(start.call_count, 3)
        for call in start.call_args_list:
            self.assertEqual(call.args, (supervisor.settings, 12345, supervisor.stopping))
        old.close.assert_called_once()
        recovered.close.assert_called_once()

    def test_invalid_hosts_and_users_are_rejected(self):
        for field in ("host", "user"):
            for value in ("", "-option", "a\nb", "a b", "a\tb", "a@b", "a\x00b", None, 123):
                with self.subTest(field=field, value=value):
                    settings = {**desktop.DEFAULTS, field: value}
                    with self.assertRaises(ValueError):
                        desktop.validate_config(settings)
        desktop.validate_config({**desktop.DEFAULTS, "user": "root"})

    def test_prompt_validates_before_saving(self):
        tkinter = Mock()
        tkinter.simpledialog.askstring.side_effect = ["-host", "user", ""]
        with patch.dict(sys.modules, {"tkinter": tkinter}), \
                patch.object(desktop, "CONFIG") as config:
            with self.assertRaises(ValueError):
                desktop.ask_config()
        config.write_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
