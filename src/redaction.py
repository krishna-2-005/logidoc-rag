"""
Removes what must never reach a log line, an alert or an error report:

    credentials       passwords, tokens, API keys, cookies, session values
                      written as name=value or name: value, Bearer tokens,
                      user:password@ in URLs
    URLs              everything after the host (a webhook's path is often
                      its secret)
    file paths        the folders of an absolute Windows, UNC or POSIX path
                      (the file name is kept: "<path>/audit_history.sqlite3")
    e-mail addresses
    long text         cut to MAX_LENGTH characters, so a document's text that
                      reached an exception message cannot fill a log

API routes (/api/...) are not file paths and are kept.
"""

import re

MAX_LENGTH = 2000

PATTERNS = (
    # user:password@host in any URL
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1<credentials>@"),
    # Everything after scheme://host
    (re.compile(r"(?i)\b(https?://[^/\s?#'\"]+)[^\s'\"]*"), r"\1/<redacted>"),
    (re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|authorization|cookie|set-cookie|session|"
        r"access[_-]?key|private[_-]?key|client[_-]?secret|logidoc_session)\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"),
     r"\1\2<redacted>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    # Windows drive and UNC paths: keep the last part. Separators may be doubled: Python's own error messages
    # quote paths with repr ("[WinError 5] Access is denied: 'C:\\Users\\...'")
    (re.compile(r"(?i)(?:\b[a-z]:|\\{2,4}[^\\/\s]+)(?:\\{1,2}|/)(?:[^\\/\s:*?\"'<>|]+(?:\\{1,2}|/))*([^\\/\s:*?\"'<>|]*)"),
     r"<path>/\1"),
    # POSIX absolute paths of two or more parts, except API routes
    (re.compile(r"(?<![\w.:/<>-])/(?!api/)(?:[\w.@+-]+/)+([\w.@+-]*)"), r"<path>/\1"),
)


def redact(text, limit=MAX_LENGTH):
    """The text with credentials, URL paths, file folders and e-mail addresses removed, cut to limit characters."""

    if text is None:
        return None

    text = str(text)

    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)

    if limit and len(text) > limit:
        text = text[:limit] + " ... (truncated)"

    return text
