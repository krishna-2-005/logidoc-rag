"""
The document text cache from the command line (docs/DOCUMENT_TEXT_CACHE.md).

    python scripts/manage_page_cache.py status           what the cache holds (never its text)
    python scripts/manage_page_cache.py clear --confirm  remove it; the next audit rebuilds it

The cache (data/cache/page_text.cache under LOGIDOC_DATA_DIR) holds the text
parsed from the documents. Clearing it loses nothing: every entry is rebuilt
from the PDFs at the next audit, which then takes as long as a first start.

clear needs the server stopped: it takes the data folder's lock, like a backup
from the command line, and refuses while the server holds it. It removes the
cache file and its journal only, never a document or a database.

Exit status: 0 done; 1 refused (no --confirm, or the server is running); 2 the file could not be removed.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import config  # noqa: E402
import page_text_cache  # noqa: E402
from audit_service import Locations  # noqa: E402
from backup import server_lock_path  # noqa: E402
from file_lock import FileLock, LockBusy  # noqa: E402

SUFFIXES = ("", "-journal", "-wal", "-shm")


def cache_path():

    return Locations.from_config(config).page_cache_path()


def status(arguments):

    found = page_text_cache.describe(cache_path())

    if not found["exists"]:
        print("No document text cache yet: it is created by the first audit.")
        return 0

    entries = "unreadable (it is ignored, and can be cleared)" if found["entries"] is None else f"{found['entries']:,}"
    print(f"Documents cached: {entries}")
    print(f"Size: {found['bytes'] / 1048576:.1f} MB")
    print(f"Parser: {', '.join(found['parsers']) or 'none'} (this version reads {page_text_cache.PARSER})")

    if found["mode"] is not None:
        print(f"Permissions: {found['mode']}" + ("" if found["mode"] == "0o600" else "  (expected 0o600: chmod 600 the file)"))

    return 0


def clear(arguments):

    if not arguments.confirm:
        print("Nothing removed: add --confirm. The next audit will parse every document again.", file=sys.stderr)
        return 1

    lock = FileLock(server_lock_path(config.DATA_DIR))

    try:
        lock.acquire()
    except LockBusy:
        print("The server (or a backup or restore) is using this data folder: stop it first.", file=sys.stderr)
        return 1

    try:
        path = cache_path()
        removed = 0

        for suffix in SUFFIXES:
            target = Path(str(path) + suffix)

            if target.is_file():
                try:
                    target.unlink()
                    removed += 1
                except OSError as error:
                    print(f"The cache could not be removed ({type(error).__name__}).", file=sys.stderr)
                    return 2
    finally:
        lock.release()

    print("The document text cache was removed." if removed else "There was no document text cache to remove.")
    print("It is rebuilt by the next audit (the first request after the server starts).")

    return 0


def main(argv=None):

    parser = argparse.ArgumentParser(description="Inspect or clear the document text cache.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="what the cache holds").set_defaults(run=status)
    command = commands.add_parser("clear", help="remove the cache (the server must be stopped)")
    command.add_argument("--confirm", action="store_true", help="really remove it")
    command.set_defaults(run=clear)

    arguments = parser.parse_args(argv)

    return arguments.run(arguments)


if __name__ == "__main__":
    sys.exit(main())
