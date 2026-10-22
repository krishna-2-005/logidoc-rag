"""
LogiDoc-RAG historical trends and carrier scorecards

Reads the recorded audit history (audit_history.HistoryStore): what the
audit found each time a shipment was audited. It never reads documents
and never recalculates an audit. It is separate from the current-state
analytics (audit_analytics), which show the documents as they are now;
everything here is "what was recorded, and when".

What is counted

    An observation is one shipment seen by one recorded audit run
    (HistoryStore.observations). A run that found a shipment unchanged
    still observed it, so an unchanged shipment counts in every period it
    was audited in. Within a period a shipment counts once, with its
    latest observation in that period.

    Periods are calendar weeks (ISO), months or quarters of the time the
    audit ran, not of the delivery. Nothing is inferred for a period with
    no audit run: it is simply absent.

Rates always come with their sample size

    rate = count / n, with the 95% Wilson interval. n counts only the
    shipments for which the question could be answered. A shipment whose
    value is missing, conflicting or unreadable (NEEDS REVIEW, not
    reconciled) is never counted as on time, complete or correct: it is
    left out of that rate's n and reported in needs_review.

    delay rate          DELAYED / (ON TIME + EARLY + DELAYED)
    shortage rate       BOL pieces above POD pieces / (quantity MATCH + MISMATCH)
    invoice mismatch    invoice MISMATCH / (invoice MATCH + MISMATCH)
    discrepancy rate    DISCREPANCIES DETECTED / (DISCREPANCIES DETECTED +
                        NO DISCREPANCIES)
    needs review        NEEDS REVIEW or not reconciled / all shipments

Money

    The invoice variance is an arithmetic mismatch: the stated total
    differs from the sum of the line items. It is not proven overbilling
    (that would need an agreed rate, which the history does not have) and
    not a recoverable amount. Amounts are added only within one currency;
    a currency that was not stated on the documents is its own group
    ("Not stated") and is never merged with a stated one.

Carrier scorecards

    Based on the carrier the documents name. A carrier with fewer than
    MIN_SAMPLE decided shipments is shown with its figures but is not
    ranked. Carriers are ranked by discrepancy rate, lowest first; when the
    intervals of the ranked carriers all overlap, the ranking says the
    differences may be chance.
"""

import re
from collections import defaultdict
from datetime import date, datetime
from math import sqrt

from audit_analytics import cell, write_csv
from audit_history import compare


NOT_STATED = "Not stated"
NOT_RECONCILED = "NOT RECONCILED"

MIN_SAMPLE = 5

PERIODS = ("week", "month", "quarter")

ON_TIME_STATUSES = ("ON TIME", "EARLY", "DELAYED")
DECIDED_STATUSES = ("DISCREPANCIES DETECTED", "NO DISCREPANCIES")

# 1.96: the 95% interval
Z = 1.96

METRICS = {
    "delay_rate": "Delayed deliveries / deliveries with a known date comparison (ON TIME, EARLY or DELAYED).",
    "shortage_rate": "Shipments with fewer pieces delivered than on the BOL / shipments whose quantity was compared.",
    "invoice_mismatch_rate": "Invoices whose line items do not add up to the stated total / invoices that could be checked.",
    "discrepancy_rate": "Shipments with a confirmed discrepancy / shipments that were decided (a confirmed discrepancy, or none).",
    "needs_review_rate": "Shipments that needed review or could not be reconciled / all shipments in the group.",
    "arithmetic_variance": (
        "The difference between the stated invoice total and its line items, for invoices that do not add up. "
        "An arithmetic mismatch, not proven overbilling and not a recoverable amount."
    ),
    "n": "The number of shipments the rate is based on. A missing value is never counted as 0 or as on time.",
}

OVERBILLING_NOTE = (
    "Invoice variances are arithmetic mismatches (the total differs from the line items). They are not "
    "proof of overbilling: that needs an agreed rate, which the audit history does not hold."
)

SCORECARD_NOTE = (
    "Carriers with fewer than {minimum} decided shipments are shown but not ranked: a few shipments cannot "
    "tell carriers apart."
)

OVERLAP_NOTE = (
    "The intervals of the ranked carriers overlap: the differences between them may be chance."
)

SERIES_COLUMNS = (
    "period", "shipments", "delay_n", "delayed", "delay_rate", "delay_low", "delay_high",
    "shortage_n", "shortages", "shortage_rate", "invoice_n", "invoice_mismatches",
    "invoice_mismatch_rate", "decided_n", "discrepancies", "discrepancy_rate",
    "needs_review", "currency", "arithmetic_mismatches", "net_variance", "absolute_variance",
)

CARRIER_COLUMNS = (
    "rank", "carrier", "shipments", "decided_n", "discrepancies", "discrepancy_rate",
    "interval_low", "interval_high", "delay_n", "delay_rate", "shortage_n", "shortage_rate",
    "invoice_n", "invoice_mismatch_rate", "needs_review", "sample", "note",
)

CATEGORY_COLUMNS = (
    "category", "shipments_affected", "audit_observations", "shipments_repeating", "carriers",
)


# ============================================================
# ROWS
# ============================================================

def parse_time(text):

    moment = datetime.fromisoformat(text)

    return moment


def carrier_key(carrier):

    return re.sub(r"\s+", " ", carrier).casefold() if carrier else None


def rows_from(observations):
    """
    Plain rows from HistoryStore.observations(). Values the audit did
    not have stay None.
    """

    rows = []

    for item in observations:

        reconciled = bool(item.get("reconciled"))
        observed = parse_time(item["observed_at"])

        rows.append({
            "snapshot_id": item["id"],
            "run_id": item.get("run_id"),
            "shipment_key": item["shipment_key"],
            "shipment_id": item["shipment_id"],
            "observed_at": observed,
            "date": observed.date(),
            "carrier": item.get("carrier"),
            "currency": item.get("currency"),
            "reconciled": reconciled,
            "final_status": item.get("final_status") if reconciled else NOT_RECONCILED,
            "quantity_status": item.get("quantity_status"),
            "delivery_status": item.get("delivery_status"),
            "invoice_status": item.get("invoice_status"),
            "piece_difference": item.get("piece_difference"),
            "days_late": item.get("days_late"),
            "invoice_total": item.get("invoice_total"),
            "invoice_difference": item.get("invoice_difference"),
            "categories": sorted({finding["category"] for finding in item.get("findings", [])}),
        })

    return rows


def period_key(day, period):

    if period == "week":
        iso = day.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    if period == "quarter":
        return f"{day.year}-Q{(day.month - 1) // 3 + 1}"

    if period == "month":
        return f"{day.year}-{day.month:02d}"

    raise ValueError(f"Unknown period: {period!r}.")


def order(row):

    return (row["run_id"] or 0, row["snapshot_id"] or 0)


def latest_per(rows, key):
    """
    The latest observation of every group key(row) (the last run wins).
    """

    latest = {}

    for row in rows:

        group = key(row)

        if group not in latest or order(row) >= order(latest[group]):
            latest[group] = row

    return list(latest.values())


def in_range(row, start=None, end=None):

    return (start is None or row["date"] >= start) and (end is None or row["date"] <= end)


def matches(row, carriers=(), statuses=(), categories=()):

    if carriers and (row["carrier"] or NOT_STATED) not in carriers:
        return False

    if statuses and row["final_status"] not in statuses:
        return False

    if categories and not set(categories) & set(row["categories"]):
        return False

    return True


# ============================================================
# FIGURES
# ============================================================

def wilson(count, n):
    """
    The 95% Wilson score interval of count / n, as (low, high).
    """

    if n == 0:
        return None, None

    p = count / n
    denominator = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / denominator
    margin = Z * sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / denominator

    return max(0.0, centre - margin), min(1.0, centre + margin)


def proportion(count, n):
    """
    {"count", "n", "rate", "low", "high"}; the rate is None (not 0) when
    nothing could be counted.
    """

    low, high = wilson(count, n)

    return {
        "count": count, "n": n,
        "rate": None if n == 0 else count / n,
        "low": low, "high": high,
    }


def is_number(value):

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def variance_groups(rows):
    """
    The invoice mismatches per currency (never added across currencies):
    [{"currency", "invoices_checked", "mismatches", "net", "absolute",
    "unknown_amount"}], with "Not stated" for a currency the documents
    did not give.
    """

    groups = defaultdict(list)

    for row in rows:
        groups[row["currency"] or NOT_STATED].append(row)

    result = []

    for currency in sorted(groups):

        members = groups[currency]
        mismatches = [
            row for row in members
            if row["invoice_status"] == "MISMATCH" and is_number(row["invoice_difference"])
        ]

        result.append({
            "currency": currency,
            "invoices_checked": sum(row["invoice_status"] in ("MATCH", "MISMATCH") for row in members),
            "mismatches": len(mismatches),
            "net": round(sum(row["invoice_difference"] for row in mismatches), 2) if mismatches else None,
            "absolute": round(sum(abs(row["invoice_difference"]) for row in mismatches), 2) if mismatches else None,
            # A MISMATCH whose difference is missing is counted, not turned into 0
            "unknown_amount": sum(
                row["invoice_status"] == "MISMATCH" and not is_number(row["invoice_difference"])
                for row in members
            ),
        })

    return result


def figures(rows):
    """
    The rates and counts of a group of observations (one per shipment).
    """

    delivered = [row for row in rows if row["delivery_status"] in ON_TIME_STATUSES]
    compared = [row for row in rows if row["quantity_status"] in ("MATCH", "MISMATCH")]
    invoiced = [row for row in rows if row["invoice_status"] in ("MATCH", "MISMATCH")]
    decided = [row for row in rows if row["final_status"] in DECIDED_STATUSES]
    unresolved = [row for row in rows if row["final_status"] not in DECIDED_STATUSES]

    late_days = [
        row["days_late"] for row in delivered
        if row["delivery_status"] == "DELAYED" and is_number(row["days_late"])
    ]

    return {
        "shipments": len(rows),
        "delay": proportion(sum(row["delivery_status"] == "DELAYED" for row in delivered), len(delivered)),
        "shortage": proportion(
            sum(is_number(row["piece_difference"]) and row["piece_difference"] > 0 for row in compared),
            len(compared),
        ),
        "invoice_mismatch": proportion(
            sum(row["invoice_status"] == "MISMATCH" for row in invoiced), len(invoiced)
        ),
        "discrepancy": proportion(
            sum(row["final_status"] == "DISCREPANCIES DETECTED" for row in decided), len(decided)
        ),
        "needs_review": proportion(len(unresolved), len(rows)),
        "average_days_late": (
            {"value": round(sum(late_days) / len(late_days), 2), "n": len(late_days)}
            if late_days else {"value": None, "n": 0}
        ),
        "variance": variance_groups(rows),
    }


# ============================================================
# ANALYSIS
# ============================================================

def series(rows, period="month"):
    """
    One entry per period with an audit, oldest first: the figures of the
    shipments audited in it (a shipment counts once, with its latest
    observation in the period).
    """

    groups = defaultdict(list)

    for row in rows:
        groups[period_key(row["date"], period)].append(row)

    result = []

    for key in sorted(groups):

        entry = figures(latest_per(groups[key], lambda row: row["shipment_key"]))
        entry["period"] = key

        result.append(entry)

    return result


def carrier_scorecards(rows, min_sample=MIN_SAMPLE):
    """
    One scorecard per carrier from each shipment's latest observation.
    {"carriers": [...], "ranked": n, "notes": [...]}; a carrier with
    fewer than min_sample decided shipments has rank None.
    """

    groups = defaultdict(list)
    names = {}

    for row in latest_per(rows, lambda row: row["shipment_key"]):

        key = carrier_key(row["carrier"]) or NOT_STATED
        names.setdefault(key, row["carrier"] or NOT_STATED)
        groups[key].append(row)

    cards = []

    for key, members in groups.items():

        card = figures(members)
        card["carrier"] = names[key]
        card["sufficient"] = card["discrepancy"]["n"] >= min_sample and key != NOT_STATED
        card["rank"] = None

        cards.append(card)

    ranked = sorted(
        (card for card in cards if card["sufficient"]),
        key=lambda card: (card["discrepancy"]["rate"], -card["discrepancy"]["n"], card["carrier"].casefold()),
    )

    for position, card in enumerate(ranked, start=1):
        card["rank"] = position

    cards.sort(key=lambda card: (card["rank"] is None, card["rank"] or 0, card["carrier"].casefold()))

    notes = [SCORECARD_NOTE.format(minimum=min_sample)]

    if len(ranked) >= 2 and all(
        a["discrepancy"]["low"] <= b["discrepancy"]["high"] and b["discrepancy"]["low"] <= a["discrepancy"]["high"]
        for a in ranked for b in ranked
    ):
        notes.append(OVERLAP_NOTE)

    if any(card["carrier"] == NOT_STATED for card in cards):
        notes.append("Shipments whose documents do not name a carrier are grouped as 'Not stated' and never ranked.")

    return {"carriers": cards, "ranked": len(ranked), "notes": notes, "minimum_sample": min_sample}


def repeat_categories(rows):
    """
    Discrepancy categories over all audits in range: how many shipments
    were affected, how many audits saw it, and on how many shipments it
    was seen by two or more audit runs (a repeat), with the carriers
    involved.
    """

    seen = defaultdict(lambda: defaultdict(set))
    observations = defaultdict(int)
    carriers = defaultdict(set)

    for row in rows:

        for category in row["categories"]:

            seen[category][row["shipment_key"]].add(row["run_id"])
            observations[category] += 1
            carriers[category].add(row["carrier"] or NOT_STATED)

    result = [
        {
            "category": category,
            "shipments_affected": len(shipments),
            "audit_observations": observations[category],
            "shipments_repeating": sum(len(runs) >= 2 for runs in shipments.values()),
            "carriers": sorted(carriers[category]),
        }
        for category, shipments in seen.items()
    ]

    result.sort(key=lambda item: (-item["shipments_repeating"], -item["shipments_affected"], item["category"]))

    return result


def analyze(
    observations, period="month", carriers=(), start=None, end=None, statuses=(), categories=(),
    min_sample=MIN_SAMPLE,
):
    """
    The trends of the recorded history under the filters given (carrier
    names as shown, final statuses, discrepancy categories, a date range
    of the audit runs).

    Filters apply to a shipment's observation in each period: a shipment
    is in a period's figures when its latest observation there matches.
    Returns {"observations", "shipments", "series", "carriers",
    "categories", "empty", "insufficient_reason", "notes", "filters"};
    empty is True when there is no recorded history at all (nothing to
    chart), insufficient_reason says why a trend line is not drawn.
    """

    if period not in PERIODS:
        raise ValueError(f"Unknown period: {period!r}.")

    all_rows = rows_from(observations)
    ranged = [row for row in all_rows if in_range(row, start, end)]

    by_period = defaultdict(list)

    for row in ranged:
        by_period[(period_key(row["date"], period), row["shipment_key"])].append(row)

    # The shipment's latest observation per period, then the filters
    per_period = [max(group, key=order) for group in by_period.values()]
    selected = [row for row in per_period if matches(row, carriers, statuses, categories)]

    # Repeats read every audit in range of the shipments that pass the filters
    chosen = {row["shipment_key"] for row in selected}
    kept = [row for row in ranged if row["shipment_key"] in chosen]

    time_series = series(selected, period)

    reason = None

    if not all_rows:
        reason = "No audit history has been recorded yet. Record an audit run in Audit Operations."
    elif not ranged:
        reason = "No audit run was recorded in this date range."
    elif not selected:
        reason = "No shipment matches the filters."
    elif len(time_series) < 2:
        reason = (
            "Only one period has recorded audits, so there is no trend to draw yet. "
            "Record audits over time to see how the rates change."
        )

    return {
        "empty": not all_rows,
        "insufficient_reason": reason,
        "observations": len(all_rows),
        "shipments": len(chosen),
        "series": time_series,
        "carriers": carrier_scorecards(selected, min_sample),
        "categories": repeat_categories(kept),
        "notes": [OVERBILLING_NOTE],
        "period": period,
        "filters": {
            "carriers": list(carriers), "statuses": list(statuses), "categories": list(categories),
            "start": start.isoformat() if start else None, "end": end.isoformat() if end else None,
        },
    }


def filter_options(observations):
    """
    The values the filters can take, from the recorded history:
    carriers (with "Not stated"), final statuses, categories and the
    first and last audit dates.
    """

    rows = rows_from(observations)

    return {
        "carriers": sorted({row["carrier"] or NOT_STATED for row in rows}, key=str.casefold),
        "statuses": sorted({row["final_status"] for row in rows}),
        "categories": sorted({category for row in rows for category in row["categories"]}),
        "first": min((row["date"] for row in rows), default=None),
        "last": max((row["date"] for row in rows), default=None),
    }


# ============================================================
# SHIPMENT HISTORY
# ============================================================

def compare_snapshots(snapshots, first_id, second_id):
    """
    What changed between two recorded snapshots of one shipment (the
    list from HistoryStore.snapshots), whichever order they were
    recorded in: the earlier one is compared with the later one. None
    when either id is not among them.
    """

    by_id = {snapshot["id"]: snapshot for snapshot in snapshots}

    if first_id not in by_id or second_id not in by_id:
        return None

    first, second = sorted((by_id[first_id], by_id[second_id]), key=lambda snapshot: snapshot["id"])

    earlier = {
        finding["finding_key"]
        for snapshot in snapshots if snapshot["id"] < first["id"]
        for finding in snapshot["findings"]
    }

    return {
        "earlier": first,
        "later": second,
        "changes": compare(first, second, earlier_keys=earlier),
    }


# ============================================================
# CSV
# ============================================================

def percent(proportion_):

    return None if proportion_["rate"] is None else f"{proportion_['rate'] * 100:.1f}"


def series_csv(entries):
    """
    The period series as CSV (UTF-8 with a byte-order mark, every cell
    formula-protected). One row per period and currency; a period with
    no mismatch has one row with blank variance cells (not 0).
    """

    rows = []

    for entry in entries:

        groups = entry["variance"] or [None]

        for group in groups:

            rows.append([
                cell(entry["period"]), str(entry["shipments"]),
                str(entry["delay"]["n"]), str(entry["delay"]["count"]), cell(percent(entry["delay"])),
                cell(None if entry["delay"]["low"] is None else f"{entry['delay']['low'] * 100:.1f}"),
                cell(None if entry["delay"]["high"] is None else f"{entry['delay']['high'] * 100:.1f}"),
                str(entry["shortage"]["n"]), str(entry["shortage"]["count"]), cell(percent(entry["shortage"])),
                str(entry["invoice_mismatch"]["n"]), str(entry["invoice_mismatch"]["count"]),
                cell(percent(entry["invoice_mismatch"])),
                str(entry["discrepancy"]["n"]), str(entry["discrepancy"]["count"]),
                cell(percent(entry["discrepancy"])), str(entry["needs_review"]["count"]),
                cell(group["currency"] if group else None),
                cell(group["mismatches"] if group else None),
                cell(group["net"], money=True) if group else cell(None),
                cell(group["absolute"], money=True) if group else cell(None),
            ])

    return write_csv(SERIES_COLUMNS, rows)


def carriers_csv(scorecards):

    return write_csv(CARRIER_COLUMNS, [
        [
            cell(card["rank"]), cell(card["carrier"]), str(card["shipments"]),
            str(card["discrepancy"]["n"]), str(card["discrepancy"]["count"]), cell(percent(card["discrepancy"])),
            cell(None if card["discrepancy"]["low"] is None else f"{card['discrepancy']['low'] * 100:.1f}"),
            cell(None if card["discrepancy"]["high"] is None else f"{card['discrepancy']['high'] * 100:.1f}"),
            str(card["delay"]["n"]), cell(percent(card["delay"])),
            str(card["shortage"]["n"]), cell(percent(card["shortage"])),
            str(card["invoice_mismatch"]["n"]), cell(percent(card["invoice_mismatch"])),
            str(card["needs_review"]["count"]),
            "sufficient" if card["sufficient"] else f"insufficient (fewer than {scorecards['minimum_sample']} decided shipments)",
            "Arithmetic mismatches only; not proven overbilling.",
        ]
        for card in scorecards["carriers"]
    ])


def categories_csv(items):

    return write_csv(CATEGORY_COLUMNS, [
        [
            cell(item["category"]), str(item["shipments_affected"]), str(item["audit_observations"]),
            str(item["shipments_repeating"]), cell(", ".join(item["carriers"])),
        ]
        for item in items
    ])


LONG_COLUMNS = (
    "section", "group", "currency", "metric", "count", "n", "value", "interval_low", "interval_high", "note",
)


def pct(value):

    return None if value is None else f"{value * 100:.1f}"


def long_rows(section, group, entry, note=""):

    rows = []

    for metric, key in (
        ("delay_rate_pct", "delay"), ("shortage_rate_pct", "shortage"),
        ("invoice_mismatch_rate_pct", "invoice_mismatch"), ("discrepancy_rate_pct", "discrepancy"),
        ("needs_review_rate_pct", "needs_review"),
    ):
        item = entry[key]

        rows.append([
            section, cell(group), cell(None), metric, str(item["count"]), str(item["n"]),
            cell(pct(item["rate"])), cell(pct(item["low"])), cell(pct(item["high"])), note,
        ])

    late = entry["average_days_late"]

    rows.append([
        section, cell(group), cell(None), "average_days_late_when_delayed", cell(None), str(late["n"]),
        cell(late["value"]), cell(None), cell(None), note,
    ])

    for item in entry["variance"]:

        for metric, value in (("arithmetic_variance_absolute", item["absolute"]),
                              ("arithmetic_variance_net", item["net"])):
            rows.append([
                section, cell(group), cell(item["currency"]), metric, str(item["mismatches"]),
                str(item["invoices_checked"]), cell(value, money=True), cell(None), cell(None),
                "Arithmetic mismatch, not proven overbilling.",
            ])

    return rows


def analytics_csv(result):
    """
    The filtered analytics as one flat CSV (UTF-8 with a byte-order
    mark, every cell formula-protected): one row per metric, with its
    count, its sample size n and its interval, for each period, each
    carrier and each repeat category. The first rows record the filters.
    Blank means not available; it is never 0.
    """

    filters = result["filters"]

    rows = [[
        "filters", cell(None), cell(None), "period_basis", cell(None), cell(None), result["period"],
        cell(None), cell(None),
        f"carriers={', '.join(filters['carriers']) or 'all'}; statuses={', '.join(filters['statuses']) or 'all'}; "
        f"categories={', '.join(filters['categories']) or 'all'}; from={filters['start'] or 'any'}; "
        f"to={filters['end'] or 'any'}",
    ]]

    for entry in result["series"]:
        rows += long_rows("period", entry["period"], entry)

    for card in result["carriers"]["carriers"]:

        note = (
            f"rank {card['rank']}" if card["rank"]
            else f"not ranked: fewer than {result['carriers']['minimum_sample']} decided shipments"
        )

        rows += long_rows("carrier", card["carrier"], card, note)

    for item in result["categories"]:

        rows.append([
            "category", cell(item["category"]), cell(None), "shipments_affected", cell(None),
            cell(None), str(item["shipments_affected"]), cell(None), cell(None),
            f"seen in {item['audit_observations']} audit observation(s); "
            f"repeating on {item['shipments_repeating']} shipment(s)",
        ])

    rows.append(["note", cell(None), cell(None), "variance", cell(None), cell(None), cell(None),
                 cell(None), cell(None), OVERBILLING_NOTE])

    return write_csv(LONG_COLUMNS, rows)


def day(text):
    """
    A date from "YYYY-MM-DD" (a filter value), or None for an empty one.
    """

    return date.fromisoformat(text) if text else None
