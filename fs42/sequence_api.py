import hashlib
import logging
import os
import random
from fs42.timings import DAYS
from fs42.sequence_io import SequenceIO
from fs42.media_processor import MediaProcessor
from fs42.sequence import NamedSequence, SequenceEntry

class SequenceAPI:
    @staticmethod
    def make_sequence_key(station_config, sequence_name, tag_path, sequence_strategy=None) -> dict:
        if sequence_strategy == "random_show":
            tag_path = SequenceAPI._get_active_child_sequence(
                station_config,
                sequence_name,
                tag_path
            )
        return {"station_name": station_config["network_name"], "sequence_name": sequence_name, "tag_path": tag_path}

    @staticmethod
    def get_sequences_for_station(station_config):
        _l = logging.getLogger("SEQUENCE")
        sio = SequenceIO()
        slist = sio.get_all_sequences_for_station(station_config['network_name'])
        return slist

    @staticmethod
    def get_sequence(station_config, sequence_name, tag_path, sequence_strategy=None) -> NamedSequence:
        _l = logging.getLogger("SEQUENCE")
        sio = SequenceIO()

        if sequence_strategy == "random_show":
            tag_path = SequenceAPI._get_active_child_sequence(
                station_config,
                sequence_name,
                tag_path
            )
        
        seq = sio.get_sequence(station_config["network_name"], sequence_name, tag_path)

        if not seq:
            _l.debug(f"Sequence {sequence_name} for {station_config['network_name']} not found.")
            return None

        return seq

    @staticmethod
    def get_next_in_sequence(station_config, sequence_name, tag_path, sequence_strategy=None) -> SequenceEntry:
        next_entry, _sequence_key = SequenceAPI.get_next_in_sequence_with_key(
            station_config,
            sequence_name,
            tag_path,
            sequence_strategy
        )
        return next_entry

    @staticmethod
    def get_next_in_sequence_with_key(station_config, sequence_name, tag_path, sequence_strategy=None):
        _l = logging.getLogger("SEQUENCE")
        sio = SequenceIO()
        group_parent_tag = tag_path

        if sequence_strategy == "random_show":
            active_tag_path = SequenceAPI._get_active_child_sequence(
                station_config,
                sequence_name,
                group_parent_tag
            )
        else:
            active_tag_path = tag_path

        sequence_key = {
            "station_name": station_config["network_name"],
            "sequence_name": sequence_name,
            "tag_path": active_tag_path
        }

        seq = sio.get_sequence(station_config["network_name"], sequence_name, active_tag_path)
        
        next_entry = None
        if not seq:
            _l.error(f"Sequence {sequence_name} for {station_config['network_name']} not found.")
            return None, sequence_key

        if not seq.episodes:
            _l.error(
                f"Sequence {sequence_name}:{active_tag_path} "
                f"contains no episodes"
            )
            return None, sequence_key

        if seq.current_index < -1:
            seq.current_index = -1
            
        # Handle end of sequence - reset to 0 to loop back to beginning
        if seq.current_index >= seq.end_index:
            _l.info(
                f"Sequence completed: "
                f"{sequence_name}:{seq.tag_path}"
            )
            parent_tag = group_parent_tag

            children = sio.get_child_sequences(
                station_config["network_name"],
                sequence_name,
                parent_tag
            )

            if seq.sequence_strategy == "random_show" and children:

                next_child = SequenceAPI._choose_next_child_sequence(
                    station_config,
                    sequence_name,
                    parent_tag,
                    seq.tag_path
                )

                _l.info(
                    f"Switching sequence from "
                    f"{seq.tag_path} -> {next_child}"
                )

                sio.set_active_sequence(
                    station_config["network_name"],
                    sequence_name,
                    parent_tag,
                    next_child
                )
                sequence_key["tag_path"] = next_child

                next_seq = sio.get_sequence(
                    station_config["network_name"],
                    sequence_name,
                    next_child
                )
                
                next_seq.current_index = 0
                if next_seq.start_index > 0:
                    next_seq.current_index = next_seq.start_index

                if not SequenceAPI._normalize_sequence_position(next_seq):
                    _l.error(
                        f"Child sequence {next_child} "
                        f"contains no episodes"
                    )
                    return None, sequence_key

                next_entry = next_seq.episodes[
                    next_seq.current_index
                ]

                next_seq.current_index += 1

                sio.update_current_index(
                    station_config["network_name"],
                    sequence_name,
                    next_child,
                    next_seq.current_index
                )

                return next_entry, sequence_key
            
            _l.debug(
                f"Current index {seq.current_index} reached end of sequence {sequence_name}. Looping back to 0."
            )
            seq.current_index = 0

        if not SequenceAPI._normalize_sequence_position(seq):
            _l.error(
                f"Sequence {sequence_name}:{active_tag_path} "
                f"contains no episodes"
            )
            return None, sequence_key

        try:
            next_entry = seq.episodes[seq.current_index]
        except IndexError:
            _l.error(f"Error with sequence or tag name")
            _l.error(f"Sequence {sequence_name} for tag {tag_path} failed on current index {seq.current_index}.")
            _l.error("Try rebuilding sequences with --rebuild_sequences.")
            raise RuntimeError()
        seq.current_index += 1
        sio.update_current_index(station_config["network_name"], sequence_name, active_tag_path, seq.current_index)

        return next_entry, sequence_key

    @staticmethod
    def reset_by_episode_path(station_config, sequence_name, tag_path, episode_path):
        _l = logging.getLogger("SEQUENCE")
        sio = SequenceIO()

        # Use the optimized database query instead of loading the entire sequence
        success = sio.update_sequence_index_by_path(
            station_config["network_name"],
            sequence_name,
            tag_path,
            episode_path
        )

        if success:
            _l.info(f"Reset sequence {sequence_name} to episode {episode_path}.")
            return True
        else:
            _l.error(f"Episode path {episode_path} not found in sequence {sequence_name}.")
            return False

    @staticmethod
    def delete_sequences(station_config):
        _l = logging.getLogger("SEQUENCE")
        _l.debug(f"Deleting sequences for {station_config['network_name']}")
        sio = SequenceIO()
        sio.delete_sequences_for_station(station_config["network_name"])
        _l.debug(f"Deleted sequences for {station_config['network_name']}")

    @staticmethod
    def rebuild_sequences(station_config):
        _l = logging.getLogger("SEQUENCE")
        _l.debug(f"Rebuilding sequences for {station_config['network_name']}")
        SequenceAPI.delete_sequences(station_config)
        SequenceAPI.scan_sequences(station_config)
        _l.debug(f"Rebuilt sequences for {station_config['network_name']}")

    @staticmethod
    def scan_sequences(station_config):
        for slot in SequenceAPI._sequence_slots(station_config):
            SequenceAPI._scan_sequence_slot(station_config, slot)

    @staticmethod
    def _sequence_slots(station_config):
        # first, scan normal weekly schedule slots
        for day in DAYS:
            if day in station_config:
                slots = station_config[day]
                if not isinstance(slots, dict):
                    continue

                for slot in slots.values():
                    if isinstance(slot, dict):
                        yield slot

        # now scan date_overrides slots, including override-only sequences
        date_overrides = station_config.get("date_overrides", {})
        if isinstance(date_overrides, dict):
            for override_slots in date_overrides.values():
                if not isinstance(override_slots, dict):
                    continue

                for slot in override_slots.values():
                    if isinstance(slot, dict):
                        yield slot

        # scan week_overrides slots
        week_overrides = station_config.get("week_overrides", {})
        if isinstance(week_overrides, dict):
            for week_schedule in week_overrides.values():
                for day_key in DAYS:
                    if day_key not in week_schedule:
                        continue
                    slots = week_schedule[day_key]
                    if not isinstance(slots, dict):
                        continue
                    for slot in slots.values():
                        if isinstance(slot, dict):
                            yield slot

    @staticmethod
    def _scan_sequence_slot(station_config, slot):
        if "sequence" not in slot or "tags" not in slot:
            return

        # the user supplied sequence name
        if isinstance(slot["tags"], list):
            for tag_index, tag in enumerate(slot["tags"]):
                slot_copy = dict(slot)
                if slot.get("sequence_strategy") == "random_show":
                    slot_tag_array = slot.get('sequence_id_array')
                    slot_tag_index = ""
                    if slot_tag_array is not None:
                        slot_tag_index = f"|{slot_tag_array[tag_index]}"
                    
                    slot_copy["effective_sequence"] = (
                        f"{slot['sequence']}{slot_tag_index}"
                    )

                    SequenceAPI._build_sequence(station_config, tag, slot_copy)
                else:
                    SequenceAPI._build_sequence(station_config, tag, slot)
        else:
            SequenceAPI._build_sequence(station_config, slot["tags"], slot)

    @staticmethod
    def _build_sequence(station_config, this_tag, slot):
        _l = logging.getLogger("SEQUENCE")
        seq_tag = this_tag
        seq_name = slot.get("effective_sequence",slot["sequence"])
        real_tag = seq_tag
        sio = SequenceIO()
        if real_tag in station_config["clip_shows"]:
            _l.error(
                f"Schedule logic error in {station_config['network_name']}: Clip shows are not currently supported as sequences"
            )
            _l.error(f"{seq_tag} is in the clip shows list, but is declared as a sequence on {this_tag} as {seq_name}")
            raise ValueError(
                f"Schedule logic error in {station_config['network_name']}: Clip shows are not currently supported as sequences"
            )

        # check if the sequence already exists

        existing = sio.get_sequence(station_config["network_name"], seq_name, seq_tag)
        
        seq_start = slot.get("sequence_start", 0)
        seq_end = slot.get("sequence_end", 1)

        if slot.get("sequence_strategy") == "random_show":

            seen_child_tags = set()

            base_dir = os.path.join(
                station_config["content_dir"],
                real_tag
            )

            for show_dir in SequenceAPI._find_show_dirs(base_dir):

                relative = os.path.relpath(
                    show_dir,
                    base_dir
                )

                child_tag = (
                    f"{seq_tag}/"
                    f"{relative.replace(os.sep, '/')}"
                )

                seen_child_tags.add(child_tag)

                existing_child = sio.get_sequence(
                    station_config["network_name"],
                    seq_name,
                    child_tag
                )
                if existing_child:
                    sio.update_parent_tag(
                        station_config["network_name"],
                        seq_name,
                        child_tag,
                        seq_tag,
                    )
                sio.update_sequence_strategy(
                    station_config["network_name"],
                    seq_name,
                    child_tag,
                    "random_show",
                )

                file_list = MediaProcessor._rfind_media(show_dir)

                if not file_list:
                    continue

                if not existing_child:

                    ns = NamedSequence(
                        station_config["network_name"],
                        seq_name,
                        child_tag,
                        seq_start,
                        seq_end,
                        0,
                        file_list,
                        False,
                        "random_show",
                        seq_tag
                    )

                    sio.put_sequence(
                        station_config["network_name"],
                        ns
                    )

                else:

                    disk_files = set(str(f) for f in file_list)
                    stored_files = set(
                        entry.fpath
                        for entry in existing_child.episodes
                    )

                    if disk_files != stored_files:

                        current_file = None

                        if (
                            existing_child.current_index
                            < len(existing_child.episodes)
                        ):
                            current_file = existing_child.episodes[
                                existing_child.current_index
                            ].fpath

                        sio.update_sequence_entries(
                            station_config["network_name"],
                            seq_name,
                            child_tag,
                            list(disk_files),
                            current_file,
                            existing_child.current_index
                        )

            # CLEAN UP STALE SHOWS
            existing_children = set(
                sio.get_child_sequences(
                    station_config["network_name"],
                    seq_name,
                    seq_tag
                )
            )

            deleted_children = (
                existing_children
                - seen_child_tags
            )

            for child_tag in deleted_children:

                _l.info(
                    f"Removing stale child sequence "
                    f"{seq_name}:{child_tag}"
                )

                sio.delete_sequence(
                    station_config["network_name"],
                    seq_name,
                    child_tag
                )

            return
        else:
            file_list = MediaProcessor._rfind_media(f"{station_config['content_dir']}/{real_tag}")

        if not existing:
            seq_start = 0
            seq_end = 1
            if "sequence_start" in slot:
                seq_start = slot["sequence_start"]
            if "sequence_end" in slot:
                seq_end = slot["sequence_end"]

            ns = NamedSequence(station_config["network_name"], seq_name, seq_tag, seq_start, seq_end, 0, file_list, False)
            sio.put_sequence(station_config["network_name"], ns)
        else:
            sio.update_sequence_strategy(
                station_config["network_name"],
                seq_name,
                seq_tag,
                None,
            )
            disk_files = set(str(f) for f in file_list)
            stored_files = set(entry.fpath for entry in existing.episodes)
            if disk_files != stored_files:
                new_on_disk = disk_files - stored_files
                removed_from_disk = stored_files - disk_files
                _l.info(f"Content changed for sequence {seq_name}: +{len(new_on_disk)} new, -{len(removed_from_disk)} removed.")
                current_file = None
                if existing.current_index < len(existing.episodes):
                    current_file = existing.episodes[existing.current_index].fpath
                    _l.debug(f"Sequence {seq_name}: current_index={existing.current_index}, current_file={current_file}")
                else:
                    _l.debug(f"Sequence {seq_name}: current_index={existing.current_index} is out of bounds for {len(existing.episodes)} stored episodes")
                sio.update_sequence_entries(
                    station_config["network_name"], seq_name, seq_tag,
                    list(disk_files), current_file, existing.current_index
                )
                
    @staticmethod
    def _choose_next_child_sequence(
        station_config,
        sequence_name,
        parent_tag,
        current_tag_path=None
    ):
        sio = SequenceIO()

        children = sio.get_child_sequences(
            station_config["network_name"],
            sequence_name,
            parent_tag
        )

        if not children:
            return None

        active_children = set(
            sio.get_all_active_sequences(
                station_config["network_name"]
            )
        )
        active_child_identities = set(
            SequenceAPI._child_sequence_identity(
                station_config,
                sequence_name,
                active_child
            )
            for active_child in active_children
        )
        active_child_identities.discard(None)
        current_identity = SequenceAPI._child_sequence_identity(
            station_config,
            sequence_name,
            current_tag_path
        )

        available = []

        for child in children:

            if child == current_tag_path:
                continue

            if child in active_children:
                continue

            child_identity = SequenceAPI._child_sequence_identity(
                station_config,
                sequence_name,
                child
            )

            if (
                child_identity
                and child_identity == current_identity
            ):
                continue

            if (
                child_identity
                and child_identity in active_child_identities
            ):
                continue

            available.append(child)

        # If every child is already active somewhere, fall back to allowing active children.
        if not available:

            available = [
                c
                for c in children
                if (
                    c != current_tag_path
                    and SequenceAPI._child_sequence_identity(
                        station_config,
                        sequence_name,
                        c
                    ) != current_identity
                )
            ]

        # If literally only one child exists, allow it.
        if not available:
            available = children

        return random.choice(available)

    @staticmethod
    def _child_sequence_identity(
        station_config,
        sequence_name,
        tag_path
    ):
        if not tag_path:
            return None

        sio = SequenceIO()
        seq = sio.get_sequence(
            station_config["network_name"],
            sequence_name,
            tag_path
        )

        if seq and seq.episodes:
            episode_dirs = [
                os.path.dirname(
                    os.path.realpath(entry.fpath)
                )
                for entry in seq.episodes
            ]

            try:
                return os.path.commonpath(episode_dirs)
            except ValueError:
                return episode_dirs[0]

        content_dir = station_config.get("content_dir")
        if content_dir:
            return os.path.realpath(
                os.path.join(content_dir, tag_path)
            )

        return os.path.realpath(tag_path)
        
    @staticmethod
    def _get_active_child_sequence(
        station_config,
        sequence_name,
        parent_tag
    ):
        sio = SequenceIO()

        children = sio.get_child_sequences(
            station_config["network_name"],
            sequence_name,
            parent_tag
        )

        if not children:
            return parent_tag

        active_child = sio.get_active_sequence(
            station_config["network_name"],
            sequence_name,
            parent_tag
        )

        if (
            not active_child
            or active_child not in children
        ):
            active_child = SequenceAPI._choose_next_child_sequence(
                station_config,
                sequence_name,
                parent_tag,
            )

            sio.set_active_sequence(
                station_config["network_name"],
                sequence_name,
                parent_tag,
                active_child
            )

        return active_child
        
    @staticmethod
    def _normalize_sequence_position(seq):

        if not seq or not seq.episodes:
            return False

        if seq.current_index < -1:
            seq.current_index = -1

        if seq.current_index >= len(seq.episodes):
            seq.current_index = 0

        return True

    @staticmethod    
    def _find_show_dirs(base_dir):
        show_dirs = []

        if not os.path.isdir(base_dir):
            return show_dirs

        with os.scandir(base_dir) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue

                if not entry.is_dir(follow_symlinks=True):
                    continue

                show_dir = entry.path

                if MediaProcessor._rfind_media(show_dir):
                    show_dirs.append(show_dir)

        return sorted(set(show_dirs))
