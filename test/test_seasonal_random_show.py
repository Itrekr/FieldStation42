import datetime
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

from fs42.catalog_entry import CatalogEntry
from fs42.catalog_api import CatalogAPI
from fs42.block_plan import BlockPlanEntry
from fs42.encore_agent import EncoreAgent
from fs42.liquid_blocks import LiquidBlock, LiquidBoundaryFillBlock
from fs42.liquid_schedule import LiquidSchedule
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


def _entry(path, duration, tag):
    entry = CatalogEntry(path, duration, tag)
    entry.realpath = os.path.realpath(path)
    return entry


def _install_entries(conf, entries):
    CatalogAPI.set_entries(conf, entries)


def _simple_plan(block, catalog):
    if block.content and not isinstance(block.content, list):
        block.plan = [BlockPlanEntry(block.content.path, 0, block.content.duration)]
    else:
        block.plan = []


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

    def _run_fluid(self, conf, start, end):
        StationManager().stations = [conf]
        with (
            patch.object(LiquidBlock, "make_plan", _simple_plan),
            patch.object(LiquidBoundaryFillBlock, "make_plan", _simple_plan),
            patch.object(CatalogAPI, "update_play_counts", lambda _conf, entries: None),
        ):
            schedule = LiquidSchedule(conf)
            schedule._fluid(start, end)
            return schedule

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

    def test_s00_specials_are_ignored_for_first_run(self):
        files = [
            _episode(self.root, "summer/Specials", "Specials", 0, 1),
            _episode(self.root, "summer/Specials", "Specials", 1, 1),
            _episode(self.root, "summer/Specials", "Specials", 1, 2),
        ]
        _put_show("TestTV", "monday_early", "summer/Specials", files, parent_tag="summer")
        seq = SequenceIO().get_sequence("TestTV", "monday_early", "summer/Specials")
        run = SeasonalRunPlanner.next_run(seq)
        self.assertEqual(run.season, 1)
        self.assertEqual(run.episode_paths, files[1:])

    def test_parsed_episode_order_overrides_path_order(self):
        files = [
            os.path.join(self.root, "summer/Order/Season 10", "Order - S10E01.mkv"),
            os.path.join(self.root, "summer/Order/Season 2", "Order - S02E01.mkv"),
            os.path.join(self.root, "summer/Order/Season 1", "Order - S01E01.mkv"),
        ]
        _put_show("TestTV", "monday_early", "summer/Order", files, parent_tag="summer")
        seq = SequenceIO().get_sequence("TestTV", "monday_early", "summer/Order")
        self.assertEqual(
            [TitleParser.parse_episode_ref(entry.fpath).season for entry in seq.episodes],
            [1, 2, 10],
        )

    def test_duplicate_parsed_episode_makes_show_ineligible(self):
        files = [
            os.path.join(self.root, "summer/Dupe/a", "Dupe - S01E03.mkv"),
            os.path.join(self.root, "summer/Dupe/b", "Dupe - S01E03.mkv"),
        ]
        _put_show("TestTV", "monday_early", "summer/Dupe", files, parent_tag="summer")
        seq = SequenceIO().get_sequence("TestTV", "monday_early", "summer/Dupe")
        self.assertIsNone(SeasonalRunPlanner.next_run(seq))

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
        slot = _slot()
        slot.pop("hard_end")
        catalog = _Catalog({path: 44 * 60 for path in files})
        projection = SeasonalRunPlanner.project_run(
            run,
            datetime.datetime(2026, 8, 24, 22, 0),
            slot,
            conf,
            catalog,
        )
        self.assertEqual(projection.projected_finish, projection.deadline)
        self.assertTrue(projection.eligible)

        projection = SeasonalRunPlanner.project_run(
            run,
            datetime.datetime(2026, 8, 24, 22, 1),
            slot,
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

    def test_global_progress_across_different_lanes(self):
        lane1_files = [
            _episode(self.root, "summer/Lost", "Lost", 1, 1),
            _episode(self.root, "summer/Lost", "Lost", 1, 2),
            _episode(self.root, "summer/Lost", "Lost", 2, 1),
        ]
        lane2_files = list(lane1_files)
        _put_show("TestTV", "mon_early", "summer/Lost", lane1_files, current_index=2, parent_tag="summer")
        _put_show("TestTV", "wed_late", "summer/Lost", lane2_files, current_index=0, parent_tag="summer")
        SequenceIO().set_seasonal_show_progress(
            "TestTV",
            "lost",
            2,
            1,
            lane1_files[2],
            False,
        )

        entry, _key = SequenceAPI.get_next_in_sequence_with_key(
            _conf(self.root),
            "wed_late",
            "summer",
            "seasonal_random_show",
            current_mark=datetime.datetime(2026, 8, 17, 18, 30),
            slot_config=_slot(),
            catalog=_Catalog({path: 44 * 60 for path in lane1_files}),
        )

        self.assertEqual(entry.fpath, lane1_files[2])

    def test_global_progress_across_seasonal_tags(self):
        summer_files = [
            _episode(self.root, "summer/Lost", "Lost", 1, 1),
            _episode(self.root, "summer/Lost", "Lost", 2, 1),
        ]
        autumn_files = [
            _episode(self.root, "autumn/Lost", "Lost", 1, 1),
            _episode(self.root, "autumn/Lost", "Lost", 2, 1),
        ]
        _put_show("TestTV", "lane", "summer/Lost", summer_files, current_index=1, parent_tag="summer")
        _put_show("TestTV", "lane", "autumn/Lost", autumn_files, current_index=0, parent_tag="autumn")
        SequenceIO().set_seasonal_show_progress(
            "TestTV",
            "lost",
            2,
            1,
            summer_files[1],
            False,
        )

        entry, _key = SequenceAPI.get_next_in_sequence_with_key(
            _conf(self.root),
            "lane",
            "autumn",
            "seasonal_random_show",
            current_mark=datetime.datetime(2026, 9, 21, 18, 30),
            slot_config=dict(_slot(), tags="autumn"),
            catalog=_Catalog({path: 44 * 60 for path in autumn_files}),
        )

        self.assertEqual(entry.fpath, autumn_files[1])

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

    def test_seasonal_duplicate_avoidance_uses_show_identity_across_tags_and_lanes(self):
        summer_lost = [_episode(self.root, "summer/Lost", "Lost", 1, 1)]
        autumn_lost = [_episode(self.root, "autumn/Lost", "Lost", 1, 1)]
        autumn_community = [_episode(self.root, "autumn/Community", "Community", 1, 1)]
        _put_show("TestTV", "lane1", "summer/Lost", summer_lost, parent_tag="summer")
        _put_show("TestTV", "lane2", "autumn/Lost", autumn_lost, parent_tag="autumn")
        _put_show("TestTV", "lane2", "autumn/Community", autumn_community, parent_tag="autumn")
        SequenceIO().set_seasonal_run_state("TestTV", "lane1", "summer/Lost", "summer", 1)

        entry, key = SequenceAPI.get_next_in_sequence_with_key(
            _conf(self.root),
            "lane2",
            "autumn",
            "seasonal_random_show",
            current_mark=datetime.datetime(2026, 9, 21, 18, 30),
            slot_config=dict(_slot(), tags="autumn"),
            catalog=_Catalog({path: 44 * 60 for path in summer_lost + autumn_lost + autumn_community}),
        )

        self.assertEqual(key["tag_path"], "autumn/Community")
        self.assertIn("Community", entry.fpath)

    def test_only_fitting_candidate_active_elsewhere_is_unavailable(self):
        show_a = [_episode(self.root, "autumn/ShowA", "ShowA", 1, 1)]
        show_b = [
            _episode(self.root, "autumn/ShowB", "ShowB", 1, episode)
            for episode in range(1, 25)
        ]
        _put_show("TestTV", "lane1", "autumn/ShowA", show_a, parent_tag="autumn")
        _put_show("TestTV", "lane2", "autumn/ShowA", show_a, parent_tag="autumn")
        _put_show("TestTV", "lane2", "autumn/ShowB", show_b, parent_tag="autumn")
        SequenceIO().set_seasonal_run_state("TestTV", "lane1", "autumn/ShowA", "autumn", 1)

        with self.assertRaises(SeasonalRunUnavailable):
            SequenceAPI.get_next_in_sequence_with_key(
                _conf(self.root),
                "lane2",
                "autumn",
                "seasonal_random_show",
                current_mark=datetime.datetime(2026, 11, 24, 18, 30),
                slot_config=dict(_slot(), tags="autumn"),
                catalog=_Catalog({path: 44 * 60 for path in show_a + show_b}),
            )

    def test_liquid_active_only_candidate_uses_seasonal_fallback(self):
        conf = _conf(self.root)
        conf.update(
            {
                "network_type": "standard",
                "commercial_free": True,
                "monday": {
                    "18": {
                        "tags": "autumn",
                        "sequence": "lane2",
                        "sequence_strategy": "seasonal_random_show",
                        "seasonal_run": {
                            "appointment_minutes": 120,
                            "interval_days": 7,
                            "overflow_days": 14,
                            "fallback_tags": "seasonal_movies/fallback",
                        },
                        "airing_id": "weekly_first",
                        "hard_end": "20:30",
                    }
                },
                "tuesday": {},
                "wednesday": {},
                "thursday": {},
                "friday": {},
                "saturday": {},
                "sunday": {},
            }
        )
        show_a = [_episode(self.root, "autumn/ShowA", "ShowA", 1, 1)]
        show_b = [
            _episode(self.root, "autumn/ShowB", "ShowB", 1, episode)
            for episode in range(1, 25)
        ]
        fallback = os.path.join(self.root, "seasonal_movies/fallback/movie.mkv")
        entries = (
            [_entry(path, 44 * 60, "autumn/ShowA") for path in show_a]
            + [_entry(path, 44 * 60, "autumn/ShowB") for path in show_b]
            + [_entry(fallback, 60 * 60, "seasonal_movies/fallback")]
        )
        _install_entries(conf, entries)
        _put_show("TestTV", "lane1", "autumn/ShowA", show_a, parent_tag="autumn")
        _put_show("TestTV", "lane2", "autumn/ShowA", show_a, parent_tag="autumn")
        _put_show("TestTV", "lane2", "autumn/ShowB", show_b, parent_tag="autumn")
        SequenceIO().set_seasonal_run_state("TestTV", "lane1", "autumn/ShowA", "autumn", 1)

        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 11, 23, 18, 30),
            datetime.datetime(2026, 11, 23, 20, 30),
        )

        self.assertEqual(len(schedule._blocks), 1)
        self.assertEqual(schedule._blocks[0].content.path, fallback)
        self.assertIsNone(SequenceIO().get_seasonal_run_state("TestTV", "lane2"))

    def test_active_show_exclusion_does_not_consume_random_bag(self):
        show_a = [_episode(self.root, "autumn/ShowA", "ShowA", 1, 1)]
        show_b = [
            _episode(self.root, "autumn/ShowB", "ShowB", 1, episode)
            for episode in range(1, 25)
        ]
        _put_show("TestTV", "lane1", "autumn/ShowA", show_a, parent_tag="autumn")
        _put_show("TestTV", "lane2", "autumn/ShowA", show_a, parent_tag="autumn")
        _put_show("TestTV", "lane2", "autumn/ShowB", show_b, parent_tag="autumn")
        sio = SequenceIO()
        sio.set_seasonal_run_state("TestTV", "lane1", "autumn/ShowA", "autumn", 1)
        sio.set_sequence_group_shuffle_state(
            "TestTV",
            "lane2",
            "autumn",
            "autumn/ShowA",
            "seed",
            0,
            ["autumn/ShowA", "autumn/ShowB"],
            0,
        )

        with self.assertRaises(SeasonalRunUnavailable):
            SequenceAPI.get_next_in_sequence_with_key(
                _conf(self.root),
                "lane2",
                "autumn",
                "seasonal_random_show",
                current_mark=datetime.datetime(2026, 11, 24, 18, 30),
                slot_config=dict(_slot(), tags="autumn"),
                catalog=_Catalog({path: 44 * 60 for path in show_a + show_b}),
            )

        state = sio.get_sequence_group_shuffle_state("TestTV", "lane2", "autumn")
        self.assertEqual(state["position"], 0)
        self.assertEqual(state["order"][0], "autumn/ShowA")

        sio.clear_seasonal_run_state("TestTV", "lane1")
        entry, key = SequenceAPI.get_next_in_sequence_with_key(
            _conf(self.root),
            "lane2",
            "autumn",
            "seasonal_random_show",
            current_mark=datetime.datetime(2026, 11, 24, 18, 30),
            slot_config=dict(_slot(), tags="autumn"),
            catalog=_Catalog({path: 44 * 60 for path in show_a + show_b}),
        )
        self.assertEqual(key["tag_path"], "autumn/ShowA")
        self.assertEqual(entry.fpath, show_a[0])

    def test_same_show_other_seasonal_tag_active_with_no_alternative_is_unavailable(self):
        summer_lost = [_episode(self.root, "summer/Lost", "Lost", 1, 1)]
        autumn_lost = [_episode(self.root, "autumn/Lost", "Lost", 1, 1)]
        _put_show("TestTV", "lane1", "summer/Lost", summer_lost, parent_tag="summer")
        _put_show("TestTV", "lane2", "autumn/Lost", autumn_lost, parent_tag="autumn")
        SequenceIO().set_seasonal_run_state("TestTV", "lane1", "summer/Lost", "summer", 1)

        with self.assertRaises(SeasonalRunUnavailable):
            SequenceAPI.get_next_in_sequence_with_key(
                _conf(self.root),
                "lane2",
                "autumn",
                "seasonal_random_show",
                current_mark=datetime.datetime(2026, 9, 21, 18, 30),
                slot_config=dict(_slot(), tags="autumn"),
                catalog=_Catalog({path: 44 * 60 for path in summer_lost + autumn_lost}),
            )

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

    def test_liquid_rejected_finale_restores_seasonal_transaction(self):
        conf = _conf(self.root)
        conf.update(
            {
                "network_type": "standard",
                "commercial_free": True,
                "monday": {
                    "18": {
                        "tags": "summer",
                        "sequence": "monday_early",
                        "sequence_strategy": "seasonal_random_show",
                        "seasonal_run": {
                            "appointment_minutes": 120,
                            "interval_days": 7,
                            "overflow_days": 14,
                            "fallback_tags": "seasonal_movies/fallback",
                        },
                        "airing_id": "weekly_first",
                        "hard_end": "20:30",
                    }
                },
                "tuesday": {},
                "wednesday": {},
                "thursday": {},
                "friday": {},
                "saturday": {},
                "sunday": {},
            }
        )
        files = [
            _episode(self.root, "summer/Lost", "Lost", 1, 1),
            _episode(self.root, "summer/Lost", "Lost", 1, 2),
        ]
        entries = [
            _entry(files[0], 44 * 60, "summer/Lost"),
            _entry(files[1], 150 * 60, "summer/Lost"),
            _entry(os.path.join(self.root, "seasonal_movies/fallback/movie.mkv"), 60 * 60, "seasonal_movies/fallback"),
        ]
        _install_entries(conf, entries)
        _put_show("TestTV", "monday_early", "summer/Lost", files, current_index=1, parent_tag="summer")
        sio = SequenceIO()
        sio.set_seasonal_run_state("TestTV", "monday_early", "summer/Lost", "summer", 1)
        sio.set_seasonal_show_progress("TestTV", "lost", 1, 2, files[1], False)

        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 8, 17, 18, 30),
            datetime.datetime(2026, 8, 17, 20, 30),
        )

        self.assertEqual(len(schedule._blocks), 1)
        self.assertNotEqual(getattr(schedule._blocks[0].content, "path", None), files[1])
        self.assertEqual(
            sio.get_sequence("TestTV", "monday_early", "summer/Lost").current_index,
            1,
        )
        self.assertEqual(
            sio.get_seasonal_run_state("TestTV", "monday_early")["active_tag_path"],
            "summer/Lost",
        )
        self.assertEqual(
            sio.get_seasonal_show_progress("TestTV", "lost")["next_path"],
            files[1],
        )
        retry, _key = SequenceAPI.get_next_in_sequence_with_key(
            conf,
            "monday_early",
            "summer",
            "seasonal_random_show",
            current_mark=datetime.datetime(2026, 8, 24, 18, 30),
            slot_config=conf["monday"]["18"],
            catalog=_Catalog({entry.path: entry.duration for entry in entries}),
        )
        self.assertEqual(retry.fpath, files[1])

    def test_liquid_finale_uses_fallback_without_second_tv_show_or_airing_history(self):
        conf = _conf(self.root)
        conf.update(
            {
                "network_type": "standard",
                "commercial_free": True,
                "monday": {
                    "18": {
                        "tags": "summer",
                        "sequence": "monday_early",
                        "sequence_strategy": "seasonal_random_show",
                        "seasonal_run": {
                            "appointment_minutes": 120,
                            "interval_days": 7,
                            "overflow_days": 14,
                            "fallback_tags": "seasonal_movies/fallback",
                        },
                        "airing_id": "weekly_first",
                        "hard_end": "20:30",
                    }
                },
                "tuesday": {},
                "wednesday": {},
                "thursday": {},
                "friday": {},
                "saturday": {},
                "sunday": {},
            }
        )
        files = [
            _episode(self.root, "summer/Lost", "Lost", 1, 1),
            _episode(self.root, "summer/Lost", "Lost", 1, 2),
            _episode(self.root, "summer/Lost", "Lost", 2, 1),
        ]
        entries = [
            _entry(files[0], 44 * 60, "summer/Lost"),
            _entry(files[1], 44 * 60, "summer/Lost"),
            _entry(files[2], 44 * 60, "summer/Lost"),
            _entry(os.path.join(self.root, "seasonal_movies/fallback/movie.mkv"), 30 * 60, "seasonal_movies/fallback"),
        ]
        _install_entries(conf, entries)
        _put_show("TestTV", "monday_early", "summer/Lost", files, current_index=1, parent_tag="summer")
        sio = SequenceIO()
        sio.set_seasonal_run_state("TestTV", "monday_early", "summer/Lost", "summer", 1)
        sio.set_seasonal_show_progress("TestTV", "lost", 1, 2, files[1], False)

        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 8, 17, 18, 30),
            datetime.datetime(2026, 8, 17, 20, 30),
        )

        self.assertEqual([getattr(block.content, "path", None) for block in schedule._blocks], [files[1], entries[3].path])
        self.assertIsNone(sio.get_seasonal_run_state("TestTV", "monday_early"))
        progress = sio.get_seasonal_show_progress("TestTV", "lost")
        self.assertEqual((progress["next_season"], progress["next_episode"]), (2, 1))

        with sqlite3.connect(StationManager().server_conf["db_path"]) as conn:
            rows = conn.execute(
                "SELECT airing_id, content_path FROM airing_history WHERE station = ? ORDER BY source_start_time",
                ("TestTV",),
            ).fetchall()
        self.assertEqual(rows, [("weekly_first", files[1])])

    def test_schedule_offset_does_not_accumulate_across_extensions(self):
        conf = _conf(self.root)
        conf.update({"network_type": "standard", "schedule_offset": 30})
        schedule = LiquidSchedule(conf)
        calls = []

        class Block:
            def __init__(self, end_time):
                self.end_time = end_time

        def fake_fluid(start, end):
            calls.append((start, end))
            schedule._blocks = [Block(end)]

        with patch.object(schedule, "_fluid", side_effect=fake_fluid):
            schedule.add_week()
            schedule.add_week()
            schedule.add_week()

        self.assertEqual(calls[0][0].minute, 30)
        self.assertEqual(calls[1][0], calls[0][1])
        self.assertEqual(calls[2][0], calls[1][1])
        self.assertEqual([call[0].minute for call in calls], [30, 30, 30])
