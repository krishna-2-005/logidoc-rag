"""
Cross-process advisory locks on a file, released by the operating system
when the process holding one exits (a crash leaves no stale lock to clean
up). Used for:

    the data folder     the API server holds <data>/.server.lock while it
                        runs, so a backup or a restore from the command line
                        can tell whether the server may be writing
    the backup folder   one backup at a time per backup folder, whatever
                        process starts it (<backups>/.backup.lock)

The lock file itself is never deleted (deleting it while another process
waits on it would let two holders in). It holds no data.

Windows: msvcrt.locking on the first byte; elsewhere: fcntl.flock. Both are
per open file, so two FileLock objects on the same path conflict even inside
one process.
"""

import os
from pathlib import Path


class LockBusy(Exception):
    """Another holder has the lock."""


class FileLock:

    def __init__(self, path):

        self.path = Path(path)
        self.handle = None

    def acquire(self):
        """Takes the lock without waiting; raises LockBusy when it is held."""

        if self.handle is not None:
            raise LockBusy("This lock is already held by this object.")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")

        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise LockBusy(f"{self.path.name} is held by another process or task.") from None

        self.handle = handle

        return self

    def release(self):

        if self.handle is None:
            return

        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

    @property
    def held(self):

        return self.handle is not None

    def __enter__(self):

        return self.acquire()

    def __exit__(self, *exc):

        self.release()


def is_locked(path):
    """Whether another holder has the lock at path (a missing file is not locked; nothing is created then)."""

    if not Path(path).is_file():
        return False

    probe = FileLock(path)

    try:
        probe.acquire()
    except LockBusy:
        return True

    probe.release()

    return False
