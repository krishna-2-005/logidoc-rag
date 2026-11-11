# Carrier registry operations

The carrier registry holds the master data of the carriers a company works with, and decides which carrier
names printed on the documents count as which registered carrier. Sprint 2 adds the management screens and the
paged API they use. The rules from `docs/ENTERPRISE_INTEGRATION_READINESS.md` (section 3) are unchanged.

---

## 1. What an operator can do

| Screen | Address | Who |
|---|---|---|
| Carrier Registry (list) | `/carrier-registry` | everyone who can read exceptions (`exceptions.view`) |
| Carrier detail | `/carrier-registry/{id}` | same; the history is shown to administrators only |
| Name link approvals | `/carrier-registry/links` | same; proposing and deciding need `carriers.manage` |

- **List.** Search by legal or trading name, SCAC, MC or USDOT number (with or without the `MC-` or `USDOT`
  prefix); filter by registration status; sort by any column shown; page through 25 at a time. Empty, loading
  and error states are distinct, and the filters live in the address, so a filtered list can be bookmarked.
- **Register and edit** (administrators). One form with company, identifiers, contact and address. The server
  validates every value and its message is shown as it is:
  - A malformed identifier is refused with its rule (for example "2 to 4 letters"); nothing is cleaned into
    something else.
  - An identifier another carrier holds is refused, naming that carrier.
  - A name equal to an existing carrier's (ignoring case, spaces and punctuation) is refused as a possible
    duplicate. The form then offers a checkbox: "this is a different company". Only with it ticked is the carrier
    registered separately (`confirm_distinct`).
- **Detail.** Master data; the document names linked to the carrier (who proposed and who approved each); the
  requests waiting for it; its shipments, delivery, freight audit and exception figures; recent decisions; and the
  history (every change, with the values before and after).
- **Name links.** Three tabs:
  - *Awaiting approval:* each request with what it would change, the name it is linked to now, the reason, and
    Approve or Reject.
  - *Names on the documents:* every carrier name the current documents print, with the shipments and documents
    it was read from, its link state, and the registered carriers with the same name.
  - *Decided:* approved and rejected requests, with who decided and why.

### The figures on the detail page are Carrier Intelligence's

The detail page does not compute anything. Once a document name is linked, Carrier Intelligence groups those
shipments under `registered:{id}`, and the page reads `GET /api/carriers/registered:{id}`: the same figures,
rates and definitions as the Carrier Intelligence screen. Until a name is linked, the page says that the carrier
has no figures yet.

## 2. Two-person approval

1. An administrator proposes that a document name belongs to a registered carrier (or that a linked name moves
   or is unlinked), with a reason of at least 10 characters.
2. A **different** administrator approves or rejects it. A rejection needs a note.
3. The proposer cannot decide their own request. The screen says so instead of showing the buttons, and the
   server refuses it anyway (403): hiding a button is never the protection.
4. Every step is an event in `carrier_events`, which is append-only (database triggers refuse UPDATE and DELETE).

Names are never merged because they look alike. The "same name registered" hint on the names tab and in the
proposal dialog is only a hint: nothing is linked until two administrators agree.

## 3. Registration status and address (schema version 2)

| Field | Values |
|---|---|
| `status` | `active` (in use), `on_hold` (do not use until checked), `inactive` (no longer used) |
| `address_line1`, `address_line2` | up to 200 characters |
| `city`, `region` | up to 100 characters (`region` is the state or province) |
| `postal_code` | 2 to 12 letters, digits, spaces or hyphens; stored in upper case |
| `country` | two letters (ISO 3166-1 format), stored in upper case; the code itself is not looked up |

### The migration

- `carrier_registry.sqlite3` moves from schema version 1 to 2 **when it is next opened**.
- One transaction: seven `ALTER TABLE ... ADD COLUMN` statements (status defaults to `active`, the address to
  empty), four indexes for the searches and the per-carrier history, and the version number. Either all of it is
  applied or none of it.
- No row is rewritten and no event is touched. The version is read again inside the transaction, so two processes
  opening the store at once apply it once.
- A store written by a newer version is refused (503 with "Update the application"), never read half-understood.
- Backups made before the upgrade restore normally: the restored version 1 store is upgraded when it is opened.
- **This installation:** the real `data/` folder has no carrier registry yet, so there was nothing to migrate.
  The migration was tested on synthetic version 1 stores (`tests/test_carrier_registry_operations.py`),
  including a failing upgrade that leaves version 1 untouched.

## 4. API

All routes are also served under `/api/v1/...` (the same API). Every change needs the `X-LogiDoc-Client` header.

| Method and path | Permission | Answer |
|---|---|---|
| `GET /api/carrier-registry/carriers?search=&status=&sort=&order=&offset=&limit=` | `exceptions.view` | `CarrierPage` |
| `GET /api/carrier-registry/carriers/{id}` | `exceptions.view` | `CarrierRegistryDetail` (history null for non-administrators) |
| `GET /api/carrier-registry/carriers/{id}/events?offset=&limit=` | `carriers.manage` | `RegistryEventPage` |
| `GET /api/carrier-registry/links?state=pending\|decided\|all&carrier_id=&offset=&limit=` | `exceptions.view` | `LinkQueue` |
| `GET /api/carrier-registry/document-names?search=&state=all\|unlinked\|pending\|linked&offset=&limit=` | `exceptions.view` | `DocumentNamePage` |
| `POST /api/carrier-registry` | `carriers.manage` | 201 `RegisteredCarrierRecord`; 409 duplicate; 422 refused value |
| `PATCH /api/carrier-registry/{id}` | `carriers.manage` | `RegisteredCarrierRecord` |
| `POST /api/carrier-registry/links` | `carriers.manage` | 201 `LinkRequest` |
| `POST /api/carrier-registry/links/{id}/decision` | `carriers.manage` (not the proposer) | `LinkRequest` |

- The existing `GET /api/carrier-registry` and `GET /api/carrier-registry/events` keep their shapes.
- `RegisteredCarrierRecord` gained `status` and the address fields, which is additive and stays within v1.
- Sorting accepts only the listed column names; anything else is 422, never passed to SQL.
- Errors are always `{"detail": "..."}`.

## 5. Webhook events from the registry

Committed registry changes become webhook events (`docs/WEBHOOKS_AND_DELIVERY.md`):

- `carrier.created`: the master data as registered.
- `carrier.updated`: each changed field, before and after.
- `carrier_name_link.approved`: the document name, the carrier, the previous carrier, who approved and when.

A proposal and a rejection are not published.

## 6. Limitations

- Identifiers are checked for their format, not against FMCSA or NMFTA.
- No carrier number is read from the documents yet: the link is by the name printed on them.
- The names tab lists the names of the **current** documents (and linked names no document prints any more).
- The registry is managed through the React application. The Streamlit application does not show it, and was not
  changed.
