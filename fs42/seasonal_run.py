import datetime
import logging
from dataclasses import dataclass

from fs42.schedule_math import effective_schedule_increment, rounded_schedule_duration
from fs42.title_parser import TitleParser


class SeasonalRunUnavailable(Exception):
    pass


class SeasonalRunCompleted(Exception):
    pass


@dataclass(frozen=True)
class EpisodeRun:
    show_tag: str
    season: int
    start_index: int
    end_index: int
    episode_paths: list[str]


@dataclass(frozen=True)
class RunProjection:
    appointment_count: int
    projected_finish: datetime.datetime
    deadline: datetime.datetime
    eligible: bool
    scheduled_seconds: int
    final_appointment_used_seconds: int


def meteorological_season_window(when):
    year = when.year
    boundaries = [
        ("spring", datetime.datetime(year, 3, 1), datetime.datetime(year, 6, 1)),
        ("summer", datetime.datetime(year, 6, 1), datetime.datetime(year, 9, 1)),
        ("autumn", datetime.datetime(year, 9, 1), datetime.datetime(year, 12, 1)),
    ]

    for name, start, end in boundaries:
        if start <= when < end:
            return name, start, end

    if when.month == 12:
        return (
            "winter",
            datetime.datetime(year, 12, 1),
            datetime.datetime(year + 1, 3, 1),
        )

    return (
        "winter",
        datetime.datetime(year - 1, 12, 1),
        datetime.datetime(year, 3, 1),
    )


class SeasonalRunPlanner:
    @staticmethod
    def next_run(sequence):
        _l = logging.getLogger("SEQUENCE")
        if not sequence or not sequence.episodes:
            return None

        index = sequence.current_index
        if index < 0:
            index = 0

        if index >= len(sequence.episodes):
            index = 0

        refs = []
        for entry in sequence.episodes:
            ref = TitleParser.parse_episode_ref(entry.fpath)
            if not ref:
                _l.warning(
                    "[QC] seasonal_random_show: cannot determine season/episode from "
                    f"{entry.fpath}; excluding {sequence.tag_path} from seasonal selection"
                )
                return None
            refs.append(ref)

        season = refs[index].season
        end_index = index
        while end_index < len(refs) and refs[end_index].season == season:
            end_index += 1

        return EpisodeRun(
            show_tag=sequence.tag_path,
            season=season,
            start_index=index,
            end_index=end_index,
            episode_paths=[
                entry.fpath
                for entry in sequence.episodes[index:end_index]
            ],
        )

    @staticmethod
    def run_contains_current_index(sequence, season):
        if not sequence or sequence.current_index >= len(sequence.episodes):
            return False
        index = max(sequence.current_index, 0)
        ref = TitleParser.parse_episode_ref(sequence.episodes[index].fpath)
        return bool(ref and ref.season == season)

    @staticmethod
    def project_run(run, current_mark, slot_config, station_config, catalog):
        seasonal_conf = (slot_config or {}).get("seasonal_run") or {}
        appointment_minutes = seasonal_conf["appointment_minutes"]
        interval_days = seasonal_conf.get("interval_days", 7)
        overflow_days = seasonal_conf.get("overflow_days", 14)
        appointment_capacity = appointment_minutes * 60

        appointments = 0
        remaining = 0
        used_in_final = 0
        scheduled_seconds = 0

        for episode_path in run.episode_paths:
            entry = catalog.entry_by_fpath(episode_path)
            increment = effective_schedule_increment(
                station_config,
                slot_config or {},
                run.show_tag,
                episode_path,
            )
            duration = rounded_schedule_duration(entry.duration, increment)
            if duration > appointment_capacity:
                _, _season_start, season_end = meteorological_season_window(current_mark)
                deadline = season_end + datetime.timedelta(days=overflow_days)
                return RunProjection(0, current_mark, deadline, False, scheduled_seconds, 0)

            if appointments == 0 or duration > remaining:
                appointments += 1
                remaining = appointment_capacity

            remaining -= duration
            used_in_final = appointment_capacity - remaining
            scheduled_seconds += duration

        _, _season_start, season_end = meteorological_season_window(current_mark)
        deadline = season_end + datetime.timedelta(days=overflow_days)
        final_start = current_mark + datetime.timedelta(
            days=(appointments - 1) * interval_days
        )
        projected_finish = final_start + datetime.timedelta(seconds=used_in_final)

        return RunProjection(
            appointment_count=appointments,
            projected_finish=projected_finish,
            deadline=deadline,
            eligible=projected_finish <= deadline,
            scheduled_seconds=scheduled_seconds,
            final_appointment_used_seconds=used_in_final,
        )
