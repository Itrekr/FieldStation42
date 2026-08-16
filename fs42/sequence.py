import math
import random

from fs42.title_parser import TitleParser


class SequenceEntry:
    def __init__(self, fpath):
        self.fpath = str(fpath)

    def __str__(self):
        return f"SequenceEntry(fpath={self.fpath})"


class NamedSequence:
    def __init__(
        self,
        station_name: str,
        sequence_name: str,
        tag_path: str,
        start_perc: float,
        end_perc: float,
        current_index: int,
        file_list: list[str],
        initialized: bool = False,
        sequence_strategy: str = None,
        parent_tag: str = None,
        shuffle_seed: str = None,
        shuffle_cycle: int = 0
    ):
        self.station_name = station_name
        self.sequence_name = sequence_name
        self.tag_path = tag_path
        self.parent_tag = parent_tag
        self.start_perc = start_perc
        self.end_perc = end_perc
        self.current_index = current_index
        self.initialized = initialized
        self.sequence_strategy = sequence_strategy
        self.episodes = []  # Initialize episodes as an empty list
        self.start_index = 0
        self.end_index = 0
        self.shuffle_seed = shuffle_seed
        self.shuffle_cycle = shuffle_cycle or 0
        self.populate(file_list)  # Populate episodes with the provided file list



    def __str__(self):
        return f"NamedSequence(station={self.station_name}, sequence={self.sequence_name}, tag={self.tag_path}, start={self.start_perc}, end={self.end_perc}, index={self.current_index})"

    def populate(self, file_list):
        self.episodes = []  # Reset the episodes list
        for file in file_list:
            entry = SequenceEntry(file)
            self.episodes.append(entry)

        # explicitly sort normal sequences by file path for alpha-numeric ordering.
        # Shuffle sequences persist their randomized order in the sequence_entries table.
        if self.sequence_strategy == "seasonal_random_show":
            def seasonal_key(entry):
                ref = TitleParser.parse_episode_ref(entry.fpath)
                if not ref:
                    return (float("inf"), float("inf"), entry.fpath)
                return (ref.season, ref.episode, entry.fpath)

            self.episodes = sorted(self.episodes, key=seasonal_key)
        elif self.sequence_strategy != "shuffle":
            self.episodes = sorted(self.episodes, key=lambda entry: entry.fpath)

        self.end_index = math.floor(self.end_perc * (len(self.episodes)))

        if self.start_perc < 0 and not self.initialized:
            self.start_index = 0
            self.current_index = random.randrange(self.start_index,self.end_index)
            self.initialized = True
        elif self.start_perc >= 0 and not self.initialized:
            self.start_index = math.floor(self.start_perc * (len(self.episodes)))
            self.current_index = self.start_index
            self.initialized = True

    def get_series_length(self):
        return len(self._episodes)
