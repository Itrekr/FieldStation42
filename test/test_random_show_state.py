import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_ffmpeg_stub = MagicMock()
_ffmpeg_stub.probe = MagicMock()
sys.modules.setdefault("ffmpeg", _ffmpeg_stub)

_moviepy_stub = MagicMock()
sys.modules.setdefault("moviepy", _moviepy_stub)
sys.modules.setdefault("moviepy.editor", _moviepy_stub)

from fs42.sequence import NamedSequence
from fs42.sequence_api import SequenceAPI
from fs42.sequence_io import SequenceIO
from fs42.station_manager import StationManager


def _configure_db(tmp_path):
    manager = StationManager()
    manager.server_conf["db_path"] = os.path.join(tmp_path, "fs42.db")
    manager.server_conf["normalize_titles"] = False


def _conf(content_dir="/content", station="TestTV"):
    return {
        "network_name": station,
        "content_dir": content_dir,
    }


def _put_sequence(station, sequence_name, tag_path, current_index=0, count=2, root="/content", parent_tag=None):
    SequenceIO().put_sequence(
        station,
        NamedSequence(
            station,
            sequence_name,
            tag_path,
            0,
            1,
            current_index,
            [
                os.path.join(root, tag_path, f"e{index:02}.mp4")
                for index in range(1, count + 1)
            ],
            True,
            "random_show",
            parent_tag,
        ),
    )


def _put_pool(station, sequence_names, show_tags, root="/content", current_index=0):
    for sequence_name in sequence_names:
        for show_tag in show_tags:
            _put_sequence(
                station,
                sequence_name,
                show_tag,
                current_index=current_index,
                root=root,
                parent_tag=show_tag.rsplit("/", 1)[0],
            )


def _group_state_rows(db_path):
    with sqlite3.connect(db_path) as connection:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT station, sequence_name, parent_tag, active_tag_path
            FROM sequence_group_state
            ORDER BY station, sequence_name
            """
        )
        return cursor.fetchall()


class TestRandomShowState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _configure_db(self.tmp.name)
        self.db_path = StationManager().server_conf["db_path"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_rebuild_clears_group_state(self):
        _put_pool("TestTV", ["lane1", "lane2"], ["pool/show_a", "pool/show_b"])
        sio = SequenceIO()
        sio.set_active_sequence("TestTV", "lane1", "pool", "pool/show_a")
        sio.set_active_sequence("TestTV", "lane2", "pool", "pool/show_b")

        sio.delete_sequences_for_station("TestTV")

        self.assertEqual(sio.get_all_sequences_for_station("TestTV"), [])
        self.assertEqual(_group_state_rows(self.db_path), [])

    def test_rebuild_does_not_affect_another_station_group_state(self):
        _put_pool("StationA", ["lane1"], ["pool/show_a"])
        _put_pool("StationB", ["lane1"], ["pool/show_b"])
        sio = SequenceIO()
        sio.set_active_sequence("StationA", "lane1", "pool", "pool/show_a")
        sio.set_active_sequence("StationB", "lane1", "pool", "pool/show_b")

        sio.delete_sequences_for_station("StationA")

        self.assertEqual(
            _group_state_rows(self.db_path),
            [("StationB", "lane1", "pool", "pool/show_b")],
        )

    def test_three_lanes_choose_three_different_shows(self):
        conf = _conf()
        _put_pool(
            "TestTV",
            ["lane1", "lane2", "lane3"],
            ["pool/show_a", "pool/show_b", "pool/show_c", "pool/show_d"],
        )

        selections = [
            SequenceAPI._get_active_child_sequence(conf, lane, "pool")
            for lane in ("lane1", "lane2", "lane3")
        ]

        self.assertEqual(len(set(selections)), 3)

    def test_duplication_allowed_when_unavoidable(self):
        conf = _conf()
        _put_pool(
            "TestTV",
            ["lane1", "lane2", "lane3"],
            ["pool/show_a", "pool/show_b"],
        )

        selections = [
            SequenceAPI._get_active_child_sequence(conf, lane, "pool")
            for lane in ("lane1", "lane2", "lane3")
        ]

        self.assertEqual(len(selections), 3)
        self.assertTrue(all(selections))
        self.assertLess(len(set(selections)), 3)

    def test_existing_active_child_persists(self):
        conf = _conf()
        _put_pool("TestTV", ["lane1"], ["pool/show_a", "pool/show_b"])
        SequenceIO().set_active_sequence("TestTV", "lane1", "pool", "pool/show_a")

        selections = [
            SequenceAPI._get_active_child_sequence(conf, "lane1", "pool")
            for _ in range(3)
        ]

        self.assertEqual(selections, ["pool/show_a", "pool/show_a", "pool/show_a"])

    def test_rollover_avoids_current_show(self):
        conf = _conf()
        _put_pool(
            "TestTV",
            ["lane1"],
            ["pool/show_a", "pool/show_b", "pool/show_c"],
            current_index=2,
        )
        SequenceIO().set_active_sequence("TestTV", "lane1", "pool", "pool/show_a")

        SequenceAPI.get_next_in_sequence(conf, "lane1", "pool", "random_show")

        self.assertIn(
            SequenceIO().get_active_sequence("TestTV", "lane1", "pool"),
            {"pool/show_b", "pool/show_c"},
        )

    def test_rollover_avoids_other_active_lanes(self):
        conf = _conf()
        _put_pool(
            "TestTV",
            ["lane1", "lane2"],
            ["pool/show_a", "pool/show_b", "pool/show_c"],
        )
        SequenceIO().update_current_index("TestTV", "lane1", "pool/show_a", 2)
        sio = SequenceIO()
        sio.set_active_sequence("TestTV", "lane1", "pool", "pool/show_a")
        sio.set_active_sequence("TestTV", "lane2", "pool", "pool/show_b")

        SequenceAPI.get_next_in_sequence(conf, "lane1", "pool", "random_show")

        self.assertEqual(
            sio.get_active_sequence("TestTV", "lane1", "pool"),
            "pool/show_c",
        )

    def test_nested_child_rollover_keeps_group_parent(self):
        conf = _conf()
        _put_sequence(
            "TestTV",
            "lane1",
            "pool/show_a/season_a",
            current_index=2,
            parent_tag="pool",
        )
        _put_sequence(
            "TestTV",
            "lane1",
            "pool/show_b",
            parent_tag="pool",
        )
        sio = SequenceIO()
        sio.set_active_sequence(
            "TestTV",
            "lane1",
            "pool",
            "pool/show_a/season_a",
        )

        SequenceAPI.get_next_in_sequence(conf, "lane1", "pool", "random_show")

        self.assertEqual(
            sio.get_active_sequence("TestTV", "lane1", "pool"),
            "pool/show_b",
        )
        self.assertIsNone(
            sio.get_active_sequence("TestTV", "lane1", "pool/show_a")
        )

    def test_completed_child_rollover_preserves_configured_parent(self):
        conf = _conf()
        _put_sequence(
            "TestTV",
            "lane1",
            "pool/show_a",
            current_index=2,
            parent_tag="pool",
        )
        _put_sequence(
            "TestTV",
            "lane1",
            "pool/show_b",
            parent_tag="pool",
        )
        sio = SequenceIO()
        sio.set_active_sequence("TestTV", "lane1", "pool", "pool/show_a")

        SequenceAPI.get_next_in_sequence(conf, "lane1", "pool", "random_show")

        self.assertEqual(
            sio.get_active_sequence("TestTV", "lane1", "pool"),
            "pool/show_b",
        )
        self.assertIsNone(
            sio.get_active_sequence("TestTV", "lane1", "pool/show_a")
        )

    def test_multi_season_show_remains_one_sequence(self):
        content_dir = os.path.join(self.tmp.name, "content")
        episode_paths = [
            os.path.join(content_dir, "pool", "Show A", "Season 01", "E01.mp4"),
            os.path.join(content_dir, "pool", "Show A", "Season 01", "E02.mp4"),
            os.path.join(content_dir, "pool", "Show A", "Season 02", "E01.mp4"),
            os.path.join(content_dir, "pool", "Show A", "Season 02", "E02.mp4"),
        ]
        for path in episode_paths:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8"):
                pass

        SequenceAPI._build_sequence(
            {
                "network_name": "TestTV",
                "content_dir": content_dir,
                "clip_shows": {},
            },
            "pool",
            {
                "sequence": "lane1",
                "sequence_strategy": "random_show",
            },
        )

        sio = SequenceIO()
        self.assertEqual(
            sio.get_child_sequences("TestTV", "lane1", "pool"),
            ["pool/Show A"],
        )
        self.assertEqual(
            [entry.fpath for entry in sio.get_sequence("TestTV", "lane1", "pool/Show A").episodes],
            sorted(episode_paths),
        )

    def test_decorated_and_nested_season_dirs_remain_one_sequence(self):
        content_dir = os.path.join(self.tmp.name, "content")
        episode_paths = [
            os.path.join(
                content_dir,
                "pool",
                "Show A",
                "Season 01 [1080p]",
                "Disc 1",
                "E01.mp4",
            ),
            os.path.join(
                content_dir,
                "pool",
                "Show A",
                "Season 02 (WEB-DL)",
                "E02.mp4",
            ),
        ]
        for path in episode_paths:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8"):
                pass

        SequenceAPI._build_sequence(
            {
                "network_name": "TestTV",
                "content_dir": content_dir,
                "clip_shows": {},
            },
            "pool",
            {
                "sequence": "lane1",
                "sequence_strategy": "random_show",
            },
        )

        sio = SequenceIO()
        self.assertEqual(
            sio.get_child_sequences("TestTV", "lane1", "pool"),
            ["pool/Show A"],
        )
        self.assertEqual(
            [entry.fpath for entry in sio.get_sequence("TestTV", "lane1", "pool/Show A").episodes],
            sorted(episode_paths),
        )

    def test_scan_sequences_caches_random_show_media_per_tag(self):
        content_dir = os.path.join(self.tmp.name, "content")
        show_a_dir = os.path.join(content_dir, "pool", "show_a")
        show_b_dir = os.path.join(content_dir, "pool", "show_b")
        os.makedirs(os.path.join(show_a_dir, "Season 1"))
        os.makedirs(os.path.join(show_a_dir, "Season 2"))
        os.makedirs(os.path.join(show_b_dir, "Season 1"))
        os.makedirs(os.path.join(show_b_dir, "Season 2"))

        show_files = {
            show_a_dir: [
                os.path.join(show_a_dir, "Season 1", "e01.mp4"),
                os.path.join(show_a_dir, "Season 2", "e02.mp4"),
            ],
            show_b_dir: [
                os.path.join(show_b_dir, "Season 1", "e01.mp4"),
                os.path.join(show_b_dir, "Season 2", "e02.mp4"),
            ],
        }

        def fake_rfind_media(path, media_filter="video"):
            return show_files.get(path, [])

        conf = {
            "network_name": "TestTV",
            "content_dir": content_dir,
            "clip_shows": {},
            "monday": {
                "06:00": {
                    "sequence": "morning",
                    "tags": "pool",
                    "sequence_strategy": "random_show",
                },
                "12:00": {
                    "sequence": "daytime",
                    "tags": "pool",
                    "sequence_strategy": "random_show",
                },
                "20:00": {
                    "sequence": "prime",
                    "tags": "pool",
                    "sequence_strategy": "random_show",
                },
            },
        }

        with patch("fs42.sequence_api.MediaProcessor._rfind_media", side_effect=fake_rfind_media) as rfind_media:
            SequenceAPI.scan_sequences(conf)

        self.assertEqual(
            [call.args[0] for call in rfind_media.call_args_list],
            [show_a_dir, show_b_dir],
        )

        sio = SequenceIO()
        self.assertEqual(
            sio.get_child_sequences("TestTV", "morning", "pool"),
            ["pool/show_a", "pool/show_b"],
        )
        self.assertEqual(
            sio.get_child_sequences("TestTV", "daytime", "pool"),
            ["pool/show_a", "pool/show_b"],
        )
        self.assertEqual(
            sio.get_child_sequences("TestTV", "prime", "pool"),
            ["pool/show_a", "pool/show_b"],
        )

        sio.update_current_index("TestTV", "morning", "pool/show_a", 1)
        self.assertEqual(
            sio.get_sequence("TestTV", "morning", "pool/show_a").current_index,
            1,
        )
        self.assertEqual(
            sio.get_sequence("TestTV", "daytime", "pool/show_a").current_index,
            0,
        )

    def test_symlink_canonical_identity_avoids_same_physical_show(self):
        content_dir = os.path.join(self.tmp.name, "content")
        media_dir = os.path.join(self.tmp.name, "media", "Show X")
        os.makedirs(media_dir)
        os.makedirs(os.path.join(content_dir, "pool_a"))
        os.makedirs(os.path.join(content_dir, "pool_b"))
        os.symlink(media_dir, os.path.join(content_dir, "pool_a", "show_x"))
        os.symlink(media_dir, os.path.join(content_dir, "pool_b", "show_x"))
        os.makedirs(os.path.join(content_dir, "pool_b", "show_y"))

        show_x_file = os.path.join(media_dir, "e01.mp4")
        show_y_file = os.path.join(content_dir, "pool_b", "show_y", "e01.mp4")
        with open(show_x_file, "w", encoding="utf-8"):
            pass
        with open(show_y_file, "w", encoding="utf-8"):
            pass

        sio = SequenceIO()
        sio.put_sequence(
            "TestTV",
            NamedSequence(
                "TestTV",
                "lane1",
                "pool_a/show_x",
                0,
                1,
                0,
                [os.path.join(content_dir, "pool_a", "show_x", "e01.mp4")],
                True,
                "random_show",
                "pool_a",
            ),
        )
        sio.put_sequence(
            "TestTV",
            NamedSequence(
                "TestTV",
                "lane2",
                "pool_b/show_x",
                0,
                1,
                0,
                [os.path.join(content_dir, "pool_b", "show_x", "e01.mp4")],
                True,
                "random_show",
                "pool_b",
            ),
        )
        sio.put_sequence(
            "TestTV",
            NamedSequence(
                "TestTV",
                "lane2",
                "pool_b/show_y",
                0,
                1,
                0,
                [show_y_file],
                True,
                "random_show",
                "pool_b",
            ),
        )
        sio.set_active_sequence("TestTV", "lane1", "pool_a", "pool_a/show_x")

        with patch("random.choice", side_effect=lambda choices: choices[0]):
            selection = SequenceAPI._get_active_child_sequence(
                _conf(content_dir=content_dir),
                "lane2",
                "pool_b",
            )

        self.assertEqual(selection, "pool_b/show_y")


if __name__ == "__main__":
    unittest.main()
