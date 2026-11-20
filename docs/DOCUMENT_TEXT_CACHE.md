# The document text cache

LogiDoc-RAG keeps the text it parses from each PDF page in one file, so an unchanged document is not parsed again
after a restart. That file holds document text. This page says how it is protected, kept small, cleared and
rebuilt.

Code: `src/page_text_cache.py`. Command: `scripts/manage_page_cache.py`. Tests: `tests/test_page_text_cache.py`,
`tests/test_page_cache_security.py`.

## 1. What it is

| | |
|---|---|
| File | `cache/page_text.cache` inside the data folder (`LOGIDOC_DATA_DIR`), with a short-lived `-journal` file beside it while it is written |
| Holds | For each document: the SHA-256 of its bytes, the parser version, and the text of each page. No file name, no shipment, no user |
| Does not hold | Anything the application decides: classifications, extracted fields, audit results and reviews stay in the databases, which hold no document text |
| Sensitivity | The same as the PDFs in `raw/` and the search index in `LOGIDOC_CHROMA_DIR`: whoever can read it can read the documents' text |
| If it is lost | Nothing is lost: it is rebuilt from the PDFs at the next audit, which then takes as long as a first start |

## 2. Who can read it

The cache must be readable only by the account the server runs as.

**Linux and other POSIX systems.** The application creates the folder with mode `0700` and the file with `0600`.
The service unit (`deploy/logidoc-api.service`) already runs as a dedicated user. To check, or repair after
copying files as another user:

```
ls -ld /var/lib/logidoc/data/cache          # drwx------ logidoc logidoc
ls -l  /var/lib/logidoc/data/cache          # -rw------- logidoc logidoc page_text.cache
chown -R logidoc:logidoc /var/lib/logidoc/data/cache
chmod 700 /var/lib/logidoc/data/cache && chmod 600 /var/lib/logidoc/data/cache/page_text.cache
```

**Windows (NTFS).** Windows has no permission bits: the file inherits the access control list of the data
folder, so protect the data folder once and everything in it follows. For a service account named
`LogiDocService`, in an elevated PowerShell:

```
icacls "D:\LogiDocData" /inheritance:r
icacls "D:\LogiDocData" /grant:r "LogiDocService:(OI)(CI)M" "Administrators:(OI)(CI)F" "SYSTEM:(OI)(CI)F"
icacls "D:\LogiDocData\cache"              # check: only those three, all inherited
```

`/inheritance:r` removes the entries inherited from the drive (often *Users* and *Authenticated Users*). Do not
put the data folder inside a user profile, a synchronised folder (OneDrive, Dropbox) or a network share open to
others.

`python scripts/manage_page_cache.py status` prints the file's permissions on POSIX and says when they are not
`0600`.

## 3. Where it must never go

| Place | How it is kept out |
|---|---|
| Git | `.gitignore` lists `data/cache/`. `scripts/check_release.py` fails when it would not be ignored |
| Backups | A backup copies `raw/`, `archive/`, `quarantine/`, the databases and the index only. The cache is not in the manifest, and a restored installation has none (tested) |
| Deployment artifacts | Deploy the code and the frontend build only, never the data folder. The data folder lives outside the release folder in production (`LOGIDOC_DATA_DIR` is an absolute path) |
| Logs | The cache logs one warning when it cannot be used, naming the error's type only. No text, entry or path is logged (tested) |
| API answers | No route returns it or names its location |
| Support bundles | The application has no diagnostic bundle. If you collect files for support by hand, leave out the whole data folder and the index |

## 4. Retention

The cache never grows beyond the documents present:

- every audit reads every document, then removes the entries of content it did not see: a document that was
  removed or replaced is gone from the cache after the next audit;
- removed text is overwritten in the file (`secure_delete`), not only unlinked;
- entries written by another parser version are removed at the same time.

So the retention of the cache follows the retention of the documents: remove a document through the application,
let one audit run (any request after the change), and its text is no longer in the cache. To be certain at once,
clear the cache (section 5).

Disk encryption is recommended for the whole data volume, because the PDFs, the index and this cache all hold
document text: BitLocker on Windows, LUKS (or the provider's volume encryption) on Linux. The application does
not encrypt the cache itself.

## 5. Clearing and rebuilding

```
python scripts/manage_page_cache.py status           # entries, size, parser, permissions; never any text
python scripts/manage_page_cache.py clear --confirm  # remove it
```

1. Stop the server. `clear` takes the data folder's lock and refuses while the server holds it.
2. Run `clear --confirm`. It removes the cache file and its journal only: no document, database or index.
3. Start the server. The first audit parses every document again and rebuilds the cache; results are identical.

Clear the cache when a document was removed for confidentiality and you do not want to wait for the next audit,
before handing a disk or a copy of the data folder to someone else, or when `status` says it is unreadable.

## 6. When it fails

The cache is an optimisation only. If the file is damaged, locked, or cannot be created, the application logs
`The page text cache cannot be used (<error type>): documents are parsed instead.` once, parses the documents, and
gives the same results, more slowly. A damaged file can be cleared as above; nothing else needs repair.
