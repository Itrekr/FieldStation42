import datetime
import json
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
from fs42.encore_agent import EncoreAgent, EncoreUnavailable
from fs42.liquid_blocks import LiquidBlock
from fs42.liquid_manager import LiquidManager
from fs42.liquid_schedule import LiquidSchedule
from fs42.liquid_api import LiquidAPI
from fs42.marathon_agent import MarathonAgent
from fs42.sequence import NamedSequence
from fs42.sequence_api import SequenceAPI
from fs42.sequence_io import SequenceIO
from fs42.station_manager import StationManager


def _configure_db(tmp_path):
    manager = StationManager()
    manager.server_conf["db_path"] = os.path.join(tmp_path, "fs42.db")
    manager.server_conf["normalize_titles"] = False


def _entry(path, tag="prime"):
    entry = CatalogEntry(path, 60 * 60, tag)
    entry.realpath = os.path.realpath(path)
    return entry


def _station_conf():
    morning = {
        "encore": {
            "source": "prime1",
            "strategy": "queue",
            "cursor": "prime1_morning",
        }
    }
    source = {
        "tags": "prime",
        "sequence": "prime1",
        "sequence_strategy": "random_show",
        "airing_id": "prime1",
    }
    sunday = {
        "encore": {
            "source": "prime1",
            "strategy": "queue",
            "cursor": "prime1_sunday",
        },
        "marathon": {
            "chance": 1.0,
            "count": 6,
        },
    }
    return {
        "network_name": "TestTV",
        "network_type": "standard",
        "content_dir": "/content",
        "clip_shows": {},
        "break_strategy": "standard",
        "commercial_free": True,
        "bump_dir": "bump",
        "schedule_increment": 60,
        "monday": {"6": morning, "7": morning, "18": source, "19": source},
        "tuesday": {"6": morning, "7": morning, "18": source, "19": source},
        "wednesday": {"6": morning, "7": morning, "18": source, "19": source},
        "thursday": {"6": morning, "7": morning, "18": source, "19": source},
        "friday": {"6": morning, "7": morning, "18": source, "19": source},
        "saturday": {"6": morning, "7": morning, "18": source, "19": source},
        "sunday": {"6": sunday},
    }


def _install_catalog_and_sequences(conf, show_a_count=12, show_b_count=12):
    entries = []
    for index in range(1, show_a_count + 1):
        entries.append(_entry(f"/content/prime/show_a/e{index:02}.mp4", "prime/show_a"))
    for index in range(1, show_b_count + 1):
        entries.append(_entry(f"/content/prime/show_b/e{index:02}.mp4", "prime/show_b"))
    entries.append(_entry("/content/offair.mp4", "off_air"))
    CatalogAPI.set_entries(conf, entries)

    sio = SequenceIO()
    sio.put_sequence(
        "TestTV",
        NamedSequence(
            "TestTV",
            "prime1",
            "prime/show_a",
            0,
            1,
            0,
            [entry.path for entry in entries if entry.tag == "prime/show_a"],
            True,
            "random_show",
            "prime",
        ),
    )
    sio.put_sequence(
        "TestTV",
        NamedSequence(
            "TestTV",
            "prime1",
            "prime/show_b",
            0,
            1,
            0,
            [entry.path for entry in entries if entry.tag == "prime/show_b"],
            True,
            "random_show",
            "prime",
        ),
    )
    sio.set_active_sequence("TestTV", "prime1", "prime", "prime/show_a")


class FakeCatalog:
    def __init__(self, entries):
        self.entries = {entry.path: entry for entry in entries}

    def entry_by_fpath(self, fpath):
        return self.entries.get(fpath)


def _block(entry, start):
    return LiquidBlock(
        entry,
        start,
        start + datetime.timedelta(hours=1),
        entry.title,
        "standard",
        {},
    )


def _agent(tmp_path, entries):
    _configure_db(tmp_path)
    conf = {
        "network_name": "TestTV",
        "content_dir": "/content",
        "clip_shows": {},
    }
    return EncoreAgent(conf, FakeCatalog(entries))


def _resolve_and_consume(agent, encore_config, when):
    candidate, key = agent.resolve(encore_config, when)
    agent.record_consumption(key)
    return candidate, key


def _seed_encore_state(station, cursor_name="prime1_sunday"):
    entry = _entry(f"/content/{station}/prime/e01.mp4")
    conf = {
        "network_name": station,
        "content_dir": "/content",
        "clip_shows": {},
    }
    agent = EncoreAgent(conf, FakeCatalog([entry]))
    source_start = datetime.datetime(2026, 8, 9, 18)
    agent.record_airing("prime1", _block(entry, source_start))
    agent.record_consumption({
        "source": "prime1",
        "strategy": "queue",
        "cursor": cursor_name,
        "source_start_time": source_start.isoformat(),
    })
    agent.commit()


def _encore_counts():
    with sqlite3.connect(StationManager().server_conf["db_path"]) as connection:
        cursor = connection.cursor()
        cursor.execute("""
            SELECT station, COUNT(*)
            FROM airing_history
            GROUP BY station
            ORDER BY station
        """)
        history = dict(cursor.fetchall())
        cursor.execute("""
            SELECT station, COUNT(*)
            FROM encore_cursor
            GROUP BY station
            ORDER BY station
        """)
        cursors = dict(cursor.fetchall())
        return history, cursors


class TestEncoreAgent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    @property
    def tmp_path(self):
        return self.tmp.name

    def test_offset_replay_uses_build_local_airing_without_sequence(self):
        mon18 = datetime.datetime(2026, 8, 3, 18)
        tue06 = datetime.datetime(2026, 8, 4, 6)
        e01 = _entry("/content/prime/show_a/e01.mp4")
        agent = _agent(self.tmp_path, [e01])

        agent.record_airing("prime1", _block(e01, mon18))

        candidate, key = agent.resolve(
            {"source": "prime1", "strategy": "offset", "offset": "12h"},
            tue06,
        )

        self.assertEqual(candidate.path, e01.path)
        self.assertEqual(key["source_start_time"], mon18.isoformat())

    def test_offset_missing_source_raises_for_fallback_path(self):
        agent = _agent(self.tmp_path, [])

        with self.assertRaises(EncoreUnavailable):
            agent.resolve(
                {"source": "prime1", "strategy": "offset", "offset": "12h"},
                datetime.datetime(2026, 8, 4, 6),
            )

    def test_offset_continued_progression_replays_prior_evening(self):
        mon18 = datetime.datetime(2026, 8, 3, 18)
        tue06 = datetime.datetime(2026, 8, 4, 6)
        tue18 = datetime.datetime(2026, 8, 4, 18)
        wed06 = datetime.datetime(2026, 8, 5, 6)
        entries = [_entry(f"/content/prime/show_a/e{i:02}.mp4") for i in range(1, 5)]
        agent = _agent(self.tmp_path, entries)

        for index, entry in enumerate(entries[:2]):
            agent.record_airing("prime1", _block(entry, mon18 + datetime.timedelta(hours=index)))
        tue_paths = [
            agent.resolve(
                {"source": "prime1", "strategy": "offset", "offset": "12h"},
                tue06 + datetime.timedelta(hours=index),
            )[0].path
            for index in range(2)
        ]

        for index, entry in enumerate(entries[2:]):
            agent.record_airing("prime1", _block(entry, tue18 + datetime.timedelta(hours=index)))
        wed_paths = [
            agent.resolve(
                {"source": "prime1", "strategy": "offset", "offset": "12h"},
                wed06 + datetime.timedelta(hours=index),
            )[0].path
            for index in range(2)
        ]

        self.assertEqual(tue_paths, [entries[0].path, entries[1].path])
        self.assertEqual(wed_paths, [entries[2].path, entries[3].path])

    def test_offset_replays_odd_show_boundary_and_seasonal_change(self):
        mon18 = datetime.datetime(2026, 9, 21, 18)
        tue06 = datetime.datetime(2026, 9, 22, 6)
        entries = [
            _entry("/content/summer/prime/show_a/e21.mp4", tag="summer/prime/show_a"),
            _entry("/content/autumn/prime/show_b/e01.mp4", tag="autumn/prime/show_b"),
        ]
        agent = _agent(self.tmp_path, entries)

        for index, entry in enumerate(entries):
            agent.record_airing("prime1", _block(entry, mon18 + datetime.timedelta(hours=index)))

        encore_paths = [
            agent.resolve(
                {"source": "prime1", "strategy": "offset", "offset": "12h"},
                tue06 + datetime.timedelta(hours=index),
            )[0].path
            for index in range(2)
        ]

        self.assertEqual(encore_paths, [entry.path for entry in entries])

    def test_queue_persists_progress_and_next_run_starts_after_consumed(self):
        start = datetime.datetime(2026, 8, 3, 18)
        entries = [_entry(f"/content/prime/show_a/e{i:02}.mp4") for i in range(1, 13)]
        agent = _agent(self.tmp_path, entries)
        for index, entry in enumerate(entries):
            agent.record_airing("prime1", _block(entry, start + datetime.timedelta(hours=index)))
        agent.commit()

        sunday = datetime.datetime(2026, 8, 9, 6)
        first_paths = []
        for _ in range(6):
            candidate, _key = _resolve_and_consume(
                agent,
                {"source": "prime1", "strategy": "queue", "cursor": "prime1_sunday"},
                sunday,
            )
            first_paths.append(candidate.path)
        agent.commit()

        next_agent = _agent(self.tmp_path, entries)
        candidate, _key = next_agent.resolve(
            {"source": "prime1", "strategy": "queue", "cursor": "prime1_sunday"},
            sunday + datetime.timedelta(days=7),
        )

        self.assertEqual(first_paths, [entry.path for entry in entries[:6]])
        self.assertEqual(candidate.path, entries[6].path)

    def test_queue_survives_show_change_by_consuming_history_order(self):
        start = datetime.datetime(2026, 8, 3, 18)
        entries = [
            _entry("/content/prime/show_a/e21.mp4"),
            _entry("/content/prime/show_a/e22.mp4"),
            _entry("/content/prime/show_b/e01.mp4"),
        ]
        agent = _agent(self.tmp_path, entries)
        for index, entry in enumerate(entries):
            agent.record_airing("prime1", _block(entry, start + datetime.timedelta(hours=index)))
        agent.commit()

        paths = []
        for _ in range(3):
            candidate, _key = _resolve_and_consume(
                agent,
                {"source": "prime1", "strategy": "queue", "cursor": "prime1_sunday"},
                datetime.datetime(2026, 8, 9, 6),
            )
            paths.append(candidate.path)

        self.assertEqual(paths, [entry.path for entry in entries])

    def test_independent_queue_cursors_share_source_without_sharing_position(self):
        start = datetime.datetime(2026, 8, 3, 18)
        entries = [_entry(f"/content/prime/show_a/e{i:02}.mp4") for i in range(1, 4)]
        agent = _agent(self.tmp_path, entries)
        for index, entry in enumerate(entries):
            agent.record_airing("prime1", _block(entry, start + datetime.timedelta(hours=index)))
        agent.commit()

        a1, _ = _resolve_and_consume(
            agent,
            {"source": "prime1", "strategy": "queue", "cursor": "sunday_a"},
            datetime.datetime(2026, 8, 9, 6),
        )
        a2, _ = _resolve_and_consume(
            agent,
            {"source": "prime1", "strategy": "queue", "cursor": "sunday_a"},
            datetime.datetime(2026, 8, 9, 6),
        )
        b1, _ = _resolve_and_consume(
            agent,
            {"source": "prime1", "strategy": "queue", "cursor": "sunday_b"},
            datetime.datetime(2026, 8, 9, 6),
        )

        self.assertEqual([a1.path, a2.path], [entries[0].path, entries[1].path])
        self.assertEqual(b1.path, entries[0].path)

    def test_queue_does_not_replay_future_source_occurrences(self):
        entry = _entry("/content/prime/show_a/e01.mp4")
        agent = _agent(self.tmp_path, [entry])
        agent.record_airing("prime1", _block(entry, datetime.datetime(2026, 8, 9, 18)))

        with self.assertRaises(EncoreUnavailable):
            agent.resolve(
                {"source": "prime1", "strategy": "queue", "cursor": "prime1_sunday"},
                datetime.datetime(2026, 8, 9, 6),
            )

    def test_reset_cursors_from_blocks_rewinds_deleted_future_encores(self):
        entries = [_entry(f"/content/prime/show_a/e{i:02}.mp4") for i in range(1, 8)]
        agent = _agent(self.tmp_path, entries)
        start = datetime.datetime(2026, 8, 3, 18)
        for index, entry in enumerate(entries):
            agent.record_airing("prime1", _block(entry, start + datetime.timedelta(hours=index)))
        agent.commit()

        sunday = datetime.datetime(2026, 8, 9, 6)
        future_blocks = []
        for _ in range(6):
            candidate, key = _resolve_and_consume(
                agent,
                {"source": "prime1", "strategy": "queue", "cursor": "prime1_sunday"},
                sunday,
            )
            block = _block(candidate, sunday)
            block.encore_key = key
            future_blocks.append(block)
        agent.commit()

        conf = {"network_name": "TestTV", "content_dir": "/content", "clip_shows": {}}
        EncoreAgent.reset_cursors_from_blocks(
            conf,
            future_blocks,
            datetime.datetime(2026, 8, 9),
        )
        next_agent = _agent(self.tmp_path, entries)
        candidate, _key = next_agent.resolve(
            {"source": "prime1", "strategy": "queue", "cursor": "prime1_sunday"},
            datetime.datetime(2026, 8, 9, 6),
        )

        self.assertEqual(candidate.path, entries[0].path)

    def test_reset_airing_history_removes_future_records_before_regeneration(self):
        old_future = _entry("/content/prime/show_a/e05.mp4")
        new_future = _entry("/content/prime/show_b/e01.mp4")
        retained = _entry("/content/prime/show_a/e04.mp4")
        agent = _agent(self.tmp_path, [old_future, retained])
        cutoff = datetime.datetime(2026, 8, 20)
        agent.record_airing("prime1", _block(retained, cutoff - datetime.timedelta(hours=1)))
        agent.record_airing("prime1", _block(old_future, cutoff + datetime.timedelta(hours=18)))
        agent.commit()

        conf = {"network_name": "TestTV", "content_dir": "/content", "clip_shows": {}}
        EncoreAgent.reset_airing_history(conf, cutoff)

        after_reset = _agent(self.tmp_path, [old_future, retained])
        with self.assertRaises(EncoreUnavailable):
            after_reset.resolve(
                {"source": "prime1", "strategy": "offset", "offset": "12h"},
                cutoff + datetime.timedelta(hours=30),
            )

        regen = _agent(self.tmp_path, [new_future, retained])
        regen.record_airing("prime1", _block(new_future, cutoff + datetime.timedelta(hours=18)))
        regen.commit()

        candidate, _key = regen.resolve(
            {"source": "prime1", "strategy": "offset", "offset": "12h"},
            cutoff + datetime.timedelta(hours=30),
        )

        self.assertEqual(candidate.path, new_future.path)

    def test_reset_station_state_clears_history_and_cursors_for_station_only(self):
        _configure_db(self.tmp_path)
        _seed_encore_state("TestTV")
        _seed_encore_state("OtherTV")

        EncoreAgent.reset_station_state({
            "network_name": "TestTV",
            "content_dir": "/content",
            "clip_shows": {},
        })

        history, cursors = _encore_counts()
        self.assertEqual(history, {"OtherTV": 1})
        self.assertEqual(cursors, {"OtherTV": 1})

    def test_delete_sequences_clears_encore_state(self):
        _configure_db(self.tmp_path)
        _seed_encore_state("TestTV")
        _seed_encore_state("OtherTV")
        conf = {
            "network_name": "TestTV",
            "content_dir": "/content",
            "clip_shows": {},
        }
        SequenceIO().put_sequence(
            "TestTV",
            NamedSequence(
                "TestTV",
                "prime1",
                "prime",
                0,
                1,
                0,
                ["/content/prime/e01.mp4"],
                True,
            ),
        )

        SequenceAPI.delete_sequences(conf)

        self.assertEqual(SequenceIO().get_all_sequences_for_station("TestTV"), [])
        history, cursors = _encore_counts()
        self.assertEqual(history, {"OtherTV": 1})
        self.assertEqual(cursors, {"OtherTV": 1})

    def test_reset_schedule_clears_encore_state(self):
        _configure_db(self.tmp_path)
        _seed_encore_state("TestTV")
        station = {
            "network_name": "TestTV",
            "network_type": "standard",
            "_has_schedule": True,
            "content_dir": "/content",
            "clip_shows": {},
        }
        manager = LiquidManager()
        manager.schedules = {"TestTV": []}

        with (
            patch.object(LiquidManager, "reset_sequences", return_value=None),
            patch.object(LiquidAPI, "delete_blocks", return_value=None),
            patch.object(LiquidManager, "reload_schedules", return_value=None),
        ):
            manager.reset_schedule(station)

        history, cursors = _encore_counts()
        self.assertEqual(history, {})
        self.assertEqual(cursors, {})

    def test_failed_queue_resolution_does_not_advance_cursor(self):
        first = _entry("/content/prime/show_a/e01.mp4")
        missing = _entry("/content/prime/show_a/e02.mp4")
        agent = _agent(self.tmp_path, [first])
        start = datetime.datetime(2026, 8, 3, 18)
        agent.record_airing("prime1", _block(first, start))
        agent.record_airing("prime1", _block(missing, start + datetime.timedelta(hours=1)))
        agent.commit()

        config = {"source": "prime1", "strategy": "queue", "cursor": "prime1_sunday"}
        _candidate, key = agent.resolve(config, datetime.datetime(2026, 8, 9, 6))
        agent.record_consumption(key)

        with self.assertRaises(EncoreUnavailable):
            agent.resolve(config, datetime.datetime(2026, 8, 9, 6))
        agent.commit()

        retry_agent = _agent(self.tmp_path, [first, missing])
        candidate, _key = retry_agent.resolve(config, datetime.datetime(2026, 8, 9, 6))

        self.assertEqual(candidate.path, missing.path)

    def test_random_show_completed_child_rolls_to_another_child(self):
        _configure_db(self.tmp_path)
        conf = {"network_name": "TestTV"}
        sio = SequenceIO()
        sio.put_sequence(
            "TestTV",
            NamedSequence(
                "TestTV",
                "prime1",
                "prime/show_a",
                0,
                1,
                2,
                ["/content/prime/show_a/e01.mp4", "/content/prime/show_a/e02.mp4"],
                True,
                "random_show",
                "prime",
            ),
        )
        sio.put_sequence(
            "TestTV",
            NamedSequence(
                "TestTV",
                "prime1",
                "prime/show_b",
                0,
                1,
                0,
                ["/content/prime/show_b/e01.mp4", "/content/prime/show_b/e02.mp4"],
                True,
                "random_show",
                "prime",
            ),
        )
        sio.set_active_sequence("TestTV", "prime1", "prime", "prime/show_a")

        next_entry = SequenceAPI.get_next_in_sequence(conf, "prime1", "prime", "random_show")

        self.assertEqual(next_entry.fpath, "/content/prime/show_b/e01.mp4")
        self.assertEqual(sio.get_active_sequence("TestTV", "prime1", "prime"), "prime/show_b")

    def test_normal_nested_sequence_loops_without_random_show_rollover(self):
        _configure_db(self.tmp_path)
        conf = {"network_name": "TestTV"}
        sio = SequenceIO()
        sio.put_sequence(
            "TestTV",
            NamedSequence(
                "TestTV",
                "prime1",
                "prime/show_a",
                0,
                1,
                2,
                ["/content/prime/show_a/e01.mp4", "/content/prime/show_a/e02.mp4"],
                True,
            ),
        )
        sio.put_sequence(
            "TestTV",
            NamedSequence(
                "TestTV",
                "prime1",
                "prime/show_b",
                0,
                1,
                0,
                ["/content/prime/show_b/e01.mp4", "/content/prime/show_b/e02.mp4"],
                True,
            ),
        )

        next_entry = SequenceAPI.get_next_in_sequence(conf, "prime1", "prime/show_a")

        self.assertEqual(next_entry.fpath, "/content/prime/show_a/e01.mp4")
        self.assertIsNone(sio.get_active_sequence("TestTV", "prime1", "prime"))

    def test_marathon_fill_does_not_mutate_slot(self):
        slot = {
            "encore": {
                "source": "prime1",
                "strategy": "queue",
                "cursor": "prime1_sunday",
            },
            "marathon": {
                "chance": 1.0,
                "count": 3,
            },
        }

        first = MarathonAgent.fill_marathon(slot)
        second = MarathonAgent.fill_marathon(slot)

        self.assertIn("marathon", slot)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)

    def test_fluid_builds_queue_encores_and_multiple_recurring_marathons(self):
        _configure_db(self.tmp_path)
        conf = _station_conf()
        StationManager().stations = [conf]
        _install_catalog_and_sequences(conf)

        def fake_make_plan(block, catalog):
            block.plan = [
                BlockPlanEntry(
                    block.content.path,
                    0,
                    block.content.duration,
                    content_type=block.content.content_type,
                    media_type=block.content.media_type,
                )
            ]

        start = datetime.datetime(2026, 8, 3, 18)
        end = datetime.datetime(2026, 8, 16, 12)
        with (
            unittest.mock.patch.object(LiquidBlock, "make_plan", fake_make_plan),
            unittest.mock.patch.object(CatalogAPI, "update_play_counts", lambda _conf, _entries: None),
        ):
            schedule = LiquidSchedule(conf)
            schedule._fluid(start, end)

        blocks = {
            block.start_time: block
            for block in schedule._blocks
            if block.content and block.content.tag != "off_air"
        }

        def paths(day, hour, count):
            start_time = datetime.datetime(2026, 8, day, hour)
            return [
                blocks[start_time + datetime.timedelta(hours=index)].content.path
                for index in range(count)
            ]

        self.assertEqual(paths(3, 18, 2), [
            "/content/prime/show_a/e01.mp4",
            "/content/prime/show_a/e02.mp4",
        ])
        self.assertEqual(paths(4, 6, 2), [
            "/content/prime/show_a/e01.mp4",
            "/content/prime/show_a/e02.mp4",
        ])
        self.assertEqual(paths(4, 18, 2), [
            "/content/prime/show_a/e03.mp4",
            "/content/prime/show_a/e04.mp4",
        ])
        self.assertEqual(paths(5, 6, 2), [
            "/content/prime/show_a/e03.mp4",
            "/content/prime/show_a/e04.mp4",
        ])
        self.assertEqual(paths(9, 6, 6), [
            f"/content/prime/show_a/e{index:02}.mp4"
            for index in range(1, 7)
        ])
        self.assertEqual(paths(10, 6, 2), [
            "/content/prime/show_a/e11.mp4",
            "/content/prime/show_a/e12.mp4",
        ])
        self.assertEqual(paths(16, 6, 6), [
            f"/content/prime/show_a/e{index:02}.mp4"
            for index in range(7, 13)
        ])

    def test_schema_requires_offset_for_offset_encores_and_cursor_for_queue_encores(self):
        try:
            import jsonschema
        except ModuleNotFoundError:
            self.skipTest("jsonschema is not installed")

        with open("fs42/station_config_schema.json", "r", encoding="utf-8") as schema_file:
            schema = json.load(schema_file)
        slot_schema = schema["$defs"]["timeSlotConfig"]

        jsonschema.validate(
            {"encore": {"source": "prime1", "strategy": "offset", "offset": "12h"}},
            slot_schema,
        )
        jsonschema.validate(
            {"encore": {"source": "prime1", "strategy": "queue", "cursor": "prime1_sunday"}},
            slot_schema,
        )

        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                {"encore": {"source": "prime1", "strategy": "offset"}},
                slot_schema,
            )
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                {"encore": {"source": "prime1", "strategy": "queue"}},
                slot_schema,
            )


if __name__ == "__main__":
    unittest.main()
