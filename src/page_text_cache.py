"""
A persistent cache of the text PyMuPDF reads from each PDF page, so an
unchanged document is not parsed again after a restart or when another
document changes (measured: parsing was 75% of a cold audit).

Identity: the SHA-256 of the file's bytes, with PARSER (the PyMuPDF version
and the way the text is read). So:

- the same content under any name, in any folder, is one entry;
- a replaced document has other bytes, another hash, and is parsed afresh:
  nothing here looks at a name, a size or a modification time;
- a PyMuPDF upgrade, or a change of TEXT_RULE, makes every entry a miss.

What is stored is exactly what the parser returned: [[page number, text]] for
every page, blank ones included, as JSON (never pickle). Classification,
identifiers, chunks and field extraction are computed from it on every audit,
by the same code as without the cache, so results and provenance are identical.

The cache is an optimisation only. It never raises: an unreadable, locked or
damaged cache file, or an entry that does not decode, counts as a miss, and
the document is parsed. A PDF that cannot be parsed is never stored. Entries
of documents no longer present are removed by prune(), and their text is
overwritten in the file (secure_delete). The file holds document text, like
the documents and the search index: it lives in the data folder
(Locations.page_cache_path), is created for its owner only where the platform
has permission bits, is never logged (only an error's type is), and is not
part of a backup (it is rebuilt). Operations: docs/DOCUMENT_TEXT_CACHE.md.
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

import pymupdf

import pdf_access

log = logging.getLogger("logidoc.cache")

# How the text of a page is read (document_loader.extract_pdf, document_extraction.extract_document)
TEXT_RULE = "text-sorted-stripped-1"
PARSER = f"pymupdf-{pymupdf.__version__}|{TEXT_RULE}"

COMMIT_EVERY = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS page_text (
    sha256  TEXT NOT NULL,
    parser  TEXT NOT NULL,
    pages   TEXT NOT NULL,
    PRIMARY KEY (sha256, parser)
)
"""


def restrict(path, mode):
    """
    Owner-only access where the platform has permission bits (POSIX). On
    Windows the file inherits the data folder's access control list, which the
    operator sets (docs/DOCUMENT_TEXT_CACHE.md); chmod changes nothing useful
    there, so nothing is attempted. Never raises.
    """

    if os.name != "posix":
        return

    try:
        os.chmod(path, mode)
    except OSError:
        pass


def describe(path):
    """
    What a cache file holds, without any of its text: {"exists", "bytes",
    "entries", "parsers", "mode"} (mode: the POSIX permission bits, else
    None). Read-only; an unreadable file gives entries None.
    """

    path = Path(path)

    if not path.is_file():
        return {"exists": False, "bytes": 0, "entries": 0, "parsers": [], "mode": None}

    status = path.stat()
    found = {"exists": True, "bytes": status.st_size, "entries": None, "parsers": [],
             "mode": oct(status.st_mode & 0o777) if os.name == "posix" else None}

    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            rows = connection.execute("SELECT parser, COUNT(*) FROM page_text GROUP BY parser ORDER BY parser").fetchall()
        finally:
            connection.close()
        found["entries"] = sum(count for _, count in rows)
        found["parsers"] = [parser for parser, _ in rows]
    except sqlite3.Error:
        pass

    return found


def parse_pages(data):
    """[(page number, text)] of every page of a PDF given as bytes, blank pages included. Raises when it cannot be opened."""

    # PyMuPDF is single-threaded: one document at a time (pdf_access)
    with pdf_access.PDF_LOCK:

        document = pymupdf.open(stream=data, filetype="pdf")

        try:
            return [(number, page.get_text("text", sort=True).strip()) for number, page in enumerate(document, start=1)]
        finally:
            document.close()


class PageTextCache:
    """
    The cache in one SQLite file. Thread-safe; counts hits and misses (stats).
    path None: nothing is kept (every read parses), with the same interface.
    """

    def __init__(self, path=None):

        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.connection = None
        self.disabled = self.path is None
        self.hits = self.misses = self.stored = self.pending = 0
        self.seen = set()

    # --------------------------------------------------------
    # The file (never raises)
    # --------------------------------------------------------

    def open(self):
        """The connection, opened on first use; None when the cache cannot be used."""

        if self.disabled:
            return None

        if self.connection is None:
            try:
                created = not self.path.parent.exists()
                self.path.parent.mkdir(parents=True, exist_ok=True)

                if created:
                    restrict(self.path.parent, 0o700)

                self.connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
                # The text of a removed or replaced document is overwritten in the file, not only unlinked
                self.connection.execute("PRAGMA secure_delete = ON")
                self.connection.execute(SCHEMA)
                self.connection.commit()
                restrict(self.path, 0o600)
            except (sqlite3.Error, OSError) as error:
                self.give_up(error)

        return self.connection

    def give_up(self, error):

        log.warning("The page text cache cannot be used (%s): documents are parsed instead.", type(error).__name__)
        self.disabled = True

        try:
            if self.connection is not None:
                self.connection.close()
        except sqlite3.Error:
            pass

        self.connection = None

    def get(self, digest):

        connection = self.open()

        if connection is None:
            return None

        try:
            row = connection.execute("SELECT pages FROM page_text WHERE sha256 = ? AND parser = ?", (digest, PARSER)).fetchone()
        except sqlite3.Error as error:
            self.give_up(error)
            return None

        if row is None:
            return None

        try:
            pages = json.loads(row[0])
            return [(int(number), str(text)) for number, text in pages]
        except (ValueError, TypeError):
            return None  # an entry that does not decode is a miss, and is written again

    def put(self, digest, pages):

        connection = self.open()

        if connection is None:
            return

        try:
            connection.execute("INSERT OR REPLACE INTO page_text (sha256, parser, pages) VALUES (?, ?, ?)",
                               (digest, PARSER, json.dumps(pages, ensure_ascii=False)))
            self.stored += 1
            self.pending += 1

            # In batches: one commit per document would cost a disk sync each on a cold start
            if self.pending >= COMMIT_EVERY:
                connection.commit()
                self.pending = 0
        except sqlite3.Error as error:
            self.give_up(error)

    # --------------------------------------------------------
    # Reading a document
    # --------------------------------------------------------

    def pages_of(self, data, digest=None):
        """
        [(page number, text)] of every page of the PDF whose bytes are data:
        from the cache when this exact content was parsed before, else parsed
        now and stored. Raises what the parser raises for a damaged file
        (nothing is stored for it).
        """

        digest = digest or hashlib.sha256(data).hexdigest()

        with self.lock:

            self.seen.add(digest)
            pages = self.get(digest)

            if pages is not None:
                self.hits += 1
                return pages

            self.misses += 1

        pages = parse_pages(data)

        with self.lock:
            self.put(digest, pages)

        return pages

    def flush(self):
        """Commits what was stored since the last commit (after a pass over the documents)."""

        with self.lock:

            if self.connection is not None and self.pending:
                try:
                    self.connection.commit()
                    self.pending = 0
                except sqlite3.Error as error:
                    self.give_up(error)

    def prune(self):
        """
        Removes the entries of content not read since the last prune (documents
        removed or replaced), and entries of another parser. Call after a
        complete pass over the documents. Returns how many were removed.
        """

        with self.lock:

            seen, self.seen = self.seen, set()
            connection = self.open()

            if connection is None:
                return 0

            try:
                stored = [row[0] for row in connection.execute("SELECT sha256 FROM page_text WHERE parser = ?", (PARSER,))]
                stale = [(digest,) for digest in stored if digest not in seen]
                connection.executemany("DELETE FROM page_text WHERE sha256 = ? AND parser = ?",
                                       [(digest, PARSER) for (digest,) in stale])
                other = connection.execute("DELETE FROM page_text WHERE parser != ?", (PARSER,)).rowcount
                connection.commit()
                self.pending = 0
            except sqlite3.Error as error:
                self.give_up(error)
                return 0

            return len(stale) + max(other, 0)

    def stats(self):

        return {"hits": self.hits, "misses": self.misses, "stored": self.stored, "enabled": not self.disabled}

    def close(self):

        with self.lock:
            if self.connection is not None:
                try:
                    self.connection.close()
                except sqlite3.Error:
                    pass
                self.connection = None
