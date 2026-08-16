import math

from fs42.path_query import PathQuery


def rounded_schedule_duration(duration_seconds, increment_minutes):
    if increment_minutes is None:
        return duration_seconds

    multiple = increment_minutes * 60
    if multiple == 0:
        return duration_seconds

    return multiple * math.ceil(duration_seconds / multiple)


def effective_schedule_increment(station_config, slot_config, tag_str, candidate_path):
    increment = (slot_config or {}).get(
        "schedule_increment",
        station_config["schedule_increment"],
    )

    tag_overrides = station_config.get("tag_overrides")
    if not tag_overrides:
        return increment

    match = PathQuery.match_any_from_base(
        candidate_path,
        station_config["content_dir"],
        tag_overrides.keys(),
    )
    override = None
    if match:
        override = tag_overrides[match]
    elif tag_str in tag_overrides:
        override = tag_overrides[tag_str]

    if override:
        increment = override.get("schedule_increment", increment)

    return increment
