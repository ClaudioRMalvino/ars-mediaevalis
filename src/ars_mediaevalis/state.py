"""state.json: what was picked today, and which artworks have been shown or rejected."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ars_mediaevalis.paths import state_dir

log = logging.getLogger(__name__)


@dataclass
class State:
    date: str | None = None              # local ISO date of the current artwork
    artwork: dict | None = None          # dataclasses.asdict() of the Artwork
    greeted: bool = False                # whether today's greeting window has been opened
    shown: list[int] = field(default_factory=list)
    rejected: list[int] = field(default_factory=list)


def _path() -> Path:
    return state_dir() / "state.json"


def load() -> State:
    """
    Reads state.json. A corrupt file is moved aside to state.json.bad.

    Returns:
        State: the stored state, or an empty one if there is none or it is unreadable.
    """
    p: Path = _path()
    if not p.exists():
        return State()
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            raise TypeError("state is not a JSON object")
        known: set[str] = {f.name for f in fields(State)}
        return State(**{k: v for k, v in data.items() if k in known})
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        log.error("corrupt state file; moving it aside to %s", p.with_suffix(".json.bad"))
        os.replace(p, p.with_suffix(".json.bad"))
        return State()


def save(state: State) -> None:
    """
    Writes state.json atomically: readers see the old file or the new one, never a mix.

    Args:
        state (State): the state to store.
    """
    p: Path = _path()
    # The temporary file must be in the same directory: rename is only atomic within a filesystem.
    with tempfile.NamedTemporaryFile("w", dir=p.parent, delete=False,
                                     prefix=".state-", suffix=".tmp") as tmp:
        try:
            json.dump(asdict(state), tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            os.unlink(tmp.name)
            raise
    os.replace(tmp.name, p)


@contextmanager
def locked() -> Iterator[None]:
    """
    Holds an exclusive lock for the duration of a run, waiting for any other run to finish.

    The kernel releases a flock when the process exits, so a crash cannot leave a stale lock.
    """
    with open(state_dir() / "lock", "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
