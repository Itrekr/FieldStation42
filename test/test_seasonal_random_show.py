import datetime
import os
import sqlite3
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

from fs42.catalog_entry import CatalogEntry
from fs42.seasonal_run import (
    SeasonalRunCompleted,
    SeasonalRunPlanner,
    SeasonalRunUnavailable,
    meteorological_season_window,
)
from fs42.sequence import NamedSequence
from fs42.sequence_api import SequenceAPI
from fs42.sequence_io import SequenceIO
from fs42.station_manager import StationManager
from fs42.title_parser import TitleParser


def _configure_db(tmp_path):
    manager = StationManager()
    manager.server_conf["db_path"] = os.path.join(tmp_path, "fs42.db")
    manager.server_conf["normalize_titles"] = False


def _conf(root="/content", station="TestTV"):
    return {
        "network_name": station,
        "content_dir": root,
        "schedule_increment": 30,
        "clip_shows": {},
        "break_strategy": "standard",
    }


def _slot(**seasonal_overrides):
    seasonal = {
        "appointment_minutes": 120,
        "interval_days": 7,
        "overflow_days": 14,
        "fallback_tags": ["seasonal_movies/fallback"],
    }
    seasonal.update(seasonal_overrides)
    return {
        "tags": "summer",
        "sequence": "monday_early",
        "sequence_strategy": "seasonal_random_show",
        "seasonal_run": seasonal,
        "hard_end": "20:30",
        "airing_id": "weekly_first",
    }


def _episode(root, tag, show, season, episode):
    return os.path.join(root, tag, f"{show} - S{season:02}E{episode:02}.mkv")


def _put_show(station, sequence_name, tag, files, current_index=0, parent_tag=None):
    SequenceIO().put_sequence(
        station,
        NamedSequence(
            station,
            sequence_name,
            tag,
            0,
            1,
            current_index,
            files,
            True,
            "seasonal_random_show",
            parent_tag or tag.rsplit("/", 1)[0],
        ),
    )


class _Catalog:
    def __init__(self, durations):
        self.durations = durations

    def entry_by_fpath(self, fpath):
        return CatalogEntry(fpath, self.durations[fpath], "tag")


class TestSeasonalRandomShow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _configure_db(self.tmp.name)
        self.root = os.path.join(self.tmp.name, "content")

    def tearDown(self):
        self.tmp.cleanup()

    def test_episode_ref_parses_show_season_episode(self):
        ref = TitleParser.parse_episode_ref("/media/Lost - S01E02.mkv")
        self.assertEqual((ref.show, ref.season, ref.episode), ("Lost", 1, 2))

    def test_episode_ref_allows_show_titles_with_hyphens(self):
        ref = TitleParser.parse_episode_ref("/media/The X-Files - S05E12.mp4")
        self.assertEqual((ref.show, ref.season, ref.episode), ("The X-Files", 5, 12))

    def test_nested_directories_do_not_affect_filename_parsing(self):
        ref = TitleParser.parse_episode_ref("/x/y/Lost/Disc 1/Lost - s01e01.mkv")
        self.assertEqual((ref.show, ref.season, ref.episode), ("Lost", 1, 1))

    def test_invalid_filename_makes_run_ineligible(self):
        files = [
            _episode(self.root, "summer/Lost", "Lost", 1, 1),
            os.path.join(self.root, "summer/Lost", "episode3.mkv"),
        ]
        _put_show("TestTV", "monday_early", "summer/Lost", files, parent_tag="summer")
        seq = SequenceIO().get_sequence("TestTV", "monday_early", "summer/Lost")
        self.assertIsNone(SeasonalRunPlanner.next_run(seq))

    def test_s01_and_s02_are_separate_runs(self):
        files = [
            _episode(self.root, "summer/Lost", "Lost", 1, 1),
            _episode(self.root, "summer/Lost", "Lost", 1, 2),
            _episode(self.root, "summer/Lost", "Lost", 2, 1),
        ]
        _put_show("TestTV", "monday_early", "summer/Lost", files, parent_tag="summer")
        seq = SequenceIO().get_sequence("TestTV", "monday_early", "summer/Lost")
        run = SeasonalRunPlanner.next_run(seq)
        self.assertEqual((run.season, run.start_index, run.end_index), (1, 0, 2))

        SequenceIO().update_current_index("TestTV", "monday_early", "summer/Lost", 2)
        seq = SequenceIO().get_sequence("TestTV", "monday_early", "summer/Lost")
        run = SeasonalRunPlanner.next_run(seq)
        self.assertEqual((run.season, run.start_index, run.end_index), (2, 2, 3))

    def test_halfway_through_season_uses_remainder(self):
        files = [
            _episode(self.root, "summer/Lost", "Lost", 3, episode)
            for episode in range(1, 5)
        ]
        _put_show("TestTV", "monday_early", "summer/Lost", files, current_index=2, parent_tag="summer")
        seq = SequenceIO().get_sequence("TestTV", "monday_early", "summer/Lost")
        run = SeasonalRunPlanner.next_run(seq)
        self.assertEqual((run.season, run.start_index, run.end_index), (3, 2, 4))

    def test_rounding_and_fragmented_packing_projection(self):
        files = [
            _episode(self.root, "summer/Frag", "Frag", 1, 1),
            _episode(self.root, "summer/Frag", "Frag", 1, 2),
            _episode(self.root, "summer/Frag", "Frag", 1, 3),
        ]
        _put_show("TestTV", "monday_early", "summer/Frag", files, parent_tag="summer")
        seq = SequenceIO().get_sequence("TestTV", "monday_early", "summer/Frag")
        run = SeasonalRunPlanner.next_run(seq)
        durations = {
            files[0]: 44 * 60,
            files[1]: 89 * 60,
            files[2]: 44 * 60,
        }
        projection = SeasonalRunPlanner.project_run(
            run,
            datetime.datetime(2026, 8, 17, 18, 30),
            _slot(),
            _conf(self.root),
            _Catalog(durations),
        )
        self.assertEqual(projection.appointment_count, 3)
        self.assertEqual(projection.scheduled_seconds, (60 + 90 + 60) * 60)

    def test_episode_longer_than_appointment_is_ineligible(self):
        files = [_episode(self.root, "summer/Epic", "Epic", 1, 1)]
        _put_show("TestTV", "monday_early", "summer/Epic", files, parent_tag="summer")
        run = SeasonalRunPlanner.next_run(
            SequenceIO().get_sequence("TestTV", "monday_early", "summer/Epic")
        )
        projection = SeasonalRunPlanner.project_run(
            run,
            datetime.datetime(2026, 8, 17, 18, 30),
            _slot(),
            _conf(self.root),
            _Catalog({files[0]: 121 * 60}),
        )
        self.assertFalse(projection.eligible)

    def test_meteorological_season_boundaries(self):
        self.assertEqual(
            meteorological_season_window(datetime.datetime(2026, 8, 17))[2],
            datetime.datetime(2026, 9, 1),
        )
        self.assertEqual(
            meteorological_season_window(datetime.datetime(2026, 10, 1))[2],
            datetime.datetime(2026, 12, 1),
        )
        self.assertEqual(
            meteorological_season_window(datetime.datetime(2026, 12, 15))[2],
            datetime.datetime(2027, 3, 1),
        )
        self.assertEqual(
            meteorological_season_window(datetime.datetime(2028, 2, 29))[2],
            datetime.datetime(2028, 3, 1),
        )

    def test_exact_deadline_is_eligible_but_one_minute_beyond_is_not(self):
        files = [
            _episode(self.root, "summer/Short", "Short", 1, episode)
            for episode in range(1, 9)
        ]
        _put_show("TestTV", "monday_early", "summer/Short", files, parent_tag="summer")
        run = SeasonalRunPlanner.next_run(
            SequenceIO().get_sequence("TestTV", "monday_early", "summer/Short")
        )
        conf = _conf(self.root)
        catalog = _Catalog({path: 44 * 60 for path in files})
        projection = SeasonalRunPlanner.project_run(
            run,
            datetime.datetime(2026, 8, 24, 22, 0),
            _slot(),
            conf,
            catalog,
        )
        self.assertEqual(projection.projected_finish, projection.deadline)
        self.assertTrue(projection.eligible)

        projection = SeasonalRunPlanner.project_run(
            run,
            datetime.datetime(2026, 8, 24, 22, 1),
            _slot(),
            conf,
            catalog,
        )
        self.assertFalse(projection.eligible)

    def test_august_bootstrap_filters_long_show_and_selects_short_show(self):
        long_files = [
            _episode(self.root, "summer/LongShow", "LongShow", 1, episode)
            for episode in range(1, 25)
        ]
        short_files = [
            _episode(self.root, "summer/ShortShow", "ShortShow", 1, episode)
            for episode in range(1, 9)
        ]
        _put_show("TestTV", "monday_early", "summer/LongShow", long_files, parent_tag="summer")
        _put_show("TestTV", "monday_early", "summer/ShortShow", short_files, parent_tag="summer")

        entry, key = SequenceAPI.get_next_in_sequence_with_key(
            _conf(self.root),
            "monday_early",
            "summer",
            "seasonal_random_show",
            current_mark=datetime.datetime(2026, 8, 17, 18, 30),
            slot_config=_slot(),
            catalog=_Catalog({path: 44 * 60 for path in long_files + short_files}),
        )
        self.assertIn("ShortShow", entry.fpath)
        self.assertEqual(key["tag_path"], "summer/ShortShow")
        state = SequenceIO().get_seasonal_run_state("TestTV", "monday_early")
        self.assertEqual(state["active_tag_path"], "summer/ShortShow")

    def test_ineligible_candidate_does_not_consume_random_bag(self):
        long_files = [
            _episode(self.root, "summer/LongShow", "LongShow", 1, episode)
            for episode in range(1, 25)
        ]
        short_files = [_episode(self.root, "summer/ShortShow", "ShortShow", 1, 1)]
        _put_show("TestTV", "monday_early", "summer/LongShow", long_files, parent_tag="summer")
        _put_show("TestTV", "monday_early", "summer/ShortShow", short_files, parent_tag="summer")
        sio = SequenceIO()
        sio.set_sequence_group_shuffle_state(
            "TestTV",
            "monday_early",
            "summer",
            "summer/LongShow",
            "seed",
            0,
            ["summer/LongShow", "summer/ShortShow"],
            0,
        )
        SequenceAPI.get_next_in_sequence_with_key(
            _conf(self.root),
            "monday_early",
            "summer",
            "seasonal_random_show",
            current_mark=datetime.datetime(2026, 8, 17, 18, 30),
            slot_config=_slot(),
            catalog=_Catalog({path: 44 * 60 for path in long_files + short_files}),
        )
        state = sio.get_sequence_group_shuffle_state("TestTV", "monday_early", "summer")
        self.assertIn("summer/LongShow", state["order"][state["position"]:])

    def test_active_run_persists_across_parent_tag_change_and_then_uses_new_pool(self):
        summer_files = [
            _episode(self.root, "summer/ShortShow", "ShortShow", 1, 1),
            _episode(self.root, "summer/ShortShow", "ShortShow", 1, 2),
            _episode(self.root, "summer/ShortShow", "ShortShow", 2, 1),
        ]
        autumn_files = [_episode(self.root, "autumn/FallShow", "FallShow", 1, 1)]
        _put_show("TestTV", "monday_early", "summer/ShortShow", summer_files, parent_tag="summer")
        _put_show("TestTV", "monday_early", "autumn/FallShow", autumn_files, parent_tag="autumn")
        SequenceIO().set_seasonal_run_state(
            "TestTV",
            "monday_early",
            "summer/ShortShow",
            "summer",
            1,
        )

        entry, key = SequenceAPI.get_next_in_sequence_with_key(
            _conf(self.root),
            "monday_early",
            "autumn",
            "seasonal_random_show",
            current_mark=datetime.datetime(2026, 9, 7, 18, 30),
            slot_config=dict(_slot(), tags="autumn"),
            catalog=_Catalog({path: 44 * 60 for path in summer_files + autumn_files}),
        )
        self.assertIn("summer/ShortShow", entry.fpath)
        self.assertEqual(key["tag_path"], "summer/ShortShow")

        entry, key = SequenceAPI.get_next_in_sequence_with_key(
            _conf(self.root),
            "monday_early",
            "autumn",
            "seasonal_random_show",
            current_mark=datetime.datetime(2026, 9, 14, 18, 30),
            slot_config=dict(_slot(), tags="autumn"),
            catalog=_Catalog({path: 44 * 60 for path in summer_files + autumn_files}),
        )
        self.assertIn("summer/ShortShow", entry.fpath)
        self.assertTrue(key.get("seasonal_run_completed_after_entry"))

        entry, key = SequenceAPI.get_next_in_sequence_with_key(
            _conf(self.root),
            "monday_early",
            "autumn",
            "seasonal_random_show",
            current_mark=datetime.datetime(2026, 9, 21, 18, 30),
            slot_config=dict(_slot(), tags="autumn"),
            catalog=_Catalog({path: 44 * 60 for path in summer_files + autumn_files}),
        )
        self.assertIn("autumn/FallShow", entry.fpath)
        self.assertEqual(key["tag_path"], "autumn/FallShow")
        self.assertEqual(
            SequenceIO().get_sequence("TestTV", "monday_early", "summer/ShortShow").current_index,
            2,
        )

    def test_no_show_fits_raises_seasonal_unavailable(self):
        files = [
            _episode(self.root, "summer/LongShow", "LongShow", 1, episode)
            for episode in range(1, 25)
        ]
        _put_show("TestTV", "monday_early", "summer/LongShow", files, parent_tag="summer")
        with self.assertRaises(SeasonalRunUnavailable):
            SequenceAPI.get_next_in_sequence_with_key(
                _conf(self.root),
                "monday_early",
                "summer",
                "seasonal_random_show",
                current_mark=datetime.datetime(2026, 8, 17, 18, 30),
                slot_config=_slot(),
                catalog=_Catalog({path: 44 * 60 for path in files}),
            )

    def test_two_seasonal_lanes_avoid_same_show_when_possible(self):
        files_a = [_episode(self.root, "summer/ShowA", "ShowA", 1, 1)]
        files_b = [_episode(self.root, "summer/ShowB", "ShowB", 1, 1)]
        for lane in ("lane1", "lane2"):
            _put_show("TestTV", lane, "summer/ShowA", files_a, parent_tag="summer")
            _put_show("TestTV", lane, "summer/ShowB", files_b, parent_tag="summer")

        catalog = _Catalog({path: 44 * 60 for path in files_a + files_b})
        first_entry, _first_key = SequenceAPI.get_next_in_sequence_with_key(
            _conf(self.root),
            "lane1",
            "summer",
            "seasonal_random_show",
            current_mark=datetime.datetime(2026, 8, 17, 18, 30),
            slot_config=_slot(),
            catalog=catalog,
        )
        second_entry, _second_key = SequenceAPI.get_next_in_sequence_with_key(
            _conf(self.root),
            "lane2",
            "summer",
            "seasonal_random_show",
            current_mark=datetime.datetime(2026, 8, 17, 20, 30),
            slot_config=_slot(),
            catalog=catalog,
        )

        first_show = first_entry.fpath.rsplit(os.sep, 2)[1]
        second_show = second_entry.fpath.rsplit(os.sep, 2)[1]
        self.assertNotEqual(first_show, second_show)

    def test_run_completed_condition_when_state_points_outside_season(self):
        files = [
            _episode(self.root, "summer/ShortShow", "ShortShow", 1, 1),
            _episode(self.root, "summer/ShortShow", "ShortShow", 2, 1),
        ]
        _put_show("TestTV", "monday_early", "summer/ShortShow", files, current_index=1, parent_tag="summer")
        SequenceIO().set_seasonal_run_state("TestTV", "monday_early", "summer/ShortShow", "summer", 1)
        with self.assertRaises(SeasonalRunCompleted):
            SequenceAPI.get_next_in_sequence_with_key(
                _conf(self.root),
                "monday_early",
                "autumn",
                "seasonal_random_show",
                current_mark=datetime.datetime(2026, 9, 7, 18, 30),
                slot_config=dict(_slot(), tags="autumn"),
                catalog=_Catalog({path: 44 * 60 for path in files}),
            )

    def test_delete_sequences_for_station_clears_only_that_station_seasonal_state(self):
        sio = SequenceIO()
        sio.set_seasonal_run_state("StationA", "lane", "summer/A", "summer", 1)
        sio.set_seasonal_run_state("StationB", "lane", "summer/B", "summer", 1)
        sio.delete_sequences_for_station("StationA")
        self.assertIsNone(sio.get_seasonal_run_state("StationA", "lane"))
        self.assertEqual(
            sio.get_seasonal_run_state("StationB", "lane")["active_tag_path"],
            "summer/B",
        )

    def test_seasonal_state_table_exists(self):
        SequenceIO()
        with sqlite3.connect(StationManager().server_conf["db_path"]) as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='seasonal_sequence_state'"
            )
            self.assertEqual(cursor.fetchone()[0], "seasonal_sequence_state")
