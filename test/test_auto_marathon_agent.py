import datetime
import os
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

from fs42.auto_marathon_agent import AutoMarathonAgent
from fs42.block_plan import BlockPlanEntry
from fs42.catalog_api import CatalogAPI
from fs42.catalog_entry import CatalogEntry
from fs42.liquid_blocks import LiquidBlock, LiquidBoundaryFillBlock
from fs42.liquid_schedule import LiquidSchedule
from fs42.station_manager import StationManager


def _configure_db(tmp_path):
    manager = StationManager()
    manager.server_conf["db_path"] = os.path.join(tmp_path, "fs42.db")
    manager.server_conf["normalize_titles"] = False


def _base_conf(content_dir, auto_conf=None):
    conf = {
        "network_name": "Movies",
        "network_type": "standard",
        "content_dir": content_dir,
        "clip_shows": {},
        "break_strategy": "standard",
        "commercial_free": True,
        "bump_dir": "bump",
        "commercial_dir": "commercials",
        "schedule_increment": 60,
        "monday": {},
        "tuesday": {},
        "wednesday": {},
        "thursday": {},
        "friday": {},
        "saturday": {},
        "sunday": {},
    }
    if auto_conf is not None:
        conf["auto_marathons"] = auto_conf
    return conf


def _entry(path, duration, tag):
    entry = CatalogEntry(path, duration, tag)
    entry.realpath = os.path.realpath(path)
    return entry


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("stub")


def _simple_plan(block, catalog):
    if block.content and not isinstance(block.content, list):
        block.plan = [BlockPlanEntry(block.content.path, 0, block.content.duration)]
    else:
        block.plan = []


class TestAutoMarathonAgent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _configure_db(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_or_absent_config_is_off(self):
        self.assertFalse(AutoMarathonAgent.enabled(_base_conf(self.tmp.name)))
        self.assertFalse(
            AutoMarathonAgent.enabled(
                _base_conf(self.tmp.name, {"enabled": False})
            )
        )

    def test_winter_season_id_is_stable_across_year_boundary(self):
        self.assertEqual(
            AutoMarathonAgent.season_id(datetime.datetime(2026, 12, 10)),
            "winter-2026",
        )
        self.assertEqual(
            AutoMarathonAgent.season_id(datetime.datetime(2027, 1, 10)),
            "winter-2026",
        )
        self.assertEqual(
            AutoMarathonAgent.season_id(datetime.datetime(2027, 2, 10)),
            "winter-2026",
        )

    def test_selected_weekend_is_deterministic_and_respects_date_overrides(self):
        conf = _base_conf(
            self.tmp.name,
            {
                "enabled": True,
                "root": "marathons",
                "respect_date_overrides": True,
            },
        )
        conf["date_overrides"] = {
            "September 1 - October 9": {},
            "October 11 - November 30": {},
        }

        first = AutoMarathonAgent.selected_weekend(conf, "autumn-2026")
        second = AutoMarathonAgent.selected_weekend(conf, "autumn-2026")

        self.assertEqual(first, [datetime.date(2026, 10, 10)])
        self.assertEqual(second, first)

    def test_discover_franchises_uses_immediate_subdirectories_and_min_titles(self):
        hp_one = os.path.join(self.tmp.name, "marathons", "autumn", "harry_potter", "01.mkv")
        hp_two = os.path.join(self.tmp.name, "marathons", "autumn", "harry_potter", "02.mkv")
        empty_one = os.path.join(self.tmp.name, "marathons", "autumn", "empty", "01.mkv")
        _touch(hp_one)
        _touch(hp_two)
        _touch(empty_one)
        conf = _base_conf(
            self.tmp.name,
            {
                "enabled": True,
                "root": "marathons",
                "min_titles": 2,
            },
        )

        franchises = AutoMarathonAgent.discover_franchises(conf, "autumn")

        self.assertEqual([franchise["tag"] for franchise in franchises], ["marathons/autumn/harry_potter"])
        self.assertEqual(franchises[0]["files"], [hp_one, hp_two])

    def test_missing_catalog_entries_fails_closed_to_empty_queue(self):
        hp_one = os.path.join(self.tmp.name, "marathons", "autumn", "harry_potter", "01.mkv")
        hp_two = os.path.join(self.tmp.name, "marathons", "autumn", "harry_potter", "02.mkv")
        _touch(hp_one)
        _touch(hp_two)
        conf = _base_conf(
            self.tmp.name,
            {
                "enabled": True,
                "root": "marathons",
                "min_titles": 2,
            },
        )
        conf["date_overrides"] = {
            "September 1 - October 9": {},
            "October 11 - November 30": {},
        }

        schedule = LiquidSchedule(conf)

        self.assertEqual(
            AutoMarathonAgent.build_queue(conf, datetime.datetime(2026, 10, 10, 10), schedule.catalog),
            [],
        )


class TestAutoMarathonScheduling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _configure_db(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_fluid(self, conf, start, end):
        StationManager().stations = [conf]
        with (
            patch.object(LiquidBlock, "make_plan", _simple_plan),
            patch.object(LiquidBoundaryFillBlock, "make_plan", _simple_plan),
            patch.object(CatalogAPI, "update_play_counts", lambda _conf, _entries: None),
        ):
            schedule = LiquidSchedule(conf)
            schedule._fluid(start, end)
            return schedule

    def test_auto_marathon_starts_after_start_hour_and_drains_past_midnight(self):
        hp_paths = [
            os.path.join(self.tmp.name, "marathons", "autumn", "harry_potter", f"0{index}.mkv")
            for index in range(1, 4)
        ]
        for path in hp_paths:
            _touch(path)

        conf = _base_conf(
            self.tmp.name,
            {
                "enabled": True,
                "root": "marathons",
                "start_hour": 10,
                "min_titles": 2,
                "respect_date_overrides": True,
            },
        )
        conf["date_overrides"] = {
            "September 1 - October 9": {},
            "October 11 - November 30": {},
        }
        conf["saturday"] = {
            "8": {"tags": "normal"},
            "10": {"tags": "normal"},
            "18": {"tags": "normal"},
        }
        normal = _entry(os.path.join(self.tmp.name, "normal", "normal.mkv"), 2 * 60 * 60, "normal")
        hp_entries = [
            _entry(path, 8 * 60 * 60, "marathons/autumn/harry_potter")
            for path in hp_paths
        ]
        CatalogAPI.set_entries(conf, [normal] + hp_entries)

        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 10, 10, 8),
            datetime.datetime(2026, 10, 11, 0),
        )

        self.assertEqual([block.content.path for block in schedule._blocks], [normal.path] + hp_paths)
        self.assertEqual(schedule._blocks[-1].end_time, datetime.datetime(2026, 10, 11, 10))
        self.assertEqual(
            [block.sequence_key["sequence_name"] for block in schedule._blocks[1:]],
            ["auto_marathon|2026-10-10|harry_potter"] * 3,
        )

    def test_auto_marathon_does_not_run_on_unconfigured_station(self):
        hp_one = os.path.join(self.tmp.name, "marathons", "autumn", "harry_potter", "01.mkv")
        hp_two = os.path.join(self.tmp.name, "marathons", "autumn", "harry_potter", "02.mkv")
        _touch(hp_one)
        _touch(hp_two)
        conf = _base_conf(self.tmp.name)
        conf["saturday"] = {
            "10": {"tags": "normal"},
            "11": {"tags": "normal"},
        }
        normal = _entry(os.path.join(self.tmp.name, "normal", "normal.mkv"), 60 * 60, "normal")
        CatalogAPI.set_entries(conf, [normal])

        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 10, 10, 10),
            datetime.datetime(2026, 10, 10, 12),
        )

        self.assertEqual([block.content.path for block in schedule._blocks], [normal.path, normal.path])
        self.assertTrue(all(not getattr(block, "sequence_key", None) for block in schedule._blocks))


if __name__ == "__main__":
    unittest.main()
