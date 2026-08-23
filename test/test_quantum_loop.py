import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import MagicMock, patch

_ffmpeg_stub = MagicMock()
_ffmpeg_stub.probe = MagicMock()
sys.modules.setdefault("ffmpeg", _ffmpeg_stub)
_moviepy_stub = MagicMock()
sys.modules.setdefault("moviepy", _moviepy_stub)
sys.modules.setdefault("moviepy.editor", _moviepy_stub)
_mpv_stub = MagicMock()
_mpv_stub.MPV = MagicMock()
sys.modules.setdefault("python_mpv_jsonipc", _mpv_stub)
_guide_stub = MagicMock()
_guide_stub.guide_channel_runner = MagicMock()
_guide_stub.GuideCommands = MagicMock()
sys.modules.setdefault("fs42.guide_tk", _guide_stub)

from field_player import is_quantum_loop
from fs42.catalog_entry import CatalogEntry
from fs42.guide_builder import GuideBuilder
from fs42.quantum_state import QuantumCursor, QuantumState
from fs42.station_player import PlayerOutcome, PlayerState, StationPlayer


class MemoryQuantumState:
    def __init__(self, cursor=None):
        self.cursor = cursor
        self.saves = []

    def load(self, network_name):
        return self.cursor

    def save(self, network_name, path, offset, order=None):
        self.cursor = QuantumCursor(path, float(offset), order)
        saved = (network_name, path, float(offset), order)
        self.saves.append(saved)
        return self.cursor


class FakeMPV:
    def __init__(self):
        self.time_pos = 0.0
        self.eof_reached = False
        self.idle_active = False


def clips():
    return [
        CatalogEntry("/content/episode-1.mkv", 3600, "content"),
        CatalogEntry("/content/episode-2.mkv", 3600, "content"),
    ]


def player(cursor=None, input_check=None):
    result = StationPlayer.__new__(StationPlayer)
    result._l = MagicMock()
    result.mpv = FakeMPV()
    result.input_check_fn = input_check or (lambda: None)
    result.quantum_state = MemoryQuantumState(cursor)
    result._quantum_active = None
    result.handle_runtime_command_outcome = MagicMock(return_value=False)
    return result


CONF = {
    "network_name": "Gilmost",
    "channel_number": 214,
    "network_type": "loop",
    "quantum": True,
    "shuffle_loop": False,
}


class TestQuantumState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tmp.name}/state.db"
        self.state = QuantumState(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_survives_new_reader(self):
        self.state.save("Gilmost", "/content/episode-2.mkv", 497)
        cursor = QuantumState(self.db_path).load("Gilmost")
        self.assertEqual((cursor.path, cursor.offset), ("/content/episode-2.mkv", 497))

    def test_invalid_state_recovers_safely(self):
        self.state.save("Gilmost", "/content/episode-1.mkv", 10)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("UPDATE quantum_state SET offset_seconds = -1 WHERE network_name = 'Gilmost'")
            connection.commit()
        self.assertIsNone(self.state.load("Gilmost"))
        self.assertIsNone(self.state.load("Gilmost"))

    def test_reset_is_isolated_by_network(self):
        self.state.save("Gilmost", "/content/episode-1.mkv", 10)
        self.state.save("Other", "/content/other.mkv", 20)
        self.state.reset("Gilmost")
        self.assertIsNone(self.state.load("Gilmost"))
        self.assertIsNotNone(self.state.load("Other"))


class TestQuantumPlayback(unittest.TestCase):
    def run_quantum(self, instance, content, conf=None):
        catalog = MagicMock()
        catalog.get_all_by_tag.return_value = content
        with patch("fs42.station_player.ShowCatalog", return_value=catalog):
            return instance.play_quantum(conf or CONF)

    def test_starts_at_first_item_and_saves_channel_change_position(self):
        change = PlayerOutcome(PlayerState.CHANNEL_CHANGE, "up")
        instance = player(input_check=lambda: change)
        instance.play_file = MagicMock(return_value=True)
        instance.mpv.time_pos = 753.25
        outcome = self.run_quantum(instance, clips())
        self.assertIs(outcome, change)
        self.assertEqual(instance.play_file.call_args.args[0], "/content/episode-1.mkv")
        self.assertEqual(instance.play_file.call_args.kwargs["offset_seconds"], 0.0)
        self.assertEqual(instance.quantum_state.saves[-1][1:3], ("/content/episode-1.mkv", 753.25))

    def test_resumes_saved_item_without_wall_clock_advancement(self):
        instance = player(QuantumCursor("/content/episode-2.mkv", 1200), lambda: PlayerOutcome(PlayerState.CHANNEL_CHANGE))
        instance.play_file = MagicMock(return_value=True)
        instance.mpv.time_pos = 1200
        self.run_quantum(instance, clips())
        self.assertEqual(instance.play_file.call_args.args[0], "/content/episode-2.mkv")
        self.assertEqual(instance.play_file.call_args.kwargs["offset_seconds"], 1200)

    def test_eof_advances_and_persists_next_item(self):
        change = PlayerOutcome(PlayerState.CHANNEL_CHANGE)
        polls = iter([None, change])
        instance = player(input_check=lambda: next(polls))
        played = []

        def play_file(path, **kwargs):
            played.append((path, kwargs["offset_seconds"]))
            instance.mpv.time_pos = kwargs["offset_seconds"]
            instance.mpv.eof_reached = len(played) == 1
            return True

        instance.play_file = play_file
        self.run_quantum(instance, clips())
        self.assertEqual(played, [("/content/episode-1.mkv", 0.0), ("/content/episode-2.mkv", 0.0)])
        self.assertIn(("Gilmost", "/content/episode-2.mkv", 0.0, None), instance.quantum_state.saves)

    def test_wraps_after_last_item(self):
        change = PlayerOutcome(PlayerState.CHANNEL_CHANGE)
        polls = iter([None, change])
        instance = player(QuantumCursor("/content/episode-2.mkv", 100), lambda: next(polls))
        played = []

        def play_file(path, **kwargs):
            played.append(path)
            instance.mpv.time_pos = kwargs["offset_seconds"]
            instance.mpv.eof_reached = len(played) == 1
            return True

        instance.play_file = play_file
        self.run_quantum(instance, clips())
        self.assertEqual(played, ["/content/episode-2.mkv", "/content/episode-1.mkv"])

    def test_near_eof_advances_before_playback(self):
        instance = player(QuantumCursor("/content/episode-1.mkv", 3599.5))
        content, index, offset = instance._resolve_quantum_cursor(CONF, clips())
        self.assertEqual((content[index].path, offset), ("/content/episode-2.mkv", 0.0))

    def test_missing_content_repairs_cursor(self):
        instance = player(QuantumCursor("/content/removed.mkv", 600))
        content, index, offset = instance._resolve_quantum_cursor(CONF, clips())
        self.assertEqual((index, offset), (0, 0.0))
        self.assertEqual(instance.quantum_state.saves[-1][1], content[0].path)

    def test_shuffle_order_survives_retune(self):
        content = clips()
        order = [content[1].path, content[0].path]
        instance = player(QuantumCursor(content[1].path, 25, order))
        resolved, index, offset = instance._resolve_quantum_cursor({**CONF, "shuffle_loop": True}, content)
        self.assertEqual([entry.path for entry in resolved], order)
        self.assertEqual((index, offset), (0, 25))

    def test_periodic_checkpoint_saves_active_position(self):
        change = PlayerOutcome(PlayerState.CHANNEL_CHANGE)
        polls = iter([None, change])
        instance = player(input_check=lambda: next(polls))
        instance.mpv.time_pos = 55.5
        instance.play_file = MagicMock(return_value=True)
        with patch("fs42.station_player.time.monotonic", side_effect=[0, 11, 12]):
            self.run_quantum(instance, clips())
        checkpoint = ("Gilmost", "/content/episode-1.mkv", 55.5, None)
        self.assertGreaterEqual(instance.quantum_state.saves.count(checkpoint), 2)

    def test_graceful_checkpoint_uses_current_mpv_position(self):
        instance = player()
        instance.mpv.time_pos = 88.25
        instance._quantum_active = {
            "network_name": "Gilmost",
            "path": "/content/episode-1.mkv",
            "offset": 80,
            "order": None,
        }
        instance._checkpoint_active_quantum()
        self.assertEqual(instance.quantum_state.saves[-1][1:3], ("/content/episode-1.mkv", 88.25))

    def test_shuffle_changes_order_only_after_full_cycle(self):
        content = clips()
        change = PlayerOutcome(PlayerState.CHANNEL_CHANGE)
        polls = iter([None, change])
        instance = player(QuantumCursor(content[1].path, 100, [entry.path for entry in content]), lambda: next(polls))
        played = []

        def play_file(path, **kwargs):
            played.append(path)
            instance.mpv.time_pos = kwargs["offset_seconds"]
            instance.mpv.eof_reached = len(played) == 1
            return True

        instance.play_file = play_file
        with patch("fs42.station_player.random.shuffle", side_effect=lambda order: order.reverse()) as shuffle:
            self.run_quantum(instance, content, {**CONF, "shuffle_loop": True})
        shuffle.assert_called_once()
        self.assertEqual(played, [content[1].path, content[1].path])

    def test_empty_content_fails_cleanly(self):
        outcome = self.run_quantum(player(), [])
        self.assertEqual(outcome.status, PlayerState.FAILED)


class TestQuantumRouting(unittest.TestCase):
    def test_only_explicit_quantum_loops_use_quantum_routing(self):
        self.assertFalse(is_quantum_loop({"network_type": "loop"}))
        self.assertFalse(is_quantum_loop({"network_type": "loop", "quantum": False}))
        self.assertFalse(is_quantum_loop({"network_type": "standard", "quantum": True}))
        self.assertTrue(is_quantum_loop({"network_type": "loop", "quantum": True}))

    def test_guide_uses_generic_quantum_programming_listing(self):
        manager = MagicMock()
        manager.stations = [{
            "network_name": "Gilmost",
            "network_long_name": "Gilmost",
            "channel_number": 214,
            "network_type": "loop",
            "quantum": True,
            "hidden": False,
            "_has_schedule": True,
        }]
        manager.server_conf = {"time_format": "%H:%M"}
        with patch("fs42.guide_builder.StationManager", return_value=manager):
            view = GuideBuilder().build_view()
        self.assertEqual(view["rows"][0][0].title, "Gilmost — Quantum programming")


if __name__ == "__main__":
    unittest.main()
