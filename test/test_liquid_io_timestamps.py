import asyncio
import datetime
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

from fs42.block_plan import BlockPlanEntry
from fs42.catalog_api import CatalogAPI
from fs42.catalog_entry import CatalogEntry
from fs42.liquid_blocks import LiquidBlock
from fs42.liquid_io import LiquidIO
from fs42.station_manager import StationManager


class TestLiquidIOTimestampQueries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        manager = StationManager()
        manager.server_conf["db_path"] = os.path.join(self.tmp.name, "fs42.db")
        manager.server_conf["normalize_titles"] = False
        self.station = {
            "network_name": "ABC",
            "network_type": "standard",
            "content_dir": "/content",
        }
        self.other_station = {
            "network_name": "XYZ",
            "network_type": "standard",
            "content_dir": "/content",
        }

        entries = [
            self._entry("/content/abc/early.mp4", "abc"),
            self._entry("/content/abc/programme.mp4", "abc"),
            self._entry("/content/abc/late.mp4", "abc"),
            self._entry("/content/xyz/programme.mp4", "xyz"),
        ]
        CatalogAPI.set_entries(self.station, entries[:3])
        CatalogAPI.set_entries(self.other_station, entries[3:])

        self.io = LiquidIO()
        self.programme_start = datetime.datetime(2026, 8, 10, 18)
        self.programme_end = datetime.datetime(2026, 8, 10, 19)
        self._write_blocks()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _entry(path, tag):
        entry = CatalogEntry(path, 3600, tag)
        entry.realpath = os.path.realpath(path)
        return entry

    def _catalog_entry(self, station, path):
        entry = CatalogAPI.get_by_path(station, path)
        self.assertIsNotNone(entry)
        return entry

    def _block(self, station, path, start, end):
        entry = self._catalog_entry(station, path)
        block = LiquidBlock(entry, start, end, entry.title, "standard", {})
        block.plan = [BlockPlanEntry(entry.path, 0, entry.duration)]
        return block

    def _write_blocks(self):
        early = self._block(
            self.station,
            "/content/abc/early.mp4",
            datetime.datetime(2026, 8, 10, 16),
            datetime.datetime(2026, 8, 10, 17, 30),
        )
        programme = self._block(
            self.station,
            "/content/abc/programme.mp4",
            self.programme_start,
            self.programme_end,
        )
        late = self._block(
            self.station,
            "/content/abc/late.mp4",
            datetime.datetime(2026, 8, 10, 19, 30),
            datetime.datetime(2026, 8, 10, 20, 30),
        )
        other = self._block(
            self.other_station,
            "/content/xyz/programme.mp4",
            self.programme_start,
            self.programme_end,
        )

        self.io.put_liquid_blocks("ABC", [early, programme, late])
        self.io.put_liquid_blocks("XYZ", [other])

    def _paths(self, blocks):
        return [block.content.path for block in blocks]

    def test_datetime_range_returns_overlapping_block(self):
        blocks = self.io.query_liquid_blocks(
            "ABC",
            datetime.datetime(2026, 8, 10, 17, 30),
            datetime.datetime(2026, 8, 10, 19, 30),
        )

        self.assertEqual(self._paths(blocks), ["/content/abc/programme.mp4"])

    def test_iso_t_string_range_returns_overlapping_block(self):
        blocks = self.io.query_liquid_blocks(
            "ABC",
            "2026-08-10T17:30:00",
            "2026-08-10T19:30:00",
        )

        self.assertEqual(self._paths(blocks), ["/content/abc/programme.mp4"])

    def test_space_separated_string_range_returns_overlapping_block(self):
        blocks = self.io.query_liquid_blocks(
            "ABC",
            "2026-08-10 17:30:00",
            "2026-08-10 19:30:00",
        )

        self.assertEqual(self._paths(blocks), ["/content/abc/programme.mp4"])

    def test_all_station_query_normalizes_datetime_range(self):
        by_station = self.io.query_all_liquid_blocks(
            datetime.datetime(2026, 8, 10, 17, 30),
            datetime.datetime(2026, 8, 10, 19, 30),
        )

        self.assertEqual(self._paths(by_station["ABC"]), ["/content/abc/programme.mp4"])
        self.assertEqual(self._paths(by_station["XYZ"]), ["/content/xyz/programme.mp4"])

    def test_all_station_query_normalizes_space_separated_string_range(self):
        by_station = self.io.query_all_liquid_blocks(
            "2026-08-10 17:30:00",
            "2026-08-10 19:30:00",
        )

        self.assertEqual(self._paths(by_station["ABC"]), ["/content/abc/programme.mp4"])
        self.assertEqual(self._paths(by_station["XYZ"]), ["/content/xyz/programme.mp4"])

    def test_block_ending_exactly_at_query_start_is_excluded(self):
        blocks = self.io.query_liquid_blocks(
            "ABC",
            datetime.datetime(2026, 8, 10, 17, 30),
            datetime.datetime(2026, 8, 10, 18),
        )

        self.assertEqual(blocks, [])

    def test_block_starting_exactly_at_query_end_is_excluded(self):
        blocks = self.io.query_liquid_blocks(
            "ABC",
            datetime.datetime(2026, 8, 10, 17, 45),
            datetime.datetime(2026, 8, 10, 18),
        )

        self.assertEqual(blocks, [])

    def test_station_schedule_api_returns_blocks_for_iso_query(self):
        try:
            from fs42.fs42_server.api import schedules as schedules_api
        except ModuleNotFoundError as e:
            if e.name == "fastapi":
                self.skipTest("fastapi is not installed")
            raise

        station_manager = MagicMock()
        station_manager.station_by_name.return_value = self.station

        with unittest.mock.patch.object(schedules_api, "StationManager", return_value=station_manager):
            response = asyncio.run(
                schedules_api.get_schedule(
                    "ABC",
                    start="2026-08-10T18:00:00",
                    end="2026-08-10T22:00:00",
                )
            )

        self.assertEqual(response["network_name"], "ABC")
        self.assertEqual(
            [block.content.path for block in response["schedule_blocks"]],
            ["/content/abc/programme.mp4", "/content/abc/late.mp4"],
        )

    def test_all_schedules_api_returns_blocks_for_iso_query(self):
        try:
            from fs42.fs42_server.api import schedules as schedules_api
        except ModuleNotFoundError as e:
            if e.name == "fastapi":
                self.skipTest("fastapi is not installed")
            raise

        response = schedules_api.get_all_schedules(
            start="2026-08-10T18:00:00",
            end="2026-08-10T22:00:00",
        )

        self.assertEqual(
            [listing["title"] for listing in response["schedules"]["ABC"]],
            ["programme", "late"],
        )
        self.assertEqual(
            [listing["title"] for listing in response["schedules"]["XYZ"]],
            ["programme"],
        )


if __name__ == "__main__":
    unittest.main()
