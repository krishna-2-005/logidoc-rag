# Dependency security

How known vulnerabilities in LogiDoc-RAG's dependencies are found, assessed and resolved.

## 1. How a vulnerability is reported

The CI job **Dependency vulnerability audit** (`.github/workflows/ci.yml`) runs on every push and pull request,
and every Monday at 06:00 UTC on the default branch:

- **Python:** `pip-audit` (pinned 2.9.0) looks up every version pinned in `requirements.txt` and
  `requirements-dev.txt` in the PyPI and OSV advisories. Nothing of the project is installed.
- **Web application:** `npm audit --omit=dev --audit-level=high` reads `frontend/package-lock.json`. The frontend
  job runs the same check after its build.

A finding fails the job and prints the package, the version, the advisory identifier (PYSEC, GHSA or CVE) and
the versions that fix it. A failed run is the report: nothing is silenced by default.

To check locally, from a separate virtual environment, so the project's own environments are not changed:

```
python -m venv .audit-venv
.audit-venv/Scripts/python -m pip install pip-audit==2.9.0
.audit-venv/Scripts/pip-audit --requirement requirements-dev.txt --no-deps --disable-pip
```

## 2. How a finding is assessed

Within two working days of the failed run, the person who sees it records in the pull request or issue:

1. **What is affected:** the advisory, its severity (CVSS when given), and the vulnerable function or feature.
2. **Whether LogiDoc-RAG reaches it:** is the vulnerable code used, with input an outsider can influence? The
   deployment matters:
   - the API listens on 127.0.0.1 behind the HTTPS proxy;
   - every data operation needs a signed-in session ([API_PERMISSIONS.md](API_PERMISSIONS.md));
   - documents are uploaded by administrators;
   - the language model runs locally (Ollama).
3. **The fix available:** a patch or minor release of the same major version, a major release only, or none.

## 3. How it is resolved

| Situation | Resolution | When |
|---|---|---|
| A fix exists in the same major version | Raise the pin in `requirements.txt` (or the lockfile), run the full test suites on Python 3.10 and 3.12, the frontend checks and the evaluations, and merge | critical or high: within 7 days; medium: 30 days; low: the next dependency update |
| The only fix is a new major version | **Never upgraded automatically.** A separate change, with its own review, the release notes read for breaking changes, and the full validation. Until it is merged, the finding is accepted as below, with the plan | as above, counted from the decision |
| No fix, or the code is not reached | Accepted, as below, if the assessment shows it does not affect LogiDoc-RAG or a mitigation covers it | reviewed every 30 days |

Upgrades are never automated (no bot merges version changes): every pin change goes through review and the
test suites, because a dependency's behaviour change can alter audit results.

## 4. Accepting a finding (the only way to suppress one)

A finding may be ignored in CI only with `--ignore-vuln <ID>` on the `pip-audit` line, and only with an entry in
the table below in the same change, giving:

- the advisory identifier and package;
- why it does not affect LogiDoc-RAG, or what mitigates it;
- who accepted it;
- the date;
- when it is reviewed again (at most 90 days later).

An expired entry is reviewed again before anything else is merged. An npm finding is accepted the same way: an
entry here first, and the audit level is never lowered and no package ignored without one. (Pinning a fixed
version of a transitive package with `overrides` in `frontend/package.json` is a fix, not an acceptance.)

### Accepted findings

| Advisory | Package and version | Why it is accepted | Accepted by, date | Review by |
|---|---|---|---|---|
| (none) | | | | |

## 5. Current state

On 2026-11-15:

- `pip-audit` found no known vulnerability in the 109 pinned Python packages;
- `npm audit --omit=dev` found none in the web application's runtime dependencies.
