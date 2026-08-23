import datetime
import json
import logging
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from fs42.station_manager import StationManager


@dataclass(frozen=True)
class QuantumCursor:
    path: str
    offset: float
    order: list[str] | None = None
    updated_at: str | None = None


class QuantumState:
    """Persistent viewer-time cursors for quantum loop channels."""

    def __init__(self, db_path=None):
        self.db_path = db_path
        self._log = logging.getLogger("QuantumState")

    def _connect(self):
        db_path = self.db_path or StationManager().server_conf["db_path"]
        connection = sqlite3.connect(db_path)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS quantum_state (
                network_name TEXT PRIMARY KEY,
                content_path TEXT NOT NULL,
                offset_seconds REAL NOT NULL,
                order_json TEXT,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.commit()
        return connection

    @staticmethod
    def _valid_order(order):
        return order is None or (
            isinstance(order, list)
            and all(isinstance(path, str) and path for path in order)
            and len(order) == len(set(order))
        )

    def load(self, network_name):
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT content_path, offset_seconds, order_json, updated_at "
                    "FROM quantum_state WHERE network_name = ?",
                    (network_name,),
                ).fetchone()
        except (sqlite3.Error, OSError) as error:
            self._log.warning("Could not load quantum state for %s: %s", network_name, error)
            return None

        if row is None:
            return None

        try:
            path, offset, order_json, updated_at = row
            order = json.loads(order_json) if order_json else None
            offset = float(offset)
            if not isinstance(path, str) or not path or not math.isfinite(offset) or offset < 0:
                raise ValueError("invalid path or offset")
            if not self._valid_order(order):
                raise ValueError("invalid shuffle order")
            return QuantumCursor(path, offset, order, updated_at)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self._log.warning("Invalid quantum state for %s; resetting: %s", network_name, error)
            self.reset(network_name)
            return None

    def save(self, network_name, path, offset, order=None):
        try:
            offset = float(offset)
            if not isinstance(network_name, str) or not network_name:
                raise ValueError("network_name must be a non-empty string")
            if not isinstance(path, str) or not path:
                raise ValueError("path must be a non-empty string")
            if not math.isfinite(offset) or offset < 0:
                raise ValueError("offset must be a finite non-negative number")
            if not self._valid_order(order):
                raise ValueError("order must contain unique non-empty paths")

            updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            order_json = json.dumps(order) if order is not None else None
            with closing(self._connect()) as connection:
                connection.execute(
                    """INSERT INTO quantum_state
                       (network_name, content_path, offset_seconds, order_json, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(network_name) DO UPDATE SET
                           content_path = excluded.content_path,
                           offset_seconds = excluded.offset_seconds,
                           order_json = excluded.order_json,
                           updated_at = excluded.updated_at""",
                    (network_name, path, offset, order_json, updated_at),
                )
                connection.commit()
            return QuantumCursor(path, offset, order, updated_at)
        except (sqlite3.Error, OSError, TypeError, ValueError) as error:
            self._log.warning("Could not save quantum state for %s: %s", network_name, error)
            return None

    def reset(self, network_name):
        try:
            with closing(self._connect()) as connection:
                connection.execute("DELETE FROM quantum_state WHERE network_name = ?", (network_name,))
                connection.commit()
        except (sqlite3.Error, OSError) as error:
            self._log.warning("Could not reset quantum state for %s: %s", network_name, error)
