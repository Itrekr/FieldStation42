import hashlib
import logging
import os
import random
import secrets
import time
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

        if sequence_strategy == "shuffle":
            return SequenceAPI._get_next_shuffle_item_with_key(
                station_config,
                sequence_name,
                active_tag_path,
                sequence_key,
            )

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
    def reset_by_sequence_key(station_config, sequence_key, episode_path=None):
        if sequence_key and sequence_key.get("strategy") == "shuffle":
            state = sequence_key.get("shuffle_state")
            if isinstance(state, dict):
                restored = SequenceIO().restore_shuffle_state(
                    station_config["network_name"],
                    sequence_key["sequence_name"],
                    sequence_key["tag_path"],
                    state,
                )
                if restored:
                    logging.getLogger("SEQUENCE").info(
                        f"Restored shuffle sequence {sequence_key['sequence_name']}:{sequence_key['tag_path']} "
                        f"to cycle {state.get('cycle', 0)} position {state.get('position', 0)}."
                    )
                    return True

        if episode_path is None:
            return False

        return SequenceAPI.reset_by_episode_path(
            station_config,
            sequence_key["sequence_name"],
            sequence_key["tag_path"],
            episode_path,
        )

    @staticmethod
    def delete_sequences(station_config):
        from fs42.encore_agent import EncoreAgent

        _l = logging.getLogger("SEQUENCE")
        _l.debug(f"Deleting sequences for {station_config['network_name']}")
        sio = SequenceIO()
        sio.delete_sequences_for_station(station_config["network_name"])
        EncoreAgent.reset_station_state(station_config)
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
        random_show_cache = {}
        for slot in SequenceAPI._sequence_slots(station_config):
            SequenceAPI._scan_sequence_slot(station_config, slot, random_show_cache)

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
    def _scan_sequence_slot(station_config, slot, random_show_cache=None):
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

                    SequenceAPI._build_sequence(
                        station_config,
                        tag,
                        slot_copy,
                        random_show_cache,
                    )
                else:
                    SequenceAPI._build_sequence(
                        station_config,
                        tag,
                        slot,
                        random_show_cache,
                    )
        else:
            SequenceAPI._build_sequence(
                station_config,
                slot["tags"],
                slot,
                random_show_cache,
            )

    @staticmethod
    def _build_sequence(station_config, this_tag, slot, random_show_cache=None):
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

            for show_dir, file_list in SequenceAPI._get_random_show_media(
                base_dir,
                real_tag,
                seq_name,
                random_show_cache,
            ):

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
        elif slot.get("sequence_strategy") == "shuffle":
            file_list = MediaProcessor._rfind_media(f"{station_config['content_dir']}/{real_tag}")
        else:
            file_list = MediaProcessor._rfind_media(f"{station_config['content_dir']}/{real_tag}")

        if slot.get("sequence_strategy") == "shuffle":
            SequenceAPI._build_shuffle_sequence(
                station_config,
                seq_name,
                seq_tag,
                file_list,
                existing,
                seq_start,
                seq_end,
            )
        elif not existing:
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
    def _build_shuffle_sequence(station_config, seq_name, seq_tag, file_list, existing, seq_start, seq_end):
        sio = SequenceIO()
        if not existing or existing.sequence_strategy != "shuffle":
            seed = secrets.token_hex(16)
            order = SequenceAPI._new_shuffle_order(file_list, seed, 0)
            ns = NamedSequence(
                station_config["network_name"],
                seq_name,
                seq_tag,
                seq_start,
                seq_end,
                0,
                order,
                True,
                "shuffle",
                None,
                seed,
                0,
            )
            sio.put_sequence(station_config["network_name"], ns)
            return

        disk_files = set(str(f) for f in file_list)
        stored_order = [entry.fpath for entry in existing.episodes]
        stored_files = set(stored_order)
        if disk_files == stored_files:
            sio.update_sequence_strategy(
                station_config["network_name"],
                seq_name,
                seq_tag,
                "shuffle",
            )
            return

        reconciled_order, reconciled_index = SequenceAPI._reconcile_shuffle_order(
            stored_order,
            existing.current_index,
            disk_files,
            existing.shuffle_seed,
            existing.shuffle_cycle,
        )
        sio.update_shuffle_state(
            station_config["network_name"],
            seq_name,
            seq_tag,
            reconciled_order,
            reconciled_index,
            existing.shuffle_cycle,
            existing.shuffle_seed or secrets.token_hex(16),
        )

    @staticmethod
    def _get_next_shuffle_item_with_key(station_config, sequence_name, tag_path, sequence_key):
        _l = logging.getLogger("SEQUENCE")
        sio = SequenceIO()
        seq = sio.get_sequence(station_config["network_name"], sequence_name, tag_path)

        if not seq:
            _l.warning(f"Shuffle sequence {sequence_name}:{tag_path} missing; rebuilding from catalog.")
            file_list = MediaProcessor._rfind_media(f"{station_config['content_dir']}/{tag_path}")
            SequenceAPI._build_shuffle_sequence(station_config, sequence_name, tag_path, file_list, None, 0, 1)
            seq = sio.get_sequence(station_config["network_name"], sequence_name, tag_path)

        if not seq or not seq.episodes:
            _l.error(f"Shuffle sequence {sequence_name}:{tag_path} contains no episodes")
            return None, sequence_key

        needs_state_persist = False
        if seq.sequence_strategy != "shuffle":
            _l.warning(
                f"Sequence {sequence_name}:{tag_path} is marked {seq.sequence_strategy!r}; "
                f"using shuffle strategy requested by slot."
            )
            seq.sequence_strategy = "shuffle"
            needs_state_persist = True

        if not seq.shuffle_seed:
            seq.shuffle_seed = secrets.token_hex(16)
            needs_state_persist = True

        if needs_state_persist:
            sio.update_shuffle_state(
                station_config["network_name"],
                sequence_name,
                tag_path,
                [entry.fpath for entry in seq.episodes],
                seq.current_index,
                seq.shuffle_cycle,
                seq.shuffle_seed,
            )
            seq = sio.get_sequence(station_config["network_name"], sequence_name, tag_path)

        if seq.current_index < 0 or seq.current_index > len(seq.episodes):
            _l.warning(
                f"Shuffle sequence {sequence_name}:{tag_path} has invalid position "
                f"{seq.current_index}; resetting state."
            )
            file_list = MediaProcessor._rfind_media(f"{station_config['content_dir']}/{tag_path}")
            order = SequenceAPI._new_shuffle_order(file_list, seq.shuffle_seed, seq.shuffle_cycle)
            sio.update_shuffle_state(
                station_config["network_name"],
                sequence_name,
                tag_path,
                order,
                0,
                seq.shuffle_cycle,
                seq.shuffle_seed,
            )
            seq = sio.get_sequence(station_config["network_name"], sequence_name, tag_path)

        if seq.current_index >= len(seq.episodes):
            previous_last = seq.episodes[-1].fpath if seq.episodes else None
            cycle = seq.shuffle_cycle + 1
            file_list = MediaProcessor._rfind_media(f"{station_config['content_dir']}/{tag_path}")
            order = SequenceAPI._new_shuffle_order(file_list, seq.shuffle_seed, cycle, previous_last)
            if not order:
                _l.error(f"Shuffle sequence {sequence_name}:{tag_path} contains no episodes")
                return None, sequence_key

            sio.update_shuffle_state(
                station_config["network_name"],
                sequence_name,
                tag_path,
                order,
                0,
                cycle,
                seq.shuffle_seed,
            )
            seq = sio.get_sequence(station_config["network_name"], sequence_name, tag_path)

        order = [entry.fpath for entry in seq.episodes]
        sequence_key.update(
            {
                "strategy": "shuffle",
                "shuffle_state": {
                    "seed": seq.shuffle_seed,
                    "cycle": seq.shuffle_cycle,
                    "position": seq.current_index,
                    "order": order,
                },
            }
        )

        next_entry = seq.episodes[seq.current_index]
        sio.update_current_index(
            station_config["network_name"],
            sequence_name,
            tag_path,
            seq.current_index + 1,
        )
        return next_entry, sequence_key

    @staticmethod
    def _new_shuffle_order(file_list, seed, cycle, previous_last=None):
        order = [str(f) for f in file_list]
        rng = random.Random(f"{seed}:{cycle}")
        rng.shuffle(order)
        if len(order) > 1 and order[0] == previous_last:
            order[0], order[1] = order[1], order[0]
        return order

    @staticmethod
    def _reconcile_shuffle_order(stored_order, position, current_files, seed, cycle):
        position = max(0, min(position, len(stored_order)))
        played = stored_order[:position]
        remaining = [
            fpath
            for fpath in stored_order[position:]
            if fpath in current_files
        ]
        new_items = list(current_files - set(stored_order))
        rng = random.Random(f"{seed or ''}:{cycle}:reconcile:{len(stored_order)}:{len(current_files)}")
        rng.shuffle(new_items)
        for item in new_items:
            insert_at = rng.randrange(0, len(remaining) + 1) if remaining else 0
            remaining.insert(insert_at, item)
        return played + remaining, position
                
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

        state = sio.get_sequence_group_shuffle_state(
            station_config["network_name"],
            sequence_name,
            parent_tag,
        )
        if not state or not state.get("seed") or not state.get("order"):
            seed = secrets.token_hex(16)
            state = {
                "seed": seed,
                "cycle": 0,
                "order": SequenceAPI._new_random_show_order(
                    children,
                    seed,
                    0,
                    current_tag_path,
                ),
                "position": 0,
            }

        order, position, cycle = SequenceAPI._reconcile_random_show_order(
            state.get("order", []),
            state.get("position", 0),
            children,
            state.get("seed"),
            state.get("cycle", 0),
        )

        if position >= len(order):
            cycle += 1
            order = SequenceAPI._new_random_show_order(
                children,
                state.get("seed"),
                cycle,
                current_tag_path,
            )
            position = 0

        selected, order, position = SequenceAPI._take_next_random_show_child(
            station_config,
            sequence_name,
            parent_tag,
            order,
            position,
            current_tag_path,
        )

        sio.set_sequence_group_shuffle_state(
            station_config["network_name"],
            sequence_name,
            parent_tag,
            selected,
            state.get("seed"),
            cycle,
            order,
            position,
        )

        return selected

    @staticmethod
    def _take_next_random_show_child(
        station_config,
        sequence_name,
        parent_tag,
        order,
        position,
        current_tag_path=None,
    ):
        played = order[:position]
        remaining = order[position:]

        preferred = [
            child
            for child in remaining
            if SequenceAPI._random_show_child_available(
                station_config,
                sequence_name,
                child,
                current_tag_path,
                avoid_active=True,
            )
        ]

        if not preferred:
            preferred = [
                child
                for child in remaining
                if SequenceAPI._random_show_child_available(
                    station_config,
                    sequence_name,
                    child,
                    current_tag_path,
                    avoid_active=False,
                )
            ]

        if not preferred:
            preferred = remaining or order

        selected = preferred[0]
        new_remaining = [child for child in remaining if child != selected]
        new_order = played + [selected] + new_remaining
        return selected, new_order, len(played) + 1

    @staticmethod
    def _random_show_child_available(
        station_config,
        sequence_name,
        child,
        current_tag_path=None,
        avoid_active=True,
    ):
        if child == current_tag_path:
            return False

        sio = SequenceIO()
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

        child_identity = SequenceAPI._child_sequence_identity(
            station_config,
            sequence_name,
            child
        )

        if child_identity and child_identity == current_identity:
            return False

        if not avoid_active:
            return True

        if child in active_children:
            return False

        if child_identity and child_identity in active_child_identities:
            return False

        return True

    @staticmethod
    def _new_random_show_order(children, seed, cycle, previous_child=None):
        order = [str(child) for child in children]
        rng = random.Random(f"{seed}:{cycle}:random_show")
        rng.shuffle(order)
        if len(order) > 1 and order[0] == previous_child:
            order[0], order[1] = order[1], order[0]
        return order

    @staticmethod
    def _reconcile_random_show_order(stored_order, position, children, seed, cycle):
        children_set = set(children)
        position = max(0, min(position or 0, len(stored_order)))
        played = [
            child
            for child in stored_order[:position]
            if child in children_set
        ]
        remaining = [
            child
            for child in stored_order[position:]
            if child in children_set
        ]
        known = set(played + remaining)
        new_children = [child for child in children if child not in known]
        rng = random.Random(f"{seed or ''}:{cycle}:random_show_reconcile:{len(stored_order)}:{len(children)}")
        rng.shuffle(new_children)
        for child in new_children:
            insert_at = rng.randrange(0, len(remaining) + 1) if remaining else 0
            remaining.insert(insert_at, child)
        return played + remaining, len(played), cycle

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

        if active_child and active_child in children:
            SequenceAPI._ensure_random_show_active_child_in_cycle(
                station_config,
                sequence_name,
                parent_tag,
                children,
                active_child,
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
    def _ensure_random_show_active_child_in_cycle(
        station_config,
        sequence_name,
        parent_tag,
        children,
        active_child,
    ):
        if not active_child or active_child not in children:
            return

        sio = SequenceIO()
        state = sio.get_sequence_group_shuffle_state(
            station_config["network_name"],
            sequence_name,
            parent_tag,
        )

        if not state or not state.get("seed") or not state.get("order"):
            seed = secrets.token_hex(16)
            remaining = [child for child in children if child != active_child]
            rng = random.Random(f"{seed}:0:random_show_migration")
            rng.shuffle(remaining)
            sio.set_sequence_group_shuffle_state(
                station_config["network_name"],
                sequence_name,
                parent_tag,
                active_child,
                seed,
                0,
                [active_child] + remaining,
                1,
            )
            return

        order, position, cycle = SequenceAPI._reconcile_random_show_order(
            state.get("order", []),
            state.get("position", 0),
            children,
            state.get("seed"),
            state.get("cycle", 0),
        )

        if active_child not in order[:position]:
            played = order[:position]
            remaining = [
                child
                for child in order[position:]
                if child != active_child
            ]
            order = played + [active_child] + remaining
            position = len(played) + 1

        sio.set_sequence_group_shuffle_state(
            station_config["network_name"],
            sequence_name,
            parent_tag,
            active_child,
            state.get("seed"),
            cycle,
            order,
            position,
        )
        
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
    def _get_random_show_media(
        base_dir,
        tag=None,
        sequence_name=None,
        random_show_cache=None,
    ):
        _l = logging.getLogger("SEQUENCE")
        cache_key = os.path.realpath(base_dir)

        if random_show_cache is not None and cache_key in random_show_cache:
            if sequence_name:
                _l.info(
                    f"Using cached random_show media for sequence "
                    f"'{sequence_name}'"
                )
            return random_show_cache[cache_key]

        if tag:
            _l.info(
                f"Scanning random_show media for tag "
                f"'{tag}'..."
            )

        start_time = time.monotonic()
        show_media = []

        if not os.path.isdir(base_dir):
            if random_show_cache is not None:
                random_show_cache[cache_key] = show_media
            return show_media

        show_dirs = []

        with os.scandir(base_dir) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue

                if not entry.is_dir(follow_symlinks=True):
                    continue

                show_dirs.append(entry.path)

        for show_dir in sorted(show_dirs):
            file_list = MediaProcessor._rfind_media(show_dir)
            if file_list:
                show_media.append((show_dir, file_list))

        elapsed = time.monotonic() - start_time
        media_count = sum(len(file_list) for _, file_list in show_media)

        _l.info(
            f"Found {len(show_media)} shows / {media_count} media files "
            f"in {elapsed:.1f}s"
        )

        if random_show_cache is not None:
            random_show_cache[cache_key] = show_media

        return show_media

    @staticmethod
    def _find_show_dirs(base_dir):
        return [
            show_dir
            for show_dir, _file_list in SequenceAPI._get_random_show_media(base_dir)
        ]
