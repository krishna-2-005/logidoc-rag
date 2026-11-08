"""
Scale benchmark on SYNTHETIC data only (every shipment ID starts with "BENCH-";
no customer document is read). Measures, at each size, the time and the peak
Python memory (tracemalloc) of:

    carrier aggregation      carrier_intelligence.analyze
    carrier detail           carrier_intelligence.carrier_detail, first page of 50
    exception aggregation    audit_analytics.build_exceptions
    exception explanations   audit_explanations.explain_all (the Exception Center list does this)
    freight audit engine     freight_audit.audit_freight on every shipment
    review store sync        review_store.ReviewStore.sync (SQLite), then list_reviews
    history record           audit_history.HistoryStore.record_run (SQLite)

and, with --pdfs N, the real end-to-end audit (AuditService.snapshot) on N
generated shipments of 4 PDFs each: reading, classifying, extracting and
reconciling every document, cold and cached.

The in-memory structures are the audit's own (a snapshot item with its
reconciliation result and freight audit), built by hand: they measure the
aggregation code, not PDF reading. All files go to a temporary folder.

Usage (from the project root):
    python scripts/benchmark_scale.py --sizes 100 1000 10000 --pdfs 100 --json report.json
"""

import argparse
import gc
import json
import random
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for path in (ROOT, ROOT / "src", ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_analytics  # noqa: E402
import audit_explanations  # noqa: E402
import carrier_intelligence as ci  # noqa: E402
import document_extraction as de  # noqa: E402
import freight_audit as fa  # noqa: E402
from audit_history import HistoryStore  # noqa: E402
from review_store import ReviewStore  # noqa: E402

CARRIERS = ["Swift Lines", "Blue Ridge Haulers", "Pacific Crest Transport", "Desert Sun Carriers", "Northern Star Freight",
            "Lakeshore Freight Lines", "SwiftLine Logistics", "Gulf Coast Express"]


def field(value):

    return {"state": "EXTRACTED", "value": value, "page": 1, "corrected": False}


def synthetic_shipment(n, rng):
    """One BENCH- shipment: a reconciliation result and freight audit inputs shaped as the audit makes them."""

    sid = f"BENCH-{n:06d}"
    carrier = rng.choice(CARRIERS)
    month = 1 + n % 12
    pickup = f"2026-{month:02d}-{1 + n % 27:02d}"
    late = rng.choice([0, 0, 0, 1, 2, -1])
    status = "DELAYED" if late > 0 else "EARLY" if late < 0 else "ON TIME"
    total = round(rng.uniform(500, 3000), 2)
    mismatch = rng.random() < 0.15
    contract = round(total - rng.choice([0, 0, 20, 50]), 2) if rng.random() < 0.5 else None
    short = rng.random() < 0.1

    result = {
        "success": True,
        "shipment_id": sid,
        "final_status": "DISCREPANCIES DETECTED" if (late > 0 or mismatch or short) else "NO DISCREPANCIES",
        "carrier": {"bol": carrier, "invoice": carrier, "pod": carrier, "bol_invoice_status": "MATCH", "bol_pod_status": "MATCH"},
        "route": {"origin": "Chicago", "destination": "Atlanta", "invoice_origin": "Chicago", "invoice_destination": "Atlanta",
                  "origin_status": "MATCH", "destination_status": "MATCH"},
        "quantity": {"bol": 48, "pod": 45 if short else 48, "difference": 3 if short else 0,
                     "status": "MISMATCH" if short else "MATCH", "reason": "", "evidence": []},
        "delivery": {"expected": pickup, "actual": pickup, "status": status, "days_late": late, "reason": "", "evidence": []},
        "invoice": {"total": total, "line_item_sum": total - (50 if mismatch else 0), "difference": 50.0 if mismatch else 0.0,
                    "status": "MISMATCH" if mismatch else "MATCH", "reason": "", "evidence": [], "line_items": [],
                    "breakdown": None, "base_freight": None, "fuel_surcharge": None, "detention_charge": None, "other_charges": None},
        "discrepancies": [{"category": "Delivery Date", "description": "Delivered after the expected date."}] if late > 0 else [],
        "review_items": [],
        "excluded_unidentified": 0,
        "unreadable_files": [],
    }

    inputs = [
        {"file": f"BOL_{sid}.pdf", "document_type": "BOL", "fields": {"carrier": field(carrier), "pickup_date": field(pickup)}},
        {"file": f"POD_{sid}.pdf", "document_type": "POD", "fields": {"carrier": field(carrier), "delivery_date": field(pickup)}},
        {"file": f"INVOICE_{sid}.pdf", "document_type": "INVOICE", "fields": {"carrier": field(carrier), "invoice_total": field(total),
                                                                              "currency": field("USD")}},
    ]
    charges = []

    if contract is not None:
        inputs.append({"file": f"RATE_{sid}.pdf", "document_type": "RATE_CONFIRMATION", "fields": {"carrier": field(carrier)}})
        charges = [
            {"charge": "Fuel surcharge", "kind": "charge", "rule": "fuel_surcharge", "expected": 180.0, "billed": 180.0 + total - contract,
             "variance": round(total - contract, 2), "currency": "USD", "expected_source": None, "billed_source": None,
             "outcome": "DISCREPANCY" if total != contract else "MATCHED", "explanation": "synthetic", "evidence": []},
            {"charge": "Invoice total against the contract", "kind": "total", "rule": "invoice_total", "expected": contract, "billed": total,
             "variance": round(total - contract, 2), "currency": "USD", "expected_source": None, "billed_source": None,
             "outcome": "DISCREPANCY" if total != contract else "MATCHED", "explanation": "synthetic", "evidence": []},
        ]

    key = sid.replace("-", "")

    return {
        "shipment_id": sid,
        "shipment_key": key,
        "input_fingerprint": f"fingerprint-{n}",
        "result": result,
        "inputs": {"applied_correction_ids": [], "fields": [], "ignored": []},
        "freight_audit": {
            "outcome": ("DISCREPANCY" if total != contract else "MATCHED") if contract is not None else "INSUFFICIENT_EVIDENCE",
            "explanation": ["synthetic"], "documents": {"contract": [f"RATE_{sid}.pdf"] if contract is not None else [],
                                                        "execution": [], "invoice": [f"INVOICE_{sid}.pdf"]},
            "currency": "USD", "charges": charges, "checks": [], "financial_impact": None, "tolerances": {},
            "inputs": inputs, "corrections_applied": [], "corrections_ignored": [],
        },
    }


def synthetic_extractions(n, rng):
    """Typed extractions of one BENCH- shipment for the freight audit engine."""

    sid = f"BENCH-{n:06d}"
    total = round(rng.uniform(500, 3000), 2)

    def doc(kind, name, items=(), **values):
        return de.DocumentExtraction(
            filename=f"{name}_{sid}.pdf", sha256=f"{name}{n}", document_type=kind, status=de.S_EXTRACTED,
            fields={k: de.ExtractedField(name=k, state=de.EXTRACTED, value=v, page=1) for k, v in values.items()},
            line_items=[de.LineItem(description=d, amount=a, page=1) for d, a in items],
        )

    return [
        doc(de.RATE, "RATE", shipment_id=sid, carrier="Swift Lines", agreed_rate=total - 180, fuel_surcharge=180.0, currency="USD"),
        doc(de.BOL, "BOL", shipment_id=sid, carrier="Swift Lines", pickup_date="2026-10-01"),
        doc(de.POD, "POD", shipment_id=sid, carrier="Swift Lines", delivery_date="2026-10-03"),
        doc(de.INVOICE, "INVOICE", [("Base Freight", total - 180), ("Fuel Surcharge", 180.0)], shipment_id=sid,
            invoice_number=f"INV-{n}", carrier="Swift Lines", invoice_total=total, currency="USD"),
    ]


def measure(label, action):
    """(result, seconds, peak MiB) of one call."""

    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    result = action()
    seconds = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1] / 2**20
    tracemalloc.stop()
    print(f"  {label:<34} {seconds:9.3f} s   peak {peak:8.1f} MiB", flush=True)

    return result, {"seconds": round(seconds, 4), "peak_mib": round(peak, 1)}


def run_size(size, folder):

    rng = random.Random(size)
    print(f"\n{size:,} synthetic shipments", flush=True)
    figures = {}

    shipments, figures["build_synthetic_data"] = measure("build synthetic data", lambda: [synthetic_shipment(n, rng) for n in range(size)])
    snapshot = {"shipments": shipments, "unidentified_sources": [], "unreadable_files": []}
    completeness = [{"shipment_key": item["shipment_key"], "missing": []} for item in shipments]

    exceptions, figures["exception_aggregation"] = measure("exception aggregation", lambda: audit_analytics.build_exceptions(snapshot))
    for item in exceptions:
        item["review_status"] = "open"
    figures["exceptions"] = len(exceptions)
    print(f"  ({len(exceptions):,} exceptions)")

    _, figures["exception_explanations"] = measure("exception explanations", lambda: audit_explanations.explain_all(snapshot, exceptions))
    result, figures["carrier_aggregation"] = measure("carrier aggregation", lambda: ci.analyze(shipments, exceptions, completeness))
    assert sum(card["shipments"] for card in result["carriers"]) == size
    _, figures["carrier_detail_page"] = measure("carrier detail (first 50)",
                                                lambda: ci.carrier_detail(shipments, exceptions, completeness, "swift lines", limit=50))

    engine_size = min(size, 2000)
    docs = [synthetic_extractions(n, rng) for n in range(engine_size)]
    policy = fa.load_policy("")
    _, timing = measure(f"freight audit engine ({engine_size:,})", lambda: [fa.audit_freight(d, policy=policy) for d in docs])
    figures["freight_audit_engine"] = {**timing, "shipments": engine_size,
                                       "per_shipment_ms": round(timing["seconds"] / engine_size * 1000, 3)}

    store = ReviewStore(folder / f"reviews_{size}.sqlite3")
    _, figures["review_sync_first"] = measure("review store sync (first)", lambda: store.sync(exceptions))
    _, figures["review_sync_again"] = measure("review store sync (unchanged)", lambda: store.sync(exceptions))
    _, figures["review_queue_query"] = measure("review queue query (all)", lambda: store.list_reviews())
    _, figures["review_queue_search"] = measure("review queue query (search)", lambda: store.list_reviews(search="BENCH-000042"))

    history = HistoryStore(folder / f"history_{size}.sqlite3")
    _, figures["history_record_run"] = measure("history record run", lambda: history.record_run(snapshot, exceptions, label="benchmark"))
    _, figures["history_record_unchanged"] = measure("history record run (unchanged)",
                                                     lambda: history.record_run(snapshot, exceptions, label="benchmark again"))

    return figures


def write_shipment_pdfs(folder, sid, carrier):
    """A BOL, POD, invoice and rate confirmation of one SYNTHETIC shipment, as the test PDFs are laid out."""

    from test_freight_audit import pairs
    from test_ingestion_manager import make_pdf

    common = dict(Carrier=carrier, Origin="Chicago, Illinois", Destination="Atlanta, Georgia")

    make_pdf(folder / f"BOL_{sid}.pdf", "BILL OF LADING", [f"Shipment Number: {sid}", ""] + pairs(
        **common, Pickup_Date="October 1, 2026", Expected_Delivery_Date="October 3, 2026", Number_of_Pieces=48))
    make_pdf(folder / f"POD_{sid}.pdf", "PROOF OF DELIVERY", [f"Shipment Number: {sid}", ""] + pairs(
        Carrier=carrier, Delivery_Date="October 3, 2026", Number_of_Pieces_Delivered=48, Delivery_Status="Delivered"))
    make_pdf(folder / f"INVOICE_{sid}.pdf", "FREIGHT INVOICE", [f"Invoice Number: INV-{sid}", f"Shipment Number: {sid}", ""] + pairs(
        **common, Base_Freight="$1,400.00", Fuel_Surcharge="$200.00", Total_Invoice_Amount="$1,600.00", Payment_Terms="Net 30"))
    make_pdf(folder / f"RATE_CONFIRMATION_{sid}.pdf", "RATE CONFIRMATION", [f"Shipment Number: {sid}", ""] + pairs(
        **common, Pickup_Date="October 1, 2026", Delivery_Date="October 3, 2026", Line_Haul="$1,400.00",
        Fuel_Surcharge="$180.00", Payment_Terms="Net 30"))


def run_pdfs(count, folder):
    """The real audit on count generated shipments (4 PDFs each)."""

    from test_api import Rig

    print(f"\nEnd to end: {count} generated shipments ({count * 4} PDFs) through AuditService.snapshot", flush=True)

    rig = Rig(folder / "pdfs", "local")

    for path in list(rig.raw.glob("*.pdf")):
        path.unlink()

    for n in range(count):
        write_shipment_pdfs(rig.raw, f"BENCH-{n:06d}", CARRIERS[n % len(CARRIERS)])

    service = rig.state.service
    service.invalidate()
    snapshot, cold = measure("snapshot (cold: every PDF read)", service.snapshot)
    _, warm = measure("snapshot (cached)", service.snapshot)
    _, carriers = measure("carrier aggregation (API inputs)",
                          lambda: ci.analyze(snapshot["shipments"], service.exceptions(), service.completeness()["shipments"]))

    reconciled = sum(bool((item["result"] or {}).get("success")) for item in snapshot["shipments"])
    audited = sum((item.get("freight_audit") or {}).get("outcome") == "DISCREPANCY" for item in snapshot["shipments"])

    return {"shipments": len(snapshot["shipments"]), "reconciled": reconciled, "freight_discrepancies": audited, "pdfs": count * 4,
            "snapshot_cold": cold, "snapshot_cached": warm, "carrier_aggregation_with_inputs": carriers,
            "cold_ms_per_pdf": round(cold["seconds"] / (count * 4) * 1000, 2)}


def main():

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--pdfs", type=int, default=0, help="also run the real audit on this many generated shipments")
    parser.add_argument("--json", type=Path, help="write the figures here")
    arguments = parser.parse_args()

    folder = Path(tempfile.mkdtemp(prefix="logidoc_benchmark_"))
    print(f"SYNTHETIC benchmark data in {folder} (shipment IDs BENCH-...)")

    report = {"synthetic": True, "python": sys.version.split()[0], "sizes": {}}

    for size in arguments.sizes:
        report["sizes"][str(size)] = run_size(size, folder)

    if arguments.pdfs:
        report["end_to_end"] = run_pdfs(arguments.pdfs, folder)

    if arguments.json:
        arguments.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nfigures written to {arguments.json}")


if __name__ == "__main__":
    main()
