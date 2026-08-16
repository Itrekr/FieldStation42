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
    show_identity: str
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
    def show_identity(sequence, log_invalid=False):
        refs = SeasonalRunPlanner._episode_refs(sequence, log_invalid=log_invalid)
        if not refs:
            return None

        for ref in refs:
            if ref and ref.season > 0:
                return SeasonalRunPlanner.normalize_show_identity(ref.show)

        for ref in refs:
            if ref:
                return SeasonalRunPlanner.normalize_show_identity(ref.show)

        return None

    @staticmethod
    def normalize_show_identity(show):
        return " ".join((show or "").split()).casefold()

    @staticmethod
    def _episode_refs(sequence, log_invalid=True):
        _l = logging.getLogger("SEQUENCE")
        if not sequence or not sequence.episodes:
            return None

        refs = []
        seen = set()
        for entry in sequence.episodes:
            ref = TitleParser.parse_episode_ref(entry.fpath)
            if not ref:
                if log_invalid:
                    _l.warning(
                        "[QC] seasonal_random_show: cannot determine season/episode from "
                        f"{entry.fpath}; excluding {sequence.tag_path} from seasonal selection"
                    )
                return None

            if ref.season > 0:
                logical_episode = (ref.season, ref.episode)
                if logical_episode in seen:
                    if log_invalid:
                        _l.warning(
                            "[QC] seasonal_random_show: duplicate season/episode "
                            f"S{ref.season:02}E{ref.episode:02} in {sequence.tag_path}; "
                            "excluding show from seasonal selection"
                        )
                    return None
                seen.add(logical_episode)

            refs.append(ref)

        return refs

    @staticmethod
    def _first_normal_index(refs):
        for index, ref in enumerate(refs):
            if ref.season > 0:
                return index
        return None

    @staticmethod
    def index_for_progress(sequence, progress=None):
        refs = SeasonalRunPlanner._episode_refs(sequence)
        if not refs:
            return None

        if progress and progress.get("completed"):
            return SeasonalRunPlanner._first_normal_index(refs)

        if progress and progress.get("next_season") is not None and progress.get("next_episode") is not None:
            for index, ref in enumerate(refs):
                if (
                    ref.season == int(progress["next_season"])
                    and ref.episode == int(progress["next_episode"])
                ):
                    return index

        if progress and progress.get("next_path"):
            for index, entry in enumerate(sequence.episodes):
                if entry.fpath == progress["next_path"] and refs[index].season > 0:
                    return index

        index = sequence.current_index
        if index < 0:
            index = 0

        while index < len(refs) and refs[index].season == 0:
            index += 1

        if index >= len(refs):
            return None

        return index

    @staticmethod
    def next_run(sequence, progress=None):
        refs = SeasonalRunPlanner._episode_refs(sequence)
        if not refs:
            return None

        index = SeasonalRunPlanner.index_for_progress(sequence, progress)
        if index is None:
            return None

        season = refs[index].season
        if season == 0:
            return None

        end_index = index
        while end_index < len(refs) and refs[end_index].season == season:
            end_index += 1

        return EpisodeRun(
            show_tag=sequence.tag_path,
            show_identity=SeasonalRunPlanner.normalize_show_identity(refs[index].show),
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
    def progress_after_index(sequence, index):
        refs = SeasonalRunPlanner._episode_refs(sequence)
        if not refs:
            return None

        while index < len(refs) and refs[index].season == 0:
            index += 1

        if index >= len(refs):
            identity = SeasonalRunPlanner.show_identity(sequence)
            return {
                "show_identity": identity,
                "next_season": None,
                "next_episode": None,
                "next_path": None,
                "completed": True,
            }

        ref = refs[index]
        return {
            "show_identity": SeasonalRunPlanner.normalize_show_identity(ref.show),
            "next_season": ref.season,
            "next_episode": ref.episode,
            "next_path": sequence.episodes[index].fpath,
            "completed": False,
        }

    @staticmethod
    def appointment_capacity_seconds(current_mark, slot_config):
        seasonal_conf = (slot_config or {}).get("seasonal_run") or {}
        hard_end_value = (slot_config or {}).get("hard_end")
        if hard_end_value:
            parts = hard_end_value.split(":")
            if len(parts) != 2:
                raise ValueError(f"Invalid hard_end value {hard_end_value!r}; expected HH:MM")
            hour = int(parts[0])
            minute = int(parts[1])
            hard_end = current_mark.replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            if hard_end <= current_mark:
                hard_end += datetime.timedelta(days=1)
            return int((hard_end - current_mark).total_seconds())

        if "appointment_minutes" not in seasonal_conf:
            raise ValueError(
                "seasonal_random_show requires seasonal_run.appointment_minutes when hard_end is not set"
            )

        return seasonal_conf["appointment_minutes"] * 60

    @staticmethod
    def project_run(run, current_mark, slot_config, station_config, catalog):
        seasonal_conf = (slot_config or {}).get("seasonal_run") or {}
        interval_days = seasonal_conf.get("interval_days", 7)
        overflow_days = seasonal_conf.get("overflow_days", 14)
        appointment_capacity = SeasonalRunPlanner.appointment_capacity_seconds(
            current_mark,
            slot_config,
        )

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
