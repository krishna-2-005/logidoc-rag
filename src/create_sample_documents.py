from pathlib import Path
import sys
import pymupdf

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import RAW_DATA_DIR


def create_pdf(filename, title, content):
    """
    Create a simple synthetic logistics PDF.
    """

    output_path = RAW_DATA_DIR / filename

    document = pymupdf.open()
    page = document.new_page()

    # Add title
    page.insert_text(
        (50, 50),
        title,
        fontsize=20
    )

    # Add body
    y_position = 90

    for line in content.strip().split("\n"):
        page.insert_text(
            (50, y_position),
            line,
            fontsize=11
        )
        y_position += 20

    document.save(output_path)
    document.close()

    print(f"Created: {output_path}")


def main():

    # ========================================================
    # 1. BILL OF LADING
    # ========================================================

    bol_content = """
Shipment Number: SHJ-2026-001

Shipper:
Texas Industrial Components
Dallas, Texas

Consignee:
Gulf Manufacturing Solutions
Houston, Texas

Carrier:
SwiftLine Logistics

Origin:
Dallas, Texas

Destination:
Houston, Texas

Pickup Date:
October 2, 2026

Expected Delivery Date:
October 3, 2026

Equipment:
53-foot Dry Van

Commodity:
Industrial Machine Components

Number of Pieces:
48

Total Weight:
12,500 lbs

Special Instructions:
Handle with care. Keep cargo dry.
"""

    create_pdf(
        "BOL_SHJ-2026-001.pdf",
        "BILL OF LADING",
        bol_content
    )

    # ========================================================
    # 2. PROOF OF DELIVERY
    # ========================================================

    pod_content = """
Shipment Number: SHJ-2026-001

Carrier:
SwiftLine Logistics

Delivery Location:
Gulf Manufacturing Solutions
Houston, Texas

Delivery Date:
October 3, 2026

Delivered By:
SwiftLine Logistics

Received By:
Michael Anderson

Number of Pieces Delivered:
48

Delivery Status:
Delivered

Damage Report:
No visible damage reported.

Receiver Comments:
Shipment received in good condition.

Signature:
Michael Anderson
"""

    create_pdf(
        "POD_SHJ-2026-001.pdf",
        "PROOF OF DELIVERY",
        pod_content
    )

    # ========================================================
    # 3. FREIGHT INVOICE
    # ========================================================

    invoice_content = """
Invoice Number: INV-SHJ-2026-001

Shipment Number: SHJ-2026-001

Carrier:
SwiftLine Logistics

Billing Date:
October 4, 2026

Origin:
Dallas, Texas

Destination:
Houston, Texas

Base Freight:
$1,200.00

Fuel Surcharge:
$180.00

Detention Charge:
$0.00

Other Accessorial Charges:
$50.00

Total Invoice Amount:
$1,430.00

Payment Terms:
Net 30

Due Date:
November 3, 2026
"""

    create_pdf(
        "INVOICE_SHJ-2026-001.pdf",
        "FREIGHT INVOICE",
        invoice_content
    )


if __name__ == "__main__":
    main()