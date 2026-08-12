import datetime
import hashlib
import logging
import os
import random
import calendar

from fs42.media_processor import MediaProcessor
from fs42.sequence import NamedSequence
from fs42.sequence_io import SequenceIO
from fs42.slot_reader import SlotReader


class AutoMarathonAgent:
    sequence_prefix = "auto_marathon"
    seasons = {
        "spring": (3, 4, 5),
        "summer": (6, 7, 8),
        "autumn": (9, 10, 11),
        "winter": (12, 1, 2),
    }

    @staticmethod
    def enabled(conf):
        auto_conf = conf.get("auto_marathons")
        return isinstance(auto_conf, dict) and auto_conf.get("enabled", False) is True

    @staticmethod
    def _conf(conf):
        return conf.get("auto_marathons", {}) if isinstance(conf.get("auto_marathons"), dict) else {}

    @staticmethod
    def season_for(when):
        for season, months in AutoMarathonAgent.seasons.items():
            if when.month in months:
                return season
        return None

    @staticmethod
    def season_year(when):
        if when.month in (1, 2):
            return when.year - 1
        return when.year

    @staticmethod
    def season_id(when):
        return f"{AutoMarathonAgent.season_for(when)}-{AutoMarathonAgent.season_year(when)}"

    @staticmethod
    def _season_bounds(season_id):
        season, year_str = season_id.split("-", 1)
        year = int(year_str)

        if season == "spring":
            return datetime.date(year, 3, 1), datetime.date(year, 5, 31)
        if season == "summer":
            return datetime.date(year, 6, 1), datetime.date(year, 8, 31)
        if season == "autumn":
            return datetime.date(year, 9, 1), datetime.date(year, 11, 30)
        if season == "winter":
            winter_end_year = year + 1
            return (
                datetime.date(year, 12, 1),
                datetime.date(winter_end_year, 2, calendar.monthrange(winter_end_year, 2)[1]),
            )

        raise ValueError(f"Unknown season id {season_id}")

    @staticmethod
    def _rng(*parts):
        material = "|".join(str(part) for part in parts)
        seed = hashlib.sha256(material.encode()).digest()
        return random.Random(seed)

    @staticmethod
    def _start_day_index(conf):
        day = AutoMarathonAgent._conf(conf).get("start_day", "saturday")
        days = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }
        return days.get(str(day).lower(), 5)

    @staticmethod
    def _date_has_override(conf, when_date):
        probe = datetime.datetime.combine(when_date, datetime.time.min)
        return SlotReader._get_date_override(conf, probe) is not None

    @staticmethod
    def eligible_weekends(conf, season_id):
        start, end = AutoMarathonAgent._season_bounds(season_id)
        target_weekday = AutoMarathonAgent._start_day_index(conf)
        respect_overrides = AutoMarathonAgent._conf(conf).get("respect_date_overrides", True)

        current = start
        weekends = []
        while current <= end:
            if current.weekday() == target_weekday:
                if not respect_overrides or not AutoMarathonAgent._date_has_override(conf, current):
                    weekends.append(current)
            current += datetime.timedelta(days=1)

        return weekends

    @staticmethod
    def selected_weekend(conf, season_id):
        weekends = AutoMarathonAgent.eligible_weekends(conf, season_id)
        if not weekends:
            logging.getLogger("Liquid").debug(
                f"[{conf.get('network_name')}] Auto-marathon: no eligible weekends for {season_id}; using normal schedule"
            )
            return None

        count = max(1, int(AutoMarathonAgent._conf(conf).get("events_per_season", 1)))
        rng = AutoMarathonAgent._rng(conf.get("network_name", ""), season_id, "auto_marathon")
        return sorted(rng.sample(weekends, min(count, len(weekends))))

    @staticmethod
    def _season_dir(conf, season):
        root = AutoMarathonAgent._conf(conf).get("root", "marathons")
        return os.path.join(conf["content_dir"], root, season)

    @staticmethod
    def _tag_for(conf, season, franchise_name):
        root = AutoMarathonAgent._conf(conf).get("root", "marathons").strip("/")
        return f"{root}/{season}/{franchise_name}"

    @staticmethod
    def discover_franchises(conf, season):
        season_dir = AutoMarathonAgent._season_dir(conf, season)
        logger = logging.getLogger("Liquid")
        if not os.path.isdir(season_dir):
            logger.debug(
                f"[{conf.get('network_name')}] Auto-marathon: missing {season_dir}; using normal schedule"
            )
            return []

        min_titles = int(AutoMarathonAgent._conf(conf).get("min_titles", 2))
        media_filter = conf.get("media_filter", "video")
        franchises = []

        for name in sorted(os.listdir(season_dir)):
            franchise_dir = os.path.join(season_dir, name)
            if not os.path.isdir(franchise_dir):
                continue

            files = sorted(MediaProcessor._rfind_media(franchise_dir, media_filter))
            if len(files) >= min_titles:
                franchises.append(
                    {
                        "name": name,
                        "tag": AutoMarathonAgent._tag_for(conf, season, name),
                        "path": franchise_dir,
                        "files": files,
                    }
                )

        if not franchises:
            logger.debug(
                f"[{conf.get('network_name')}] Auto-marathon: no eligible franchises in {season_dir}; using normal schedule"
            )

        return franchises

    @staticmethod
    def discover_franchise_tags(conf):
        if not AutoMarathonAgent.enabled(conf):
            return []

        tags = []
        for season in AutoMarathonAgent.seasons:
            tags.extend(franchise["tag"] for franchise in AutoMarathonAgent.discover_franchises(conf, season))
        return tags

    @staticmethod
    def select_franchise(conf, season_id, weekend):
        season = season_id.split("-", 1)[0]
        franchises = AutoMarathonAgent.discover_franchises(conf, season)
        if not franchises:
            return None

        rng = AutoMarathonAgent._rng(
            conf.get("network_name", ""),
            season_id,
            weekend.isoformat(),
            "franchise",
        )
        return rng.choice(franchises)

    @staticmethod
    def should_start(conf, current_mark, slot_config=None, started_events=None):
        if not AutoMarathonAgent.enabled(conf):
            return False

        start_hour = int(AutoMarathonAgent._conf(conf).get("start_hour", 10))
        if current_mark.hour < start_hour:
            return False

        season_id = AutoMarathonAgent.season_id(current_mark)
        weekends = AutoMarathonAgent.selected_weekend(conf, season_id)
        if not weekends or current_mark.date() not in weekends:
            return False

        event_key = current_mark.date().isoformat()
        if started_events and event_key in started_events:
            return False

        if AutoMarathonAgent._conf(conf).get("respect_date_overrides", True):
            if AutoMarathonAgent._date_has_override(conf, current_mark.date()):
                return False

        return True

    @staticmethod
    def build_queue(conf, current_mark, catalog=None):
        season_id = AutoMarathonAgent.season_id(current_mark)
        franchise = AutoMarathonAgent.select_franchise(conf, season_id, current_mark.date())
        if not franchise:
            return []

        event_date = current_mark.date().isoformat()
        sequence_name = f"{AutoMarathonAgent.sequence_prefix}|{event_date}|{franchise['name']}"
        tag = franchise["tag"]
        files = franchise["files"]

        if catalog is not None:
            missing = [fpath for fpath in files if not catalog.entry_by_fpath(fpath)]
            if missing:
                logging.getLogger("Liquid").warning(
                    f"[{conf.get('network_name')}] Auto-marathon: {len(missing)} title(s) from {tag} "
                    f"are missing from the catalog; using normal schedule"
                )
                return []

        SequenceIO().put_sequence(
            conf["network_name"],
            NamedSequence(
                conf["network_name"],
                sequence_name,
                tag,
                0,
                1,
                0,
                files,
                False,
            ),
        )

        logging.getLogger("Liquid").info(
            f"[{conf.get('network_name')}] Auto-marathon: scheduling {len(files)} titles from {tag} on {event_date}"
        )

        return [
            {
                "tags": tag,
                "sequence": sequence_name,
            }
            for _ in files
        ]

    @staticmethod
    def scheduled_event_dates(blocks):
        dates = set()
        for block in blocks or []:
            key = getattr(block, "sequence_key", None)
            if not key:
                continue
            sequence_name = key.get("sequence_name")
            if not isinstance(sequence_name, str):
                continue
            parts = sequence_name.split("|")
            if len(parts) >= 2 and parts[0] == AutoMarathonAgent.sequence_prefix:
                dates.add(parts[1])
        return dates
