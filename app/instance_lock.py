"""Prevent multiple schedulers from using the same local SQLite database."""
import os
from pathlib import Path


class InstanceLock:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith("sqlite") or ":memory:" in database_url:
            raise RuntimeError("Relicta requires a file-backed SQLite database")
        db_path = Path(database_url.split("///", 1)[-1]).resolve()
        self.path = db_path.with_name(db_path.name + ".scheduler.lock")
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("Another Relicta instance is using this database") from exc
        self._file = handle

    def release(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
