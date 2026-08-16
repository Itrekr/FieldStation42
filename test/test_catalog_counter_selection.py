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

from fs42.block_plan import BlockPlanEntry
from fs42.catalog import ShowCatalog
from fs42.catalog_api import CatalogAPI
from fs42.catalog_entry import CatalogEntry
from fs42.liquid_blocks import LiquidBlock, LiquidBoundaryFillBlock
from fs42.liquid_schedule import LiquidSchedule
from fs42.station_manager import StationManager


WHEN = datetime.datetime(2026, 7, 1, 20)


def _configure_db(tmp_path):
    manager = StationManager()
    manager.server_conf["db_path"] = os.path.join(tmp_path, "fs42.db")
    manager.server_conf["normalize_titles"] = False


def _conf():
    return {
        "network_name": "TestTV",
        "network_type": "standard",
        "content_dir": "/content",
        "clip_shows": {},
        "break_strategy": "standard",
        "commercial_free": True,
        "bump_dir": "bump",
        "commercial_dir": "commercials",
        "schedule_increment": 30,
        "monday": {},
        "tuesday": {},
        "wednesday": {},
        "thursday": {},
        "friday": {},
        "saturday": {},
        "sunday": {},
    }


def _entry(path, tag="movies", count=0, duration=60 * 60, realpath=None):
    entry = CatalogEntry(path, duration, tag, count=count)
    entry.realpath = realpath if realpath is not None else os.path.realpath(path)
    return entry


def _catalog(entries_by_tag):
    catalog = ShowCatalog(_conf(), load=False)
    catalog.clip_index = {
        tag: list(entries)
        for tag, entries in entries_by_tag.items()
    }
    return catalog


def _simple_plan(block, catalog):
    if block.content and not isinstance(block.content, list):
        block.plan = [BlockPlanEntry(block.content.path, 0, block.content.duration)]
    else:
        block.plan = []


class TestCatalogCounterSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _configure_db(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lowest_count_random_selection_ignores_higher_counts(self):
        entries = [
            _entry("/content/movies/a.mp4", count=0),
            _entry("/content/movies/b.mp4", count=0),
            _entry("/content/movies/c.mp4", count=1),
            _entry("/content/movies/d.mp4", count=1),
        ]
        catalog = _catalog({"movies": entries})

        for _ in range(20):
            result = catalog.find_candidate("movies", 99999, WHEN)
            self.assertIn(result.path, {entries[0].path, entries[1].path})

    def test_complete_rotation_before_repeat_with_accepted_count_updates(self):
        entries = [
            _entry(f"/content/movies/{letter}.mp4", count=0)
            for letter in ("a", "b", "c", "d")
        ]
        catalog = _catalog({"movies": entries})

        selected = []
        for _ in range(4):
            result = catalog.find_candidate("movies", 99999, WHEN)
            selected.append(result.path)
            result.count += 1

        self.assertEqual(set(selected), {entry.path for entry in entries})

    def test_randomness_among_equal_minimum_candidates(self):
        entries = [
            _entry(f"/content/movies/{letter}.mp4", count=0)
            for letter in ("a", "b", "c")
        ]
        catalog = _catalog({"movies": entries})

        with patch("fs42.catalog.random.choice", side_effect=lambda choices: choices[-1]) as choice:
            result = catalog.find_candidate("movies", 99999, WHEN)

        self.assertEqual(result.path, entries[-1].path)
        self.assertEqual({entry.path for entry in choice.call_args.args[0]}, {entry.path for entry in entries})

    def test_pooled_tags_use_global_minimum(self):
        entries = {
            "tag_a": [
                _entry("/content/tag_a/a.mp4", "tag_a", count=4),
                _entry("/content/tag_a/b.mp4", "tag_a", count=5),
            ],
            "tag_b": [
                _entry("/content/tag_b/c.mp4", "tag_b", count=4),
                _entry("/content/tag_b/d.mp4", "tag_b", count=6),
            ],
        }
        catalog = _catalog(entries)

        for _ in range(20):
            result = catalog.find_candidate(["tag_a", "tag_b"], 99999, WHEN)
            self.assertIn(result.path, {"/content/tag_a/a.mp4", "/content/tag_b/c.mp4"})

    def test_eligibility_filtering_happens_before_minimum_calculation(self):
        entries = [
            _entry("/content/movies/a.mp4", count=1, duration=180 * 60),
            _entry("/content/movies/b.mp4", count=2, duration=90 * 60),
            _entry("/content/movies/c.mp4", count=2, duration=95 * 60),
            _entry("/content/movies/d.mp4", count=3, duration=90 * 60),
        ]
        catalog = _catalog({"movies": entries})

        for _ in range(20):
            result = catalog.find_candidate("movies", 120 * 60, WHEN)
            self.assertIn(result.path, {entries[1].path, entries[2].path})

    def test_find_candidate_does_not_increment_before_acceptance(self):
        entry = _entry("/content/movies/a.mp4", count=2)
        catalog = _catalog({"movies": [entry]})

        result = catalog.find_candidate("movies", 99999, WHEN)

        self.assertIs(result, entry)
        self.assertEqual(entry.count, 2)

    def test_existing_counts_survive_rebuild(self):
        conf = _conf()
        old_entries = [
            _entry("/content/movies/a.mp4", count=7),
            _entry("/content/movies/b.mp4", count=5),
            _entry("/content/movies/c.mp4", count=9),
        ]
        CatalogAPI.set_entries(conf, old_entries)

        rebuilt = [
            _entry("/content/movies/a.mp4"),
            _entry("/content/movies/b.mp4"),
            _entry("/content/movies/c.mp4"),
        ]
        CatalogAPI.set_entries(conf, rebuilt)

        counts = {
            entry.path: entry.count
            for entry in CatalogAPI.get_entries(conf)
        }
        self.assertEqual(counts["/content/movies/a.mp4"], 7)
        self.assertEqual(counts["/content/movies/b.mp4"], 5)
        self.assertEqual(counts["/content/movies/c.mp4"], 9)

    def test_new_movie_inherits_existing_minimum(self):
        conf = _conf()
        CatalogAPI.set_entries(
            conf,
            [
                _entry("/content/movies/a.mp4", count=3),
                _entry("/content/movies/b.mp4", count=3),
                _entry("/content/movies/c.mp4", count=4),
            ],
        )

        CatalogAPI.set_entries(
            conf,
            [
                _entry("/content/movies/a.mp4"),
                _entry("/content/movies/b.mp4"),
                _entry("/content/movies/c.mp4"),
                _entry("/content/movies/d.mp4"),
            ],
        )

        counts = {
            entry.path: entry.count
            for entry in CatalogAPI.get_entries(conf)
        }
        self.assertEqual(counts["/content/movies/d.mp4"], 3)

    def test_symlink_rename_preserves_count_by_realpath(self):
        conf = _conf()
        realpath = "/media/movie.mkv"
        CatalogAPI.set_entries(
            conf,
            [_entry("/content/tag/movie-old.mkv", "tag", count=6, realpath=realpath)],
        )

        CatalogAPI.set_entries(
            conf,
            [_entry("/content/tag/movie-new.mkv", "tag", realpath=realpath)],
        )

        entry = CatalogAPI.get_entries(conf)[0]
        self.assertEqual(entry.path, "/content/tag/movie-new.mkv")
        self.assertEqual(entry.count, 6)

    def test_removed_movie_disappears(self):
        conf = _conf()
        CatalogAPI.set_entries(
            conf,
            [
                _entry("/content/movies/a.mp4", count=2),
                _entry("/content/movies/b.mp4", count=2),
                _entry("/content/movies/c.mp4", count=2),
            ],
        )

        CatalogAPI.set_entries(
            conf,
            [
                _entry("/content/movies/a.mp4"),
                _entry("/content/movies/b.mp4"),
            ],
        )

        self.assertEqual(
            {entry.path for entry in CatalogAPI.get_entries(conf)},
            {"/content/movies/a.mp4", "/content/movies/b.mp4"},
        )

    def test_empty_tag_starts_at_zero(self):
        conf = _conf()
        CatalogAPI.set_entries(
            conf,
            [
                _entry("/content/new/a.mp4", "new"),
                _entry("/content/new/b.mp4", "new"),
                _entry("/content/new/c.mp4", "new"),
            ],
        )

        self.assertEqual(
            [entry.count for entry in CatalogAPI.get_entries(conf)],
            [0, 0, 0],
        )

    def test_new_item_in_pooled_tag_gets_pooled_floor(self):
        conf = _conf()
        conf["monday"] = {
            "20": {
                "tags": ["tag_a", "tag_b"],
                "pooled_tags": True,
            }
        }
        CatalogAPI.set_entries(
            conf,
            [
                _entry("/content/tag_a/a.mp4", "tag_a", count=4),
                _entry("/content/tag_a/b.mp4", "tag_a", count=5),
                _entry("/content/tag_b/c.mp4", "tag_b", count=4),
                _entry("/content/tag_b/d.mp4", "tag_b", count=6),
            ],
        )

        CatalogAPI.set_entries(
            conf,
            [
                _entry("/content/tag_a/a.mp4", "tag_a"),
                _entry("/content/tag_a/b.mp4", "tag_a"),
                _entry("/content/tag_b/c.mp4", "tag_b"),
                _entry("/content/tag_b/d.mp4", "tag_b"),
                _entry("/content/tag_b/e.mp4", "tag_b"),
            ],
        )

        counts = {
            entry.path: entry.count
            for entry in CatalogAPI.get_entries(conf)
        }
        self.assertEqual(counts["/content/tag_b/e.mp4"], 4)

    def test_accepted_candidate_updates_in_memory_count_for_next_selection(self):
        conf = _conf()
        conf["monday"] = {
            "20": {"tags": "movies"},
            "21": {"tags": "movies"},
        }
        entries = [
            _entry("/content/movies/a.mp4", count=2),
            _entry("/content/movies/b.mp4", count=2),
        ]
        CatalogAPI.set_entries(conf, entries)
        StationManager().stations = [conf]

        with (
            patch.object(LiquidBlock, "make_plan", _simple_plan),
            patch.object(CatalogAPI, "update_play_counts", lambda _conf, entries: None),
            patch("fs42.catalog.random.choice", side_effect=lambda choices: choices[0]),
        ):
            schedule = LiquidSchedule(conf)
            schedule._fluid(
                datetime.datetime(2026, 6, 1, 20),
                datetime.datetime(2026, 6, 1, 22),
            )

        self.assertEqual(
            [block.content.path for block in schedule._blocks],
            ["/content/movies/a.mp4", "/content/movies/b.mp4"],
        )
        self.assertEqual(
            {
                entry.path: entry.count
                for entry in schedule.catalog.clip_index["movies"]
            },
            {
                "/content/movies/a.mp4": 3,
                "/content/movies/b.mp4": 3,
            },
        )

    def test_hard_end_rejection_does_not_consume_in_memory_count(self):
        conf = _conf()
        conf["monday"] = {
            "20": {"tags": "movies", "hard_end": "20:30"},
        }
        entries = [
            _entry("/content/movies/a.mp4", count=2, duration=45 * 60),
            _entry("/content/movies/b.mp4", count=2, duration=45 * 60),
        ]
        CatalogAPI.set_entries(conf, entries)
        StationManager().stations = [conf]

        with (
            patch.object(LiquidBlock, "make_plan", _simple_plan),
            patch.object(LiquidBoundaryFillBlock, "make_plan", _simple_plan),
            patch.object(CatalogAPI, "update_play_counts", lambda _conf, entries: None),
            patch("fs42.catalog.random.choice", side_effect=lambda choices: choices[0]),
        ):
            schedule = LiquidSchedule(conf)
            schedule._fluid(
                datetime.datetime(2026, 6, 1, 20),
                datetime.datetime(2026, 6, 1, 20, 30),
            )

        self.assertIsInstance(schedule._blocks[0], LiquidBoundaryFillBlock)
        self.assertEqual(
            {
                entry.path: entry.count
                for entry in schedule.catalog.clip_index["movies"]
            },
            {
                "/content/movies/a.mp4": 2,
                "/content/movies/b.mp4": 2,
            },
        )

    def test_persistence_increments_exactly_once(self):
        conf = _conf()
        conf["monday"] = {"20": {"tags": "movies"}}
        entry = _entry("/content/movies/a.mp4", count=4)
        CatalogAPI.set_entries(conf, [entry])
        StationManager().stations = [conf]

        with patch.object(LiquidBlock, "make_plan", _simple_plan):
            schedule = LiquidSchedule(conf)
            schedule._fluid(
                datetime.datetime(2026, 6, 1, 20),
                datetime.datetime(2026, 6, 1, 21),
            )

        stored = CatalogAPI.get_by_path(conf, "/content/movies/a.mp4")
        self.assertEqual(stored.count, 5)

    def test_random_tags_still_selects_one_tag_first(self):
        from fs42.slot_reader import SlotReader

        slot = {
            "tags": ["tag_a", "tag_b"],
            "random_tags": True,
        }
        with patch("fs42.slot_reader.random.randrange", return_value=1):
            tag, tag_index = SlotReader.get_tag_from_slot(slot, WHEN)

        self.assertEqual(tag, "tag_b")
        self.assertEqual(tag_index, 1)


if __name__ == "__main__":
    unittest.main()
