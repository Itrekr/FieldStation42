import os
from pathlib import Path


PLAYER_PID_PATH = Path("runtime/player.pid")


def register_player(pid=None):
    """Record the live player process so standalone tools can detect it."""
    PLAYER_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAYER_PID_PATH.write_text(str(pid or os.getpid()), encoding="utf-8")


def unregister_player(pid=None):
    """Remove this player's marker without deleting a newer player's marker."""
    expected_pid = pid or os.getpid()
    try:
        recorded_pid = int(PLAYER_PID_PATH.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return
    if recorded_pid == expected_pid:
        PLAYER_PID_PATH.unlink(missing_ok=True)


def running_player_pid():
    """Return the live player PID, cleaning up a stale marker when possible."""
    try:
        pid = int(PLAYER_PID_PATH.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ProcessLookupError, ValueError):
        PLAYER_PID_PATH.unlink(missing_ok=True)
    except PermissionError:
        return pid
    except OSError:
        pass
    return None
