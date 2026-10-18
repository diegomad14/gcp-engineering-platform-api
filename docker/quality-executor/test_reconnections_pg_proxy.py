import subprocess
import unittest
from unittest import mock

import reconnections_pg_proxy as proxy


class ProxyContract(unittest.TestCase):
    def test_fixed_loopback_command_lifecycle_and_quality_failure(self):
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch.object(proxy.subprocess, "Popen", return_value=process) as start,
            mock.patch.object(proxy.socket, "create_connection"),
            mock.patch.object(
                proxy.subprocess, "run", return_value=mock.Mock(returncode=7)
            ) as quality,
        ):
            self.assertEqual(
                proxy.run(["--mode", "run", "--service", "cgm-reconnections-api"]), 7
            )
        self.assertEqual(
            start.call_args.args[0],
            [
                "socat",
                "TCP-LISTEN:5432,bind=127.0.0.1,fork,reuseaddr",
                "TCP:eng-platform-postgres:5432",
            ],
        )
        self.assertEqual(
            quality.call_args.args[0][1], "/opt/eng-platform/quality_executor.py"
        )
        process.terminate.assert_called_once()

    def test_dead_proxy_never_launches_quality(self):
        process = mock.Mock()
        process.poll.return_value = 1
        with (
            mock.patch.object(proxy.subprocess, "Popen", return_value=process),
            mock.patch.object(proxy.subprocess, "run") as quality,
        ):
            with self.assertRaises(RuntimeError):
                proxy.run([])
        quality.assert_not_called()
        process.terminate.assert_called_once()

    def test_not_ready_never_launches_quality_and_kills_stuck_proxy(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("socat", 5), 0]
        with (
            mock.patch.object(proxy.subprocess, "Popen", return_value=process),
            mock.patch.object(proxy.socket, "create_connection", side_effect=OSError),
            mock.patch.object(proxy.time, "sleep"),
            mock.patch.object(proxy.subprocess, "run") as quality,
        ):
            with self.assertRaises(RuntimeError):
                proxy.run([])
        quality.assert_not_called()
        process.kill.assert_called_once()
