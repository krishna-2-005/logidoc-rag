"""
Create the second-shipment test fixture (SHJ-2026-002).

Reuses create_pdf() from src/create_sample_documents.py so the
fixture has the same layout as the SHJ-2026-001 sample PDFs, but
writes into tests/fixtures/shipment_SHJ-2026-002 instead of
data/raw. The production sample PDFs are never touched.

Every value differs from SHJ-2026-001 so that any cross-shipment
leak shows up in the multi-shipment regression test:

    BOL pieces 60, POD delivered 60          -> MATCH
    Expected Oct 9, delivered Oct 8, 2026    -> EARLY (no delay)
    Invoice total $2,875.50                  -> CLEAR, 0 exceptions

Run from the project root:
    python tests/fixtures/create_shipment_fixture.py
"""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"

for path in (PROJECT_ROOT, SRC_DIR):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import create_sample_documents


FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "shipment_SHJ-2026-002"
)


BOL_CONTENT = """
Shipment Number: SHJ-2026-002

Shipper:
Midwest Steel Supply
Chicago, Illinois

Consignee:
Peach State Fabrication
Atlanta, Georgia

Carrier:
Lakeshore Freight Lines

Origin:
Chicago, Illinois

Destination:
Atlanta, Georgia

Pickup Date:
October 6, 2026

Expected Delivery Date:
October 9, 2026

Equipment:
48-foot Flatbed

Commodity:
Structural Steel Beams

Number of Pieces:
60

Total Weight:
18,200 lbs

Special Instructions:
Secure with straps. Tarp required.
"""


POD_CONTENT = """
Shipment Number: SHJ-2026-002

Carrier:
Lakeshore Freight Lines

Delivery Location:
Peach State Fabrication
Atlanta, Georgia

Delivery Date:
October 8, 2026

Delivered By:
Lakeshore Freight Lines

Received By:
Sarah Whitfield

Number of Pieces Delivered:
60

Delivery Status:
Delivered

Damage Report:
No visible damage reported.

Receiver Comments:
All beams received and counted.

Signature:
Sarah Whitfield
"""


INVOICE_CONTENT = """
Invoice Number: INV-SHJ-2026-002

Shipment Number: SHJ-2026-002

Carrier:
Lakeshore Freight Lines

Billing Date:
October 9, 2026

Origin:
Chicago, Illinois

Destination:
Atlanta, Georgia

Base Freight:
$2,400.00

Fuel Surcharge:
$360.00

Detention Charge:
$0.00

Other Accessorial Charges:
$115.50

Total Invoice Amount:
$2,875.50

Payment Terms:
Net 30

Due Date:
November 8, 2026
"""


def main():

    FIXTURE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    # create_pdf() writes to its module's RAW_DATA_DIR;
    # point it at the fixture folder for this script only
    create_sample_documents.RAW_DATA_DIR = FIXTURE_DIR

    create_sample_documents.create_pdf(
        "BOL_SHJ-2026-002.pdf",
        "BILL OF LADING",
        BOL_CONTENT
    )

    create_sample_documents.create_pdf(
        "POD_SHJ-2026-002.pdf",
        "PROOF OF DELIVERY",
        POD_CONTENT
    )

    create_sample_documents.create_pdf(
        "INVOICE_SHJ-2026-002.pdf",
        "FREIGHT INVOICE",
        INVOICE_CONTENT
    )


if __name__ == "__main__":

    main()
