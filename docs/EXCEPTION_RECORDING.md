# When an exception is recorded

The audit decides which exceptions exist; the review database remembers what people did about them. Since Sprint 4,
reading never writes to the review database. This page says when a newly detected exception is recorded, and the
one delay that follows from it.

## 1. Reading shows, never records

Every read shows a newly detected exception as **open**, from the documents as they are now:

- **in the API:** the exception queue and detail, the overview, shipment detail, carriers, risk, the reports and
  the CSV exports;
- **in the Streamlit app:** Audit Analytics, Audit Operations, the Exception Queue and Administration.

Nothing is written for it:

- the database file is never created, upgraded or locked by a read (`review_store.ReviewView`,
  `review_store.ReviewSession`);
- an exception not yet recorded shows the time the server, or the Streamlit app, first saw it. That time is kept
  in memory and becomes its creation time when it is recorded.

## 2. What records it

| Where | When |
|---|---|
| API server with its background jobs (the default for `src/api/serve.py`, not under `LOGIDOC_ENV=test` or `LOGIDOC_BACKGROUND_JOBS=0`) | within 30 s: the operations loop records whatever the audit detects that the database does not hold yet, and does nothing otherwise |
| Any change to an exception: an API action, or a review saved in the Streamlit app | before the change: every detected exception is recorded, then the change |

Recording an exception writes its `detected` event in the hash-chained review history.

## 3. The eventual consistency that remains

**Until an exception is recorded, it exists only on screen:**

- **No webhook yet.** Its `exception.created` webhook event is created only once the `detected` event exists. With
  the API server's background jobs, that is within about 30 s (plus the collector's own 30 s); without them, only
  when someone changes an exception.
- **No review history yet.** Its history and the activity feed show nothing for it until it is recorded.
- **A creation time kept in memory.** If the server restarts first, the exception is shown with the time of the
  restart.

**The Streamlit app has no background loop.** Used alone, it records detections when a review is saved.

**Reopening works the same way.** A resolved or dismissed exception whose audited values changed is shown as
reopened at once, but recorded as reopened, with its event, at the next recording.

Tests: `tests/test_read_only_requests.py` (API), `tests/test_exception_consistency.py` (webhooks with and without
the background loop, and the Streamlit views).
