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

from fs42.block_plan import BlockPlanEntry
from fs42.catalog_api import CatalogAPI
from fs42.catalog_entry import CatalogEntry
from fs42.encore_agent import EncoreAgent
from fs42.liquid_blocks import LiquidBlock, LiquidBoundaryFillBlock
from fs42.liquid_schedule import LiquidSchedule
from fs42.sequence import NamedSequence
from fs42.sequence_api import SequenceAPI
from fs42.sequence_io import SequenceIO
from fs42.station_manager import StationManager


def _configure_db(tmp_path):
    manager = StationManager()
    manager.server_conf["db_path"] = os.path.join(tmp_path, "fs42.db")
    manager.server_conf["normalize_titles"] = False


def _entry(path, duration, tag):
    entry = CatalogEntry(path, duration, tag)
    entry.realpath = os.path.realpath(path)
    return entry


def _base_conf(schedule_increment=30, fallback_tag=None):
    conf = {
        "network_name": "TestTV",
        "network_type": "standard",
        "content_dir": "/content",
        "clip_shows": {},
        "break_strategy": "standard",
        "commercial_free": True,
        "bump_dir": "bump",
        "commercial_dir": "commercials",
        "schedule_increment": schedule_increment,
        "monday": {},
        "tuesday": {},
        "wednesday": {},
        "thursday": {},
        "friday": {},
        "saturday": {},
        "sunday": {},
    }
    if fallback_tag:
        conf["fallback_tag"] = fallback_tag
    return conf


def _install_entries(conf, entries):
    CatalogAPI.set_entries(conf, entries)


def _install_sequence(conf, name, tag, entries, strategy=None, current_index=0):
    SequenceIO().put_sequence(
        conf["network_name"],
        NamedSequence(
            conf["network_name"],
            name,
            tag,
            0,
            1,
            current_index,
            [entry.path for entry in entries],
            True,
            strategy,
        ),
    )


def _simple_plan(block, catalog):
    if block.content and not isinstance(block.content, list):
        block.plan = [BlockPlanEntry(block.content.path, 0, block.content.duration)]
    else:
        block.plan = []


class TestHardEndScheduling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _configure_db(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_fluid(self, conf, start, end, play_counts=None):
        StationManager().stations = [conf]
        if play_counts is None:
            play_counts = []

        def capture_counts(_conf, entries):
            play_counts.extend(entries)

        with (
            patch.object(LiquidBlock, "make_plan", _simple_plan),
            patch.object(LiquidBoundaryFillBlock, "make_plan", _simple_plan),
            patch.object(CatalogAPI, "update_play_counts", capture_counts),
        ):
            schedule = LiquidSchedule(conf)
            schedule._fluid(start, end)
            return schedule

    def test_exact_fit_programme_is_accepted(self):
        conf = _base_conf()
        conf["monday"] = {
            "19": {
                "tags": "prime",
                "sequence": "prime1",
                "airing_id": "prime1",
                "hard_end": "21:00",
            }
        }
        entries = [_entry("/content/prime/e01.mp4", 90 * 60, "prime")]
        _install_entries(conf, entries)
        _install_sequence(conf, "prime1", "prime", entries)

        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 8, 10, 19, 30),
            datetime.datetime(2026, 8, 10, 21),
        )

        self.assertEqual(len(schedule._blocks), 1)
        self.assertEqual(schedule._blocks[0].content.path, entries[0].path)
        self.assertEqual(schedule._blocks[0].end_time, datetime.datetime(2026, 8, 10, 21))

    def test_programme_exceeding_boundary_is_rejected_and_sequence_rewound(self):
        conf = _base_conf()
        conf["monday"] = {
            "18": {
                "tags": "prime",
                "sequence": "prime1",
                "airing_id": "prime1",
                "hard_end": "21:00",
            },
            "20": {
                "tags": "prime",
                "sequence": "prime1",
                "airing_id": "prime1",
                "hard_end": "21:00",
            },
            "21": {"tags": "next"},
        }
        prime = [
            _entry("/content/prime/e01.mp4", 120 * 60, "prime"),
            _entry("/content/prime/e02.mp4", 90 * 60, "prime"),
        ]
        next_entries = [_entry("/content/next/e01.mp4", 60 * 60, "next")]
        _install_entries(conf, prime + next_entries)
        _install_sequence(conf, "prime1", "prime", prime)

        play_counts = []
        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 8, 10, 18),
            datetime.datetime(2026, 8, 10, 22),
            play_counts=play_counts,
        )

        self.assertEqual(
            [(block.title, block.start_time, block.end_time, getattr(block.content, "path", None)) for block in schedule._blocks],
            [
                ("e01", datetime.datetime(2026, 8, 10, 18), datetime.datetime(2026, 8, 10, 20), "/content/prime/e01.mp4"),
                ("Filler", datetime.datetime(2026, 8, 10, 20), datetime.datetime(2026, 8, 10, 21), None),
                ("e01", datetime.datetime(2026, 8, 10, 21), datetime.datetime(2026, 8, 10, 22), "/content/next/e01.mp4"),
            ],
        )

        seq = SequenceIO().get_sequence("TestTV", "prime1", "prime")
        self.assertEqual(seq.current_index, 1)
        self.assertEqual([entry.path for entry in play_counts], ["/content/prime/e01.mp4", "/content/next/e01.mp4"])

        with sqlite3.connect(StationManager().server_conf["db_path"]) as conn:
            rows = conn.execute(
                "SELECT content_path FROM airing_history WHERE station = ? ORDER BY source_start_time",
                ("TestTV",),
            ).fetchall()
        self.assertEqual(rows, [("/content/prime/e01.mp4",)])

    def test_random_show_rejection_rewinds_actual_child(self):
        conf = _base_conf()
        conf["monday"] = {
            "20": {
                "tags": "prime",
                "sequence": "prime1",
                "sequence_strategy": "random_show",
                "airing_id": "prime1",
                "hard_end": "21:00",
            }
        }
        succession = [_entry("/content/prime/succession/e01.mp4", 90 * 60, "prime/succession")]
        other = [_entry("/content/prime/other/e01.mp4", 30 * 60, "prime/other")]
        _install_entries(conf, succession + other)
        _install_sequence(conf, "prime1", "prime/succession", succession, strategy="random_show")
        _install_sequence(conf, "prime1", "prime/other", other, strategy="random_show")
        SequenceIO().set_active_sequence("TestTV", "prime1", "prime", "prime/succession")

        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 8, 10, 20),
            datetime.datetime(2026, 8, 10, 21),
        )

        self.assertIsInstance(schedule._blocks[0], LiquidBoundaryFillBlock)
        child = SequenceIO().get_sequence("TestTV", "prime1", "prime/succession")
        self.assertEqual(child.current_index, 0)
        self.assertEqual(
            SequenceIO().get_active_sequence("TestTV", "prime1", "prime"),
            "prime/succession",
        )

    def test_rejected_encore_does_not_consume_queue(self):
        conf = _base_conf()
        conf["monday"] = {
            "4": {
                "encore": {
                    "source": "prime1",
                    "strategy": "queue",
                    "cursor": "prime1_morning",
                },
                "hard_end": "06:00",
            },
            "5": {
                "encore": {
                    "source": "prime1",
                    "strategy": "queue",
                    "cursor": "prime1_morning",
                },
                "hard_end": "06:00",
            }
        }
        entries = [
            _entry("/content/prime/e01.mp4", 60 * 60, "prime"),
            _entry("/content/prime/e02.mp4", 90 * 60, "prime"),
        ]
        _install_entries(conf, entries)
        agent = EncoreAgent(conf)
        source_start = datetime.datetime(2026, 8, 9, 18)
        for index, entry in enumerate(entries):
            agent.record_airing("prime1", LiquidBlock(entry, source_start + datetime.timedelta(hours=index), source_start + datetime.timedelta(hours=index + 1)))
        agent.commit()

        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 8, 10, 4),
            datetime.datetime(2026, 8, 10, 6),
        )

        self.assertEqual(schedule._blocks[0].content.path, entries[0].path)
        self.assertIsInstance(schedule._blocks[1], LiquidBoundaryFillBlock)

        retry_agent = EncoreAgent(conf)
        candidate, _key = retry_agent.resolve(
            {"source": "prime1", "strategy": "queue", "cursor": "prime1_morning"},
            datetime.datetime(2026, 8, 11, 4),
        )
        self.assertEqual(candidate.path, entries[1].path)

    def test_fallback_programme_that_fits_fills_to_boundary(self):
        conf = _base_conf(fallback_tag="fallback")
        conf["monday"] = {
            "20": {
                "tags": "prime",
                "sequence": "prime1",
                "hard_end": "21:00",
            }
        }
        prime = [_entry("/content/prime/e01.mp4", 90 * 60, "prime")]
        fallback = [_entry("/content/fallback/short.mp4", 22 * 60, "fallback")]
        _install_entries(conf, prime + fallback)
        _install_sequence(conf, "prime1", "prime", prime)

        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 8, 10, 20),
            datetime.datetime(2026, 8, 10, 21),
        )

        self.assertEqual(schedule._blocks[0].content.path, fallback[0].path)
        self.assertEqual(schedule._blocks[0].end_time, datetime.datetime(2026, 8, 10, 21))

    def test_fallback_programme_that_does_not_fit_uses_pure_filler(self):
        conf = _base_conf(fallback_tag="fallback")
        conf["monday"] = {
            "20": {
                "tags": "prime",
                "sequence": "prime1",
                "hard_end": "20:30",
            }
        }
        prime = [_entry("/content/prime/e01.mp4", 31 * 60, "prime")]
        fallback = [_entry("/content/fallback/long.mp4", 31 * 60, "fallback")]
        _install_entries(conf, prime + fallback)
        _install_sequence(conf, "prime1", "prime", prime)

        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 8, 10, 20),
            datetime.datetime(2026, 8, 10, 21),
        )

        self.assertIsInstance(schedule._blocks[0], LiquidBoundaryFillBlock)
        self.assertEqual(schedule._blocks[0].end_time, datetime.datetime(2026, 8, 10, 20, 30))

    def test_midnight_hard_end_resolves_to_next_day(self):
        conf = _base_conf()
        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 8, 10, 23, 30),
            datetime.datetime(2026, 8, 10, 23, 30),
        )

        hard_end = schedule._resolve_hard_end(
            {"hard_end": "00:00"},
            datetime.datetime(2026, 8, 10, 23, 30),
        )
        self.assertEqual(hard_end, datetime.datetime(2026, 8, 11, 0))

    def test_invalid_hard_end_fails_clearly(self):
        conf = _base_conf()
        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 8, 10, 20),
            datetime.datetime(2026, 8, 10, 20),
        )

        with self.assertRaisesRegex(ValueError, "Invalid hard_end"):
            schedule._resolve_hard_end(
                {"hard_end": "21:70"},
                datetime.datetime(2026, 8, 10, 20),
            )

    def test_schedule_increment_controls_boundary_decision(self):
        conf = _base_conf(schedule_increment=30)
        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 8, 10, 20),
            datetime.datetime(2026, 8, 10, 20),
        )
        candidate = _entry("/content/prime/e01.mp4", 61 * 60, "prime")

        block_30, _ = schedule._block_for_candidate(conf, "prime", datetime.datetime(2026, 8, 10, 20), candidate)
        self.assertEqual(block_30.end_time, datetime.datetime(2026, 8, 10, 21, 30))

        slot_15 = dict(conf)
        slot_15["schedule_increment"] = 15
        block_15, _ = schedule._block_for_candidate(slot_15, "prime", datetime.datetime(2026, 8, 10, 20), candidate)
        self.assertEqual(block_15.end_time, datetime.datetime(2026, 8, 10, 21, 15))

    def test_no_hard_end_preserves_fluid_crossing_behavior(self):
        conf = _base_conf()
        conf["monday"] = {
            "18": {
                "tags": "prime",
                "sequence": "prime1",
            },
            "19": {
                "tags": "prime",
                "sequence": "prime1",
            },
        }
        entries = [
            _entry("/content/prime/e01.mp4", 90 * 60, "prime"),
            _entry("/content/prime/e02.mp4", 90 * 60, "prime"),
        ]
        _install_entries(conf, entries)
        _install_sequence(conf, "prime1", "prime", entries)

        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 8, 10, 18),
            datetime.datetime(2026, 8, 10, 21),
        )

        self.assertEqual(
            [(block.start_time, block.end_time, block.content.path) for block in schedule._blocks],
            [
                (datetime.datetime(2026, 8, 10, 18), datetime.datetime(2026, 8, 10, 19, 30), entries[0].path),
                (datetime.datetime(2026, 8, 10, 19, 30), datetime.datetime(2026, 8, 10, 21), entries[1].path),
            ],
        )

    def test_rejected_candidate_is_not_registered_in_exclusion_index(self):
        conf = _base_conf()
        conf["monday"] = {
            "20": {
                "tags": "prime",
                "sequence": "prime1",
                "hard_end": "21:00",
            }
        }
        entry = _entry("/content/prime/e01.mp4", 90 * 60, "prime")
        _install_entries(conf, [entry])
        _install_sequence(conf, "prime1", "prime", [entry])
        exclusion_index = {}

        with patch.object(LiquidSchedule, "_build_exclusion_index", return_value=exclusion_index):
            self._run_fluid(
                conf,
                datetime.datetime(2026, 8, 10, 20),
                datetime.datetime(2026, 8, 10, 21),
            )

        self.assertEqual(exclusion_index, {})

    def test_initial_random_show_selection_avoids_active_child_when_possible(self):
        conf = _base_conf()
        first = [_entry("/content/prime/show_a/e01.mp4", 30 * 60, "prime/show_a")]
        second = [_entry("/content/prime/show_b/e01.mp4", 30 * 60, "prime/show_b")]
        third = [_entry("/content/prime/show_c/e01.mp4", 30 * 60, "prime/show_c")]
        _install_entries(conf, first + second + third)
        _install_sequence(conf, "prime1", "prime/show_a", first, strategy="random_show")
        _install_sequence(conf, "prime1", "prime/show_b", second, strategy="random_show")
        _install_sequence(conf, "prime1", "prime/show_c", third, strategy="random_show")
        _install_sequence(conf, "prime2", "prime/show_a", first, strategy="random_show")
        _install_sequence(conf, "prime2", "prime/show_b", second, strategy="random_show")
        _install_sequence(conf, "prime2", "prime/show_c", third, strategy="random_show")

        SequenceAPI.get_next_in_sequence(conf, "prime1", "prime", "random_show")
        SequenceAPI.get_next_in_sequence(conf, "prime2", "prime", "random_show")

        self.assertNotEqual(
            SequenceIO().get_active_sequence("TestTV", "prime1", "prime"),
            SequenceIO().get_active_sequence("TestTV", "prime2", "prime"),
        )

    def test_week_generation_has_no_hard_boundary_drift(self):
        conf = _base_conf()
        prime1_slot = {
            "tags": "prime1",
            "sequence": "prime1",
            "hard_end": "21:00",
        }
        prime2_slot = {
            "tags": "prime2",
            "sequence": "prime2",
            "hard_end": "00:00",
        }
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
            conf[day] = {
                "18": prime1_slot,
                "19": prime1_slot,
                "20": prime1_slot,
                "21": prime2_slot,
                "22": prime2_slot,
                "23": prime2_slot,
            }

        prime1 = [_entry(f"/content/prime1/e{index:02}.mp4", 90 * 60, "prime1") for index in range(1, 20)]
        prime2 = [_entry(f"/content/prime2/e{index:02}.mp4", 60 * 60, "prime2") for index in range(1, 30)]
        offair = [_entry("/content/offair.mp4", 60 * 60, "off_air")]
        _install_entries(conf, prime1 + prime2 + offair)
        _install_sequence(conf, "prime1", "prime1", prime1)
        _install_sequence(conf, "prime2", "prime2", prime2)

        schedule = self._run_fluid(
            conf,
            datetime.datetime(2026, 8, 10, 18),
            datetime.datetime(2026, 8, 17, 0),
        )

        blocks_by_start = {block.start_time: block for block in schedule._blocks}
        for offset in range(7):
            day = datetime.datetime(2026, 8, 10, 18) + datetime.timedelta(days=offset)
            self.assertEqual(blocks_by_start[day].end_time, day + datetime.timedelta(minutes=90))
            self.assertEqual(
                blocks_by_start[day + datetime.timedelta(minutes=90)].end_time,
                day.replace(hour=21),
            )
            self.assertEqual(blocks_by_start[day.replace(hour=21)].end_time, day.replace(hour=22))
            self.assertEqual(blocks_by_start[day.replace(hour=22)].end_time, day.replace(hour=23))
            self.assertEqual(
                blocks_by_start[day.replace(hour=23)].end_time,
                (day + datetime.timedelta(days=1)).replace(hour=0),
            )


if __name__ == "__main__":
    unittest.main()
