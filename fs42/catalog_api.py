from fs42.catalog_io import CatalogIO
from fs42.catalog_entry import CatalogEntry
from fs42.timings import DAYS

class CatalogAPI:
    @staticmethod
    def get_summary(station_config):
        entries = CatalogIO().get_catalog_entries(station_config["network_name"])
        duration = sum(entry.duration for entry in entries if entry.duration)
        return {
            "network_name": station_config["network_name"],
            "entry_count": len(entries),
            "total_duration": duration
        }
    
    @staticmethod
    def delete_catalog(station_config):
        CatalogIO().delete_all_entries_for_station(station_config["network_name"])

    @staticmethod
    def set_entries(station_config, entries: list[CatalogEntry]):
        CatalogAPI._preserve_counts(station_config, entries)
        CatalogAPI.delete_catalog(station_config)
        CatalogIO().put_catalog_entries(station_config["network_name"], entries)

    @staticmethod
    def _entry_identity(entry):
        physical_path = entry.realpath or entry.path
        return (entry.tag, physical_path)

    @staticmethod
    def _pooled_tag_groups(station_config):
        groups = []

        def harvest(slot):
            if not isinstance(slot, dict) or not slot.get("pooled_tags"):
                return
            tags = slot.get("tags")
            if isinstance(tags, list) and tags:
                groups.append(set(tags))

        for day in DAYS:
            for slot in station_config.get(day, {}).values():
                harvest(slot)

        for override_slots in station_config.get("date_overrides", {}).values():
            if isinstance(override_slots, dict):
                for slot in override_slots.values():
                    harvest(slot)

        for week_schedule in station_config.get("week_overrides", {}).values():
            if isinstance(week_schedule, dict):
                for day in DAYS:
                    for slot in week_schedule.get(day, {}).values():
                        harvest(slot)

        return groups

    @staticmethod
    def _preserve_counts(station_config, entries):
        old_entries = CatalogIO().get_catalog_entries(station_config["network_name"])
        if not old_entries:
            return

        old_by_identity = {
            CatalogAPI._entry_identity(entry): entry.count
            for entry in old_entries
        }
        old_by_tag = {}
        for entry in old_entries:
            old_by_tag.setdefault(entry.tag, []).append(entry.count)

        pooled_groups = CatalogAPI._pooled_tag_groups(station_config)
        old_by_pool = []
        for group in pooled_groups:
            counts = [
                entry.count
                for entry in old_entries
                if entry.tag in group
            ]
            old_by_pool.append((group, min(counts) if counts else None))

        for entry in entries:
            identity = CatalogAPI._entry_identity(entry)
            if identity in old_by_identity:
                entry.count = old_by_identity[identity]
                continue

            pool_mins = [
                min_count
                for group, min_count in old_by_pool
                if entry.tag in group and min_count is not None
            ]
            if pool_mins:
                entry.count = min(pool_mins)
            elif entry.tag in old_by_tag:
                entry.count = min(old_by_tag[entry.tag])
            else:
                entry.count = 0

    @staticmethod
    def search_entries(station_config, query: str):
        return CatalogIO().search_catalog_entries(station_config["network_name"], query)

    @staticmethod
    def get_entries(station_config):
        return CatalogIO().get_catalog_entries(station_config["network_name"])

    @staticmethod
    def get_by_tag(station_config, tag):
        return CatalogIO().get_by_tag(station_config["network_name"], tag)

    @staticmethod
    def get_by_path(station_config, path):
        return CatalogIO().get_entry_by_path(station_config["network_name"], path)

    @staticmethod
    def update_play_counts(station_config, entries: list[CatalogEntry]):
        # flatten the entries list
        flat = []
        for entry in entries:
            if isinstance(entry, list):
                flat.extend(entry)
            else:
                flat.append(entry)
        CatalogIO().batch_increment_counts(station_config["network_name"], flat)

    @staticmethod
    def get_entry_by_id(entry_id):
        return CatalogIO().entry_by_id(entry_id)

    @staticmethod
    def get_entries_by_ids(entry_ids: list[int]) -> dict[int, CatalogEntry]:
        """
        Batch lookup of catalog entries by IDs.
        Returns a dictionary mapping entry_id -> CatalogEntry for found entries.
        """
        return CatalogIO().entries_by_ids(entry_ids)
    
    @staticmethod
    def find_best_candidates(station_config, tag: str, max_duration: float):
        return CatalogIO().find_best_candidates(station_config["network_name"], tag, max_duration)
