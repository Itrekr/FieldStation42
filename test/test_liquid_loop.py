import datetime
import sys
import unittest
from unittest.mock import MagicMock, patch

_ffmpeg_stub = MagicMock()
_ffmpeg_stub.probe = MagicMock()
sys.modules.setdefault("ffmpeg", _ffmpeg_stub)

_moviepy_stub = MagicMock()
sys.modules.setdefault("moviepy", _moviepy_stub)
sys.modules.setdefault("moviepy.editor", _moviepy_stub)

from fs42.catalog_entry import CatalogEntry
from fs42.liquid_blocks import LiquidLoopBlock
from fs42.liquid_schedule import LiquidSchedule


class TestLiquidLoopSchedule(unittest.TestCase):
    def test_flood_builds_one_continuous_block_across_midnight(self):
        clips = [
            CatalogEntry("/content/episode-1.mp4", 60 * 60, "content"),
            CatalogEntry("/content/episode-2.mp4", 60 * 60, "content"),
        ]
        schedule = LiquidSchedule.__new__(LiquidSchedule)
        schedule.conf = {
            "network_name": "LoopTV",
            "network_long_name": "Loop TV",
            "shuffle_loop": False,
        }
        schedule.catalog = MagicMock()
        schedule.catalog.get_all_by_tag.return_value = clips
        schedule._l = MagicMock()
        schedule._load_blocks = MagicMock()
        schedule._blocks = []

        start = datetime.datetime(2026, 8, 23, 23, 30)
        end = datetime.datetime(2026, 8, 24, 1, 30)

        with patch("fs42.liquid_schedule.LiquidAPI.add_blocks") as add_blocks:
            schedule._flood(start, end)

        blocks = add_blocks.call_args.args[1]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].start_time, start)
        self.assertEqual(blocks[0].end_time, end)
        self.assertEqual(
            [entry.path for entry in blocks[0].plan],
            ["/content/episode-1.mp4", "/content/episode-2.mp4"],
        )
        self.assertEqual([entry.duration for entry in blocks[0].plan], [3600, 3600])
        schedule._load_blocks.assert_called_once_with()

    def test_flood_continues_partial_clip_from_previous_schedule_run(self):
        clips = [
            CatalogEntry("/content/episode-1.mp4", 3600, "content"),
            CatalogEntry("/content/episode-2.mp4", 3600, "content"),
            CatalogEntry("/content/episode-3.mp4", 3600, "content"),
        ]
        first_start = datetime.datetime(2026, 8, 23, 0, 0)
        boundary = datetime.datetime(2026, 8, 23, 1, 30)
        previous = LiquidLoopBlock(clips, first_start, boundary)
        previous.make_plan(MagicMock())

        schedule = LiquidSchedule.__new__(LiquidSchedule)
        schedule.conf = {"network_name": "LoopTV", "shuffle_loop": False}
        schedule.catalog = MagicMock()
        schedule.catalog.get_all_by_tag.return_value = clips
        schedule._l = MagicMock()
        schedule._load_blocks = MagicMock()
        schedule._blocks = [previous]

        with patch("fs42.liquid_schedule.LiquidAPI.add_blocks") as add_blocks:
            schedule._flood(boundary, boundary + datetime.timedelta(hours=2))

        plan = add_blocks.call_args.args[1][0].plan
        self.assertEqual(
            [(entry.path, entry.skip, entry.duration) for entry in plan],
            [
                ("/content/episode-2.mp4", 1800, 1800),
                ("/content/episode-3.mp4", 0, 3600),
                ("/content/episode-1.mp4", 0, 1800),
            ],
        )

    def test_flood_advances_after_clip_ending_at_schedule_boundary(self):
        clips = [
            CatalogEntry("/content/episode-1.mp4", 3600, "content"),
            CatalogEntry("/content/episode-2.mp4", 3600, "content"),
        ]
        first_start = datetime.datetime(2026, 8, 23, 0, 0)
        boundary = first_start + datetime.timedelta(hours=1)
        previous = LiquidLoopBlock(clips, first_start, boundary)
        previous.make_plan(MagicMock())

        schedule = LiquidSchedule.__new__(LiquidSchedule)
        schedule.conf = {"network_name": "LoopTV", "shuffle_loop": False}
        schedule.catalog = MagicMock()
        schedule.catalog.get_all_by_tag.return_value = clips
        schedule._l = MagicMock()
        schedule._load_blocks = MagicMock()
        schedule._blocks = [previous]

        with patch("fs42.liquid_schedule.LiquidAPI.add_blocks") as add_blocks:
            schedule._flood(boundary, boundary + datetime.timedelta(minutes=30))

        entry = add_blocks.call_args.args[1][0].plan[0]
        self.assertEqual((entry.path, entry.skip, entry.duration), ("/content/episode-2.mp4", 0, 1800))

    def test_flood_retains_previous_shuffle_order_when_extending(self):
        catalog_clips = [
            CatalogEntry("/content/episode-1.mp4", 3600, "content"),
            CatalogEntry("/content/episode-2.mp4", 3600, "content"),
            CatalogEntry("/content/episode-3.mp4", 3600, "content"),
        ]
        shuffled_clips = [catalog_clips[1], catalog_clips[0], catalog_clips[2]]
        first_start = datetime.datetime(2026, 8, 23, 0, 0)
        boundary = first_start + datetime.timedelta(hours=2)
        previous = LiquidLoopBlock(shuffled_clips, first_start, boundary, shuffle=True)
        previous.make_plan(MagicMock())

        schedule = LiquidSchedule.__new__(LiquidSchedule)
        schedule.conf = {"network_name": "LoopTV", "shuffle_loop": True}
        schedule.catalog = MagicMock()
        schedule.catalog.get_all_by_tag.return_value = catalog_clips
        schedule._l = MagicMock()
        schedule._load_blocks = MagicMock()
        schedule._blocks = [previous]

        with patch("fs42.liquid_schedule.LiquidAPI.add_blocks") as add_blocks:
            schedule._flood(boundary, boundary + datetime.timedelta(minutes=30))

        entry = add_blocks.call_args.args[1][0].plan[0]
        self.assertEqual((entry.path, entry.skip, entry.duration), ("/content/episode-3.mp4", 0, 1800))


if __name__ == "__main__":
    unittest.main()
