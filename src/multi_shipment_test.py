from pathlib import Path
import sys
import shutil
import pymupdf


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from document_loader import load_all_documents
import shipment_identity


# ============================================================
# DIRECTORIES
# ============================================================

RAW_DIR = PROJECT_ROOT / "data" / "raw"

TEST_DIR = (
    PROJECT_ROOT
    / "data"
    / "multi_shipment_test"
)

BACKUP_DIR = (
    PROJECT_ROOT
    / "data"
    / "raw_backup_phase1"
)


# ============================================================
# SHIPMENT A
# ============================================================

SHIPMENT_A_FILES = [
    (
        RAW_DIR / "BOL_SHJ-2026-001.pdf",
        TEST_DIR / "random_document_7845.pdf"
    ),
    (
        RAW_DIR / "INVOICE_SHJ-2026-001.pdf",
        TEST_DIR / "financial_scan_9921.pdf"
    ),
    (
        RAW_DIR / "POD_SHJ-2026-001.pdf",
        TEST_DIR / "delivery_scan_3388.pdf"
    ),
]


# ============================================================
# SHIPMENT B
# ============================================================

SHIPMENT_B_FILES = [
    TEST_DIR / "scan_5501.pdf",
    TEST_DIR / "billing_8842.pdf",
    TEST_DIR / "delivery_6619.pdf",
]


# ============================================================
# CREATE PDF
# ============================================================

def create_pdf(file_path, text):

    document = pymupdf.open()

    page = document.new_page()

    page.insert_text(
        (50, 60),
        text,
        fontsize=12
    )

    document.save(str(file_path))

    document.close()


# ============================================================
# PREPARE TEST DOCUMENTS
# ============================================================

def prepare_test_documents():

    print("\n" + "=" * 70)
    print("PREPARING MULTI-SHIPMENT TEST DOCUMENTS")
    print("=" * 70)

    TEST_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    # Remove old test PDFs
    for pdf_file in TEST_DIR.glob("*.pdf"):

        try:
            pdf_file.unlink()
        except Exception:
            pass

    # --------------------------------------------------------
    # Shipment A
    # --------------------------------------------------------

    print("\nShipment A:")

    for source, destination in SHIPMENT_A_FILES:

        if not source.exists():

            print(
                f"ERROR: Source file not found: "
                f"{source}"
            )

            continue

        shutil.copy2(
            source,
            destination
        )

        print(
            f"Copied: {source.name} "
            f"-> "
            f"{destination.name}"
        )

    # --------------------------------------------------------
    # Shipment B
    # --------------------------------------------------------

    print("\nShipment B:")

    bol_text = """
BILL OF LADING

Load ID:
LOAD-7788

BOL Number:
BOL-7788

Shipper:
Demo Industrial Supplier

Consignee:
Demo Manufacturing Company

Carrier:
Demo Transport Logistics

Origin:
Dallas, Texas

Destination:
Austin, Texas

Number of Pieces:
32

Total Weight:
8,500 lbs

Pickup Date:
October 2, 2026

Expected Delivery Date:
October 4, 2026
"""

    invoice_text = """
FREIGHT INVOICE

Invoice Number:
INV-LOAD-7788

Load ID:
LOAD-7788

Carrier:
Demo Transport Logistics

Origin:
Dallas, Texas

Destination:
Austin, Texas

Base Freight:
$900.00

Fuel Surcharge:
$100.00

Detention Charge:
$0.00

Other Accessorial Charges:
$25.00

Total Invoice Amount:
$1,025.00

Payment Terms:
Net 30
"""

    pod_text = """
PROOF OF DELIVERY

Load ID:
LOAD-7788

Carrier:
Demo Transport Logistics

Delivery Location:
Demo Manufacturing Company
Austin, Texas

Delivery Date:
October 4, 2026

Received By:
John Smith

Number of Pieces Delivered:
32

Delivery Status:
Delivered

Damage Report:
No visible damage reported.

Receiver Comments:
Shipment received in good condition.

Signature:
John Smith
"""

    create_pdf(
        SHIPMENT_B_FILES[0],
        bol_text
    )

    create_pdf(
        SHIPMENT_B_FILES[1],
        invoice_text
    )

    create_pdf(
        SHIPMENT_B_FILES[2],
        pod_text
    )

    for file_path in SHIPMENT_B_FILES:

        print(
            f"Created: {file_path.name}"
        )

    print(
        f"\nTest documents prepared: "
        f"{len(list(TEST_DIR.glob('*.pdf')))}"
    )


# ============================================================
# LOAD TEST DOCUMENTS
#
# IMPORTANT:
# document_loader.load_all_documents()
# DOES NOT ACCEPT A DIRECTORY ARGUMENT.
#
# Therefore we temporarily put the test PDFs inside
# data\raw, call load_all_documents(), then restore
# the original files.
# ============================================================

def load_test_documents():

    BACKUP_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    backed_up_files = []

    try:

        # ----------------------------------------------------
        # Backup current raw PDFs
        # ----------------------------------------------------

        for pdf_file in RAW_DIR.glob("*.pdf"):

            backup_file = (
                BACKUP_DIR
                / pdf_file.name
            )

            shutil.copy2(
                pdf_file,
                backup_file
            )

            backed_up_files.append(
                pdf_file.name
            )

        # ----------------------------------------------------
        # Remove current raw PDFs
        # ----------------------------------------------------

        for pdf_file in RAW_DIR.glob("*.pdf"):

            pdf_file.unlink()

        # ----------------------------------------------------
        # Copy all test PDFs into raw
        # ----------------------------------------------------

        test_files = list(
            TEST_DIR.glob("*.pdf")
        )

        for test_file in test_files:

            shutil.copy2(
                test_file,
                RAW_DIR / test_file.name
            )

        print(
            "\nTemporary test dataset loaded."
        )

        print(
            f"Test PDFs loaded: "
            f"{len(test_files)}"
        )

        # ----------------------------------------------------
        # THIS IS THE CORRECT CALL
        # ----------------------------------------------------

        documents = load_all_documents()

        return documents

    finally:

        # ----------------------------------------------------
        # Remove temporary test PDFs
        # ----------------------------------------------------

        for pdf_file in RAW_DIR.glob("*.pdf"):

            try:
                pdf_file.unlink()
            except Exception:
                pass

        # ----------------------------------------------------
        # Restore original PDFs
        # ----------------------------------------------------

        for filename in backed_up_files:

            backup_file = (
                BACKUP_DIR
                / filename
            )

            if backup_file.exists():

                shutil.copy2(
                    backup_file,
                    RAW_DIR / filename
                )

        print(
            "Original data\\raw files restored."
        )


# ============================================================
# DOCUMENT DETECTION TEST
# ============================================================

def test_document_detection():

    print("\n" + "=" * 70)
    print("DOCUMENT DETECTION TEST")
    print("=" * 70)

    documents = load_test_documents()

    print(
        f"\nDocuments detected: "
        f"{len(documents)}"
    )

    for document in documents:

        print(
            f"\n{document['filename']}"
        )

        print(
            f"  Document Type   : "
            f"{document['document_type']}"
        )

        print(
            f"  Primary ID      : "
            f"{document['primary_identifier']}"
        )

        print(
            f"  All Identifiers : "
            f"{document['identifiers']}"
        )

    return documents


# ============================================================
# MULTI-SHIPMENT IDENTITY TEST
# ============================================================

def test_multi_shipment_identity():

    print("\n" + "=" * 70)
    print("MULTI-SHIPMENT IDENTITY TEST")
    print("=" * 70)

    # --------------------------------------------------------
    # Load the test PDFs through the existing loader
    # --------------------------------------------------------

    documents = load_test_documents()

    print(
        f"\nDocuments available for identity test: "
        f"{len(documents)}"
    )

    # --------------------------------------------------------
    # Existing shipment_identity.py reads from data/raw
    # --------------------------------------------------------
    #
    # So temporarily load test files into raw again.
    # --------------------------------------------------------

    backed_up_files = []

    try:

        # Backup original files
        for pdf_file in RAW_DIR.glob("*.pdf"):

            backup_file = (
                BACKUP_DIR
                / pdf_file.name
            )

            shutil.copy2(
                pdf_file,
                backup_file
            )

            backed_up_files.append(
                pdf_file.name
            )

        # Remove originals
        for pdf_file in RAW_DIR.glob("*.pdf"):

            pdf_file.unlink()

        # Copy test files
        for test_file in TEST_DIR.glob("*.pdf"):

            shutil.copy2(
                test_file,
                RAW_DIR / test_file.name
            )

        # ----------------------------------------------------
        # CORRECT API
        # No documents argument.
        # ----------------------------------------------------

        shipment_map = (
            shipment_identity
            .build_shipment_identity_map()
        )

        print(
            f"\nShipment groups detected: "
            f"{len(shipment_map)}"
        )

        for shipment_id, shipment in shipment_map.items():

            print(
                "\n" + "-" * 70
            )

            print(
                f"Shipment ID: "
                f"{shipment_id}"
            )

            shipment_documents = shipment.get(
                "documents",
                []
            )

            print(
                f"Documents: "
                f"{len(shipment_documents)}"
            )

            for document in shipment_documents:

                print(
                    f"  - "
                    f"{document.get('source')} "
                    f"-> "
                    f"{document.get('document_type')}"
                )

        return shipment_map

    finally:

        # Remove test files
        for pdf_file in RAW_DIR.glob("*.pdf"):

            try:
                pdf_file.unlink()
            except Exception:
                pass

        # Restore original files
        for filename in backed_up_files:

            backup_file = (
                BACKUP_DIR
                / filename
            )

            if backup_file.exists():

                shutil.copy2(
                    backup_file,
                    RAW_DIR / filename
                )

        print(
            "\nOriginal data\\raw files restored."
        )


# ============================================================
# IDENTIFIER RESOLUTION TEST
# ============================================================

def test_identifier_resolution():

    print("\n" + "=" * 70)
    print("IDENTIFIER RESOLUTION TEST")
    print("=" * 70)

    test_identifiers = [
        "SHJ-2026-001",
        "INV-SHJ-2026-001",
        "LOAD-7788",
        "INV-LOAD-7788",
        "BOL-7788",
    ]

    # The current shipment_identity implementation
    # resolves identifiers through its internal shipment map.

    shipment_map = (
        shipment_identity
        .build_shipment_identity_map()
    )

    print(
        f"\nShipment groups available: "
        f"{len(shipment_map)}"
    )

    for identifier in test_identifiers:

        print(
            f"\nSearching: {identifier}"
        )

        found_shipment = None

        # ----------------------------------------------------
        # Search every shipment and its identifiers
        # ----------------------------------------------------

        for shipment_id, shipment in shipment_map.items():

            identifiers = shipment.get(
                "identifiers",
                {}
            )

            # Direct shipment ID
            if str(shipment_id).upper() == identifier.upper():

                found_shipment = shipment_id
                break

            # Other identifiers
            for key, value in identifiers.items():

                if value is None:
                    continue

                if str(value).upper() == identifier.upper():

                    found_shipment = shipment_id
                    break

            if found_shipment:
                break

        if found_shipment:

            print(
                f"Resolved Shipment: "
                f"{found_shipment}"
            )

        else:

            print(
                "Resolved Shipment: NOT FOUND"
            )


# ============================================================
# PHASE 1 VALIDATION
# ============================================================

def validate_phase_1(
    documents,
    shipment_map
):

    print("\n" + "=" * 70)
    print("PHASE 1 VALIDATION")
    print("=" * 70)

    passed = True

    # --------------------------------------------------------
    # Document count
    # --------------------------------------------------------

    if len(documents) == 6:

        print(
            "PASS: 6 documents detected"
        )

    else:

        print(
            f"FAIL: Expected 6 documents, "
            f"found {len(documents)}"
        )

        passed = False

    # --------------------------------------------------------
    # Shipment count
    # --------------------------------------------------------

    if len(shipment_map) == 2:

        print(
            "PASS: 2 shipments detected"
        )

    else:

        print(
            f"FAIL: Expected 2 shipments, "
            f"found {len(shipment_map)}"
        )

        passed = False

    # --------------------------------------------------------
    # Shipment A
    # --------------------------------------------------------

    shipment_a = shipment_map.get(
        "SHJ-2026-001"
    )

    if shipment_a:

        count_a = len(
            shipment_a.get(
                "documents",
                []
            )
        )

        if count_a == 3:

            print(
                "PASS: Shipment A contains "
                "3 documents"
            )

        else:

            print(
                f"FAIL: Shipment A contains "
                f"{count_a} documents"
            )

            passed = False

    else:

        print(
            "FAIL: Shipment A not detected"
        )

        passed = False

    # --------------------------------------------------------
    # Shipment B
    # --------------------------------------------------------

    shipment_b = shipment_map.get(
        "LOAD-7788"
    )

    if shipment_b:

        count_b = len(
            shipment_b.get(
                "documents",
                []
            )
        )

        if count_b == 3:

            print(
                "PASS: Shipment B contains "
                "3 documents"
            )

        else:

            print(
                f"FAIL: Shipment B contains "
                f"{count_b} documents"
            )

            passed = False

    else:

        print(
            "FAIL: Shipment B not detected"
        )

        passed = False

    # --------------------------------------------------------
    # Final result
    # --------------------------------------------------------

    print(
        "\n" + "-" * 70
    )

    if passed:

        print(
            "PHASE 1 MULTI-SHIPMENT TEST: PASSED"
        )

    else:

        print(
            "PHASE 1 MULTI-SHIPMENT TEST: FAILED"
        )

    return passed


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print("\n" + "=" * 70)
    print("PHASE 1 - MULTI-SHIPMENT TEST")
    print("=" * 70)

    # --------------------------------------------------------
    # 1. Prepare six test documents
    # --------------------------------------------------------

    prepare_test_documents()

    # --------------------------------------------------------
    # 2. Test document detection
    # --------------------------------------------------------

    documents = test_document_detection()

    # --------------------------------------------------------
    # 3. Test shipment grouping
    # --------------------------------------------------------

    shipment_map = test_multi_shipment_identity()

    # --------------------------------------------------------
    # 4. Test identifier resolution
    # --------------------------------------------------------

    test_identifier_resolution()

    # --------------------------------------------------------
    # 5. Validate Phase 1
    # --------------------------------------------------------

    result = validate_phase_1(
        documents,
        shipment_map
    )

    # --------------------------------------------------------
    # Final
    # --------------------------------------------------------

    print(
        "\n" + "=" * 70
    )

    if result:

        print(
            "PHASE 1 MULTI-SHIPMENT TEST COMPLETED SUCCESSFULLY"
        )

    else:

        print(
            "PHASE 1 MULTI-SHIPMENT TEST COMPLETED WITH FAILURES"
        )

    print(
        "=" * 70
    )