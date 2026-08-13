import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

_ffmpeg_stub = MagicMock()
_ffmpeg_stub.probe = MagicMock()
sys.modules.setdefault("ffmpeg", _ffmpeg_stub)

_moviepy_stub = MagicMock()
sys.modules.setdefault("moviepy", _moviepy_stub)
sys.modules.setdefault("moviepy.editor", _moviepy_stub)

from fs42.sequence_api import SequenceAPI
from fs42.sequence_io import SequenceIO
from fs42.station_manager import StationManager


def _configure_db(tmp_path):
    manager = StationManager()
    manager.server_conf["db_path"] = os.path.join(tmp_path, "fs42.db")
    manager.server_conf["normalize_titles"] = False


def _conf(content_dir, station="TestTV"):
    return {
        "network_name": station,
        "content_dir": content_dir,
        "clip_shows": {},
        "monday": {},
        "tuesday": {},
        "wednesday": {},
        "thursday": {},
        "friday": {},
        "saturday": {},
        "sunday": {},
    }


def _touch(root, tag, names):
    tag_dir = os.path.join(root, tag)
    os.makedirs(tag_dir, exist_ok=True)
    paths = []
    for name in names:
        path = os.path.join(tag_dir, f"{name}.mp4")
        with open(path, "wb") as handle:
            handle.write(b"")
        paths.append(path)
    return paths


def _scan(conf, tag, sequence="rotation"):
    SequenceAPI._scan_sequence_slot(
        conf,
        {
            "tags": tag,
            "sequence": sequence,
            "sequence_strategy": "shuffle",
        },
    )


def _next_path(conf, tag, sequence="rotation"):
    return SequenceAPI.get_next_in_sequence(
        conf,
        sequence,
        tag,
        "shuffle",
    ).fpath


class TestShuffleSequence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _configure_db(self.tmp.name)
        self.content_dir = os.path.join(self.tmp.name, "content")

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_item_once_and_reshuffle_without_boundary_duplicate(self):
        conf = _conf(self.content_dir)
        _touch(self.content_dir, "movies", ["A", "B", "C", "D"])
        _scan(conf, "movies")

        results = [_next_path(conf, "movies") for _ in range(8)]
        first = results[:4]
        second = results[4:]

        self.assertEqual(set(first), set(second))
        self.assertEqual(len(set(first)), 4)
        self.assertEqual(len(set(second)), 4)
        self.assertNotEqual(results[3], results[4])

    def test_persistence_continues_existing_bag(self):
        conf = _conf(self.content_dir)
        _touch(self.content_dir, "movies", ["A", "B", "C", "D"])
        _scan(conf, "movies")

        seq_before = SequenceIO().get_sequence("TestTV", "rotation", "movies")
        expected_third = seq_before.episodes[2].fpath
        first_two = [_next_path(conf, "movies") for _ in range(2)]

        self.assertEqual(first_two, [entry.fpath for entry in seq_before.episodes[:2]])
        self.assertEqual(_next_path(conf, "movies"), expected_third)

    def test_tags_and_stations_are_independent(self):
        conf = _conf(self.content_dir, "TestTV")
        other_conf = _conf(self.content_dir, "OtherTV")
        _touch(self.content_dir, "summer_heat", ["A", "B", "C"])
        _touch(self.content_dir, "summer_adventure", ["A", "B", "C"])
        _scan(conf, "summer_heat")
        _scan(conf, "summer_adventure")
        _scan(other_conf, "summer_heat")

        _next_path(conf, "summer_heat")
        _next_path(other_conf, "summer_heat")

        heat = SequenceIO().get_sequence("TestTV", "rotation", "summer_heat")
        adventure = SequenceIO().get_sequence("TestTV", "rotation", "summer_adventure")
        other_heat = SequenceIO().get_sequence("OtherTV", "rotation", "summer_heat")

        self.assertEqual(heat.current_index, 1)
        self.assertEqual(adventure.current_index, 0)
        self.assertEqual(other_heat.current_index, 1)
        self.assertNotEqual(heat.shuffle_seed, other_heat.shuffle_seed)

    def test_add_content_mid_cycle_enters_unplayed_bag_without_repeating_played(self):
        conf = _conf(self.content_dir)
        _touch(self.content_dir, "movies", ["A", "B", "C"])
        _scan(conf, "movies")

        played = _next_path(conf, "movies")
        _touch(self.content_dir, "movies", ["D"])
        _scan(conf, "movies")

        remainder = [_next_path(conf, "movies") for _ in range(3)]
        self.assertNotIn(played, remainder)
        self.assertIn(os.path.join(self.content_dir, "movies", "D.mp4"), remainder)

    def test_remove_unplayed_content_mid_cycle_continues_without_reset(self):
        conf = _conf(self.content_dir)
        paths = _touch(self.content_dir, "movies", ["A", "B", "C"])
        _scan(conf, "movies")

        played = _next_path(conf, "movies")
        seq = SequenceIO().get_sequence("TestTV", "rotation", "movies")
        unplayed = next(path for path in paths if path != played and path in [e.fpath for e in seq.episodes[1:]])
        os.remove(unplayed)
        _scan(conf, "movies")

        remainder = [_next_path(conf, "movies") for _ in range(1)]
        self.assertNotIn(played, remainder)
        self.assertNotIn(unplayed, remainder)

    def test_snapshot_rewind_restores_exact_shuffle_state(self):
        conf = _conf(self.content_dir)
        _touch(self.content_dir, "movies", ["A", "B", "C", "D"])
        _scan(conf, "movies")

        generated = [
            SequenceAPI.get_next_in_sequence_with_key(conf, "rotation", "movies", "shuffle")
            for _ in range(6)
        ]
        first_entry, first_key = generated[0]

        self.assertNotEqual(
            SequenceIO().get_sequence("TestTV", "rotation", "movies").current_index,
            first_key["shuffle_state"]["position"],
        )

        SequenceAPI.reset_by_sequence_key(conf, first_key, first_entry.fpath)
        regenerated = [_next_path(conf, "movies") for _ in range(6)]

        self.assertEqual(regenerated, [entry.fpath for entry, _key in generated])


if __name__ == "__main__":
    unittest.main()
