import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fs42 import player_runtime


class TestPlayerRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pid_path = Path(self.tmp.name) / "player.pid"
        self.path_patch = patch.object(player_runtime, "PLAYER_PID_PATH", self.pid_path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tmp.cleanup()

    def test_register_and_unregister_live_player(self):
        player_runtime.register_player()

        self.assertEqual(player_runtime.running_player_pid(), os.getpid())

        player_runtime.unregister_player()
        self.assertFalse(self.pid_path.exists())

    def test_stale_marker_is_removed(self):
        self.pid_path.write_text("999999999", encoding="utf-8")

        self.assertIsNone(player_runtime.running_player_pid())
        self.assertFalse(self.pid_path.exists())

    def test_old_player_does_not_remove_new_marker(self):
        self.pid_path.write_text("12345", encoding="utf-8")

        player_runtime.unregister_player(pid=54321)

        self.assertEqual(self.pid_path.read_text(encoding="utf-8"), "12345")


if __name__ == "__main__":
    unittest.main()
