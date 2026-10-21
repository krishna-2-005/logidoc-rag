"""
LogiDoc-RAG freight audit PDF export

Turns the result of reconciliation.reconcile_shipment() for one
shipment into a PDF report, in memory. Nothing is recalculated: every
value comes from the reconciliation result as it is, and the export is
refused under the same rules as the CSV export
(audit_export.check_exportable): the reconciliation must have
succeeded, be for the selected shipment, and cite only that shipment's
documents.

Sections: Shipment Overview, Quantity Reconciliation, Delivery
Reconciliation, Invoice Reconciliation (with a line-item table),
Discrepancies Detected, Items Requiring Review, Evidence and Source
References.

Values the result does not have are shown as NOT AVAILABLE, never as 0,
and an audit that needs review says so. Differences keep their sign.

The report uses the PDF standard Helvetica fonts, which cover Western
European text including € £ — and ’. A character those fonts cannot
show (for example the non-breaking hyphen U+2011) is replaced by a
plain equivalent ("-") or by "?", so nothing disappears silently.

Layout: A4 portrait, text wrapped to the column width (long words are
broken), table headers repeated after a page break, a row taller than
a page continued on the next page, and "Page n of m" on every page.
"""

from datetime import datetime

import pymupdf

from audit_export import NOT_AVAILABLE, check_exportable, export_filename


TITLE = "LOGIDOC-RAG — FREIGHT AUDIT REPORT"

PAGE_WIDTH, PAGE_HEIGHT = pymupdf.paper_size("a4")
MARGIN = 50
TOP = 72
BOTTOM = PAGE_HEIGHT - 60
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN

REGULAR = pymupdf.Font("helv")
BOLD = pymupdf.Font("hebo")

BODY_SIZE = 9
LINE_HEIGHT = 11.5
CELL_PADDING = 4
HEADING_HEIGHT = 32

BLACK = (0, 0, 0)
GREY = (0.35, 0.35, 0.35)
RULE = (0.75, 0.75, 0.75)
HEADER_FILL = (0.90, 0.92, 0.95)
TITLE_FILL = (0.12, 0.23, 0.40)

STATUS_COLOURS = {
    "DISCREPANCIES DETECTED": (0.70, 0.10, 0.10),
    "MISMATCH": (0.70, 0.10, 0.10),
    "DELAYED": (0.70, 0.10, 0.10),
    "SHORTAGE": (0.70, 0.10, 0.10),
    "OVERAGE": (0.70, 0.10, 0.10),
    "NEEDS REVIEW": (0.75, 0.40, 0.00),
    NOT_AVAILABLE: (0.75, 0.40, 0.00),
    "NO DISCREPANCIES": (0.05, 0.45, 0.15),
    "MATCH": (0.05, 0.45, 0.15),
    "ON TIME": (0.05, 0.45, 0.15),
    "EARLY": (0.05, 0.45, 0.15),
}

# Plain equivalents for characters Helvetica does not have
REPLACEMENTS = {
    "‑": "-", "‐": "-", "‒": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
}


# ============================================================
# VALUES
# ============================================================

def printable(value):
    """
    Text the report fonts can show: unsupported characters become a
    plain equivalent or "?".
    """

    text = str(value)

    return "".join(
        character if character in "\n\t" or REGULAR.has_glyph(ord(character))
        else REPLACEMENTS.get(character, "?")
        for character in text
    )


def shown(value):

    return NOT_AVAILABLE if value is None or value == "" else str(value)


def money(value):

    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return NOT_AVAILABLE

    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def signed_money(value):
    """
    A difference with its sign: "+$120.00", "-$30.00", "$0.00".
    """

    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return NOT_AVAILABLE

    if abs(value) < 0.005:
        return "$0.00"

    return ("+" if value > 0 else "-") + f"${abs(value):,.2f}"


def count(value):

    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return NOT_AVAILABLE

    return f"{value:,}"


def piece_difference(value):
    """
    BOL - POD with its sign and meaning.
    """

    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return NOT_AVAILABLE

    if value > 0:
        return f"+{value:,} (shortage of {value:,})"

    if value < 0:
        return f"{value:,} (over-delivery of {-value:,})"

    return "0"


def day_difference(value):
    """
    actual - expected delivery date, in days, with its sign.
    """

    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return NOT_AVAILABLE

    days = abs(value)
    unit = "day" if days == 1 else "days"

    if value > 0:
        return f"+{days} ({days} {unit} late)"

    if value < 0:
        return f"-{days} ({days} {unit} early)"

    return "0 (on the expected date)"


# ============================================================
# LAYOUT
# ============================================================

def wrap(text, font, size, width):
    """
    The lines of text that fit width; words longer than a line are
    broken.
    """

    lines = []

    for paragraph in printable(text).split("\n"):

        line = ""

        for word in paragraph.split(" "):

            candidate = word if not line else f"{line} {word}"

            if font.text_length(candidate, size) <= width:
                line = candidate
                continue

            if line:
                lines.append(line)

            # A word wider than the line is broken into pieces
            while font.text_length(word, size) > width:

                cut = len(word)

                while cut > 1 and font.text_length(word[:cut], size) > width:
                    cut -= 1

                lines.append(word[:cut])
                word = word[cut:]

            line = word

        lines.append(line)

    return lines or [""]


class Report:
    """
    A PDF written top to bottom; starts a new page when the next block
    does not fit.
    """

    def __init__(self, shipment_id):

        self.document = pymupdf.open()
        self.shipment_id = shipment_id
        self.page = None
        self.y = 0
        self.pending_heading = None
        self.new_page()

    def new_page(self):

        self.page = self.document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        self.y = TOP

        self.text(
            MARGIN, 36,
            f"LogiDoc-RAG Freight Audit Report — Shipment {self.shipment_id}",
            REGULAR, 8, GREY,
        )

        self.page.draw_line((MARGIN, 44), (PAGE_WIDTH - MARGIN, 44), color=RULE, width=0.5)

    def text(self, x, baseline, value, font=REGULAR, size=BODY_SIZE, colour=BLACK):

        writer = pymupdf.TextWriter(self.page.rect)
        writer.append((x, baseline), printable(value), font=font, fontsize=size)
        writer.write_text(self.page, color=colour)

    def room(self, height):
        """
        Starts a new page unless height fits below the cursor.
        """

        if self.y + height > BOTTOM:
            self.new_page()

    def title(self, value):

        height = 34

        self.page.draw_rect(
            pymupdf.Rect(MARGIN, self.y, PAGE_WIDTH - MARGIN, self.y + height),
            color=None, fill=TITLE_FILL,
        )

        self.text(MARGIN + 10, self.y + 22, value, BOLD, 15, (1, 1, 1))

        self.y += height + 14

    def heading(self, value):
        """
        A section heading. It is drawn with the block that follows it
        (see place), so it never ends a page on its own.
        """

        self.pending_heading = value

    def place(self, height):
        """
        Room for a block of height and, before it, any pending heading;
        then draws the heading.
        """

        heading_height = HEADING_HEIGHT if self.pending_heading else 0

        if self.y + heading_height + height > BOTTOM:
            self.new_page()

        if self.pending_heading:

            self.y += 8

            self.text(MARGIN, self.y + 12, self.pending_heading, BOLD, 12, TITLE_FILL)

            self.page.draw_line(
                (MARGIN, self.y + 16), (PAGE_WIDTH - MARGIN, self.y + 16),
                color=TITLE_FILL, width=0.8,
            )

            self.y += 24
            self.pending_heading = None

    def paragraph(self, value, font=REGULAR, colour=BLACK):

        for number, line in enumerate(wrap(value, font, BODY_SIZE, CONTENT_WIDTH)):

            if number == 0:
                self.place(LINE_HEIGHT)
            else:
                self.room(LINE_HEIGHT)

            self.text(MARGIN, self.y + 9, line, font, BODY_SIZE, colour)
            self.y += LINE_HEIGHT

        self.y += 4

    def table(self, headers, rows, widths, status_columns=()):
        """
        A table with wrapped cells. The header row is repeated on every
        page the table continues on; a row taller than the space left
        continues on the next page. status_columns: indexes of cells
        coloured by their status.
        """

        widths = [width * CONTENT_WIDTH / sum(widths) for width in widths]

        def cells(values, font):
            return [
                wrap(value, font, BODY_SIZE, width - 2 * CELL_PADDING)
                for value, width in zip(values, widths)
            ]

        header_lines = cells(headers, BOLD)
        header_height = max(map(len, header_lines)) * LINE_HEIGHT + 2 * CELL_PADDING

        def draw_row(lines, start, stop, font, fill=None, colours=None):

            height = (stop - start) * LINE_HEIGHT + 2 * CELL_PADDING
            x = MARGIN

            for index, (cell, width) in enumerate(zip(lines, widths)):

                rect = pymupdf.Rect(x, self.y, x + width, self.y + height)

                self.page.draw_rect(rect, color=RULE, fill=fill, width=0.5)

                colour = (colours or {}).get(index, BLACK)

                for number, line in enumerate(cell[start:stop]):
                    self.text(
                        x + CELL_PADDING,
                        self.y + CELL_PADDING + 8.5 + number * LINE_HEIGHT,
                        line, font, BODY_SIZE, colour,
                    )

                x += width

            self.y += height

        def header():
            draw_row(header_lines, 0, max(map(len, header_lines)), BOLD, HEADER_FILL)

        # A table that fits on one page is kept together; a longer one
        # starts with its header and at least one line of its first row
        table_height = header_height + sum(
            max(map(len, cells([shown(value) for value in values], REGULAR)))
            * LINE_HEIGHT + 2 * CELL_PADDING
            for values in rows
        )

        if table_height <= BOTTOM - TOP - HEADING_HEIGHT:
            self.place(table_height)
        else:
            self.place(header_height + LINE_HEIGHT + 2 * CELL_PADDING)

        header()

        for values in rows:

            values = [shown(value) for value in values]
            lines = cells(values, REGULAR)
            total = max(map(len, lines))

            colours = {
                index: STATUS_COLOURS[values[index]]
                for index in status_columns
                if values[index] in STATUS_COLOURS
            }

            # A row that fits on an empty page is moved, not split
            fits_on_a_page = (
                total * LINE_HEIGHT + 2 * CELL_PADDING
                <= BOTTOM - TOP - header_height
            )

            start = 0

            while start < total:

                available = int(
                    (BOTTOM - self.y - 2 * CELL_PADDING) // LINE_HEIGHT
                )

                if available < 1 or (start == 0 and available < total and fits_on_a_page):
                    self.new_page()
                    header()
                    continue

                stop = min(total, start + available)

                if start:
                    # The rest of a split row: say which row it continues
                    first = lines[0][start:stop] or [""]
                    first[0] = f"(continued) {first[0]}".strip()
                    part = [first + [""] * (stop - start - len(first))] + [
                        cell[start:stop] for cell in lines[1:]
                    ]
                    draw_row(part, 0, stop - start, REGULAR, colours=colours)
                else:
                    draw_row(lines, start, stop, REGULAR, colours=colours)

                start = stop

                if start < total:
                    self.new_page()
                    header()

        self.y += 8

    def finish(self):
        """
        Page numbers, metadata, and the PDF as bytes.
        """

        pages = self.document.page_count

        for number, page in enumerate(self.document, start=1):

            self.page = page

            label = f"Page {number} of {pages}"

            self.text(
                PAGE_WIDTH - MARGIN - REGULAR.text_length(label, 8),
                PAGE_HEIGHT - 30, label, REGULAR, 8, GREY,
            )

        self.document.set_metadata({
            "title": f"Freight Audit Report {self.shipment_id}",
            "subject": f"Freight audit of shipment {self.shipment_id}",
            "creator": "LogiDoc-RAG",
        })

        # Embed only the characters used
        self.document.subset_fonts()

        data = self.document.tobytes(garbage=3, deflate=True)

        self.document.close()

        return data


# ============================================================
# REPORT
# ============================================================

def audit_pdf(result, selected_shipment=None, generated_at=None):
    """
    The freight audit report of one shipment as PDF bytes.

    Raises audit_export.ExportError when the result cannot be exported
    (failed reconciliation, no shipment ID, another shipment selected,
    or evidence of another shipment).
    """

    shipment_id = check_exportable(result, selected_shipment)

    generated_at = generated_at or datetime.now()

    final_status = shown(result.get("final_status"))
    carrier = result.get("carrier") or {}
    route = result.get("route") or {}
    quantity = result.get("quantity") or {}
    delivery = result.get("delivery") or {}
    invoice = result.get("invoice") or {}
    discrepancies = result.get("discrepancies") or []
    review_items = result.get("review_items") or []

    report = Report(shipment_id)

    report.title(TITLE)

    # --------------------------------------------------------
    # A. Shipment overview
    # --------------------------------------------------------

    report.heading("A. Shipment Overview")

    report.table(
        ["Field", "Value"],
        [
            ["Shipment ID", shipment_id],
            ["Carrier", carrier.get("bol")],
            ["Origin", route.get("origin")],
            ["Destination", route.get("destination")],
            ["Final audit status", final_status],
            ["Report generated", generated_at.strftime("%Y-%m-%d %H:%M")],
        ],
        [1, 2.6],
        status_columns=(1,),
    )

    comparisons = [
        ["Carrier (BOL vs Invoice)", carrier.get("bol"), carrier.get("invoice"), carrier.get("bol_invoice_status")],
        ["Carrier (BOL vs POD)", carrier.get("bol"), carrier.get("pod"), carrier.get("bol_pod_status")],
        ["Origin (BOL vs Invoice)", route.get("origin"), route.get("invoice_origin"), route.get("origin_status")],
        ["Destination (BOL vs Invoice)", route.get("destination"), route.get("invoice_destination"), route.get("destination_status")],
    ]

    if carrier or route:
        report.table(
            ["Comparison", "BOL", "Other document", "Status"],
            comparisons,
            [1.5, 1.3, 1.3, 1],
            status_columns=(3,),
        )

    # --------------------------------------------------------
    # B. Quantity
    # --------------------------------------------------------

    report.heading("B. Quantity Reconciliation")

    report.table(
        ["Field", "Value"],
        [
            ["BOL expected quantity", count(quantity.get("bol"))],
            ["POD delivered quantity", count(quantity.get("pod"))],
            ["Difference (BOL - POD)", piece_difference(quantity.get("difference"))],
            ["Audit status", quantity.get("status")],
        ]
        + ([["Review reason", quantity.get("reason")]] if quantity.get("reason") else []),
        [1, 2.6],
        status_columns=(1,),
    )

    # --------------------------------------------------------
    # C. Delivery
    # --------------------------------------------------------

    report.heading("C. Delivery Reconciliation")

    report.table(
        ["Field", "Value"],
        [
            ["Expected delivery date", delivery.get("expected")],
            ["Actual delivery date", delivery.get("actual")],
            ["Days late (+) or early (-)", day_difference(delivery.get("days_late"))],
            ["Audit status", delivery.get("status")],
        ]
        + ([["Review reason", delivery.get("reason")]] if delivery.get("reason") else []),
        [1, 2.6],
        status_columns=(1,),
    )

    # --------------------------------------------------------
    # D. Invoice
    # --------------------------------------------------------

    report.heading("D. Invoice Reconciliation")

    line_items = invoice.get("line_items") or []

    report.table(
        ["Invoice line item", "Amount"],
        [[label, money(amount)] for label, amount in line_items]
        or [["Line items", NOT_AVAILABLE]],
        [2.6, 1],
        status_columns=(1,),
    )

    report.table(
        ["Field", "Value"],
        [
            ["Invoice line-item sum", money(invoice.get("line_item_sum"))],
            ["Stated invoice total", money(invoice.get("total"))],
            ["Difference (total - line items)", signed_money(invoice.get("difference"))],
            ["Invoice audit status", invoice.get("status")],
        ]
        + ([["Review reason", invoice.get("reason")]] if invoice.get("reason") else []),
        [1, 2.6],
        status_columns=(1,),
    )

    # --------------------------------------------------------
    # E. Discrepancies
    # --------------------------------------------------------

    report.heading("E. Discrepancies Detected")

    if discrepancies:

        def difference(item):

            if item.get("category") == "Invoice":
                return signed_money(item.get("difference"))

            if "days_late" in item:
                return day_difference(item.get("days_late"))

            if "difference" in item:
                return piece_difference(item.get("difference"))

            return "-"

        report.table(
            ["Category", "Description", "Difference"],
            [
                [item.get("category"), item.get("description"), difference(item)]
                for item in discrepancies
            ],
            [0.9, 3, 1.2],
        )

    else:

        report.paragraph("No discrepancies were detected.", BOLD, STATUS_COLOURS["MATCH"])

    # --------------------------------------------------------
    # F. Review
    # --------------------------------------------------------

    report.heading("F. Items Requiring Review")

    if review_items:

        report.paragraph(
            "These values could not be verified (missing, unreadable or "
            "conflicting) and are not reported as matches.",
            colour=STATUS_COLOURS["NEEDS REVIEW"],
        )

        report.table(
            ["Category", "Status", "Reason"],
            [
                [item.get("category"), "NEEDS REVIEW", item.get("reason")]
                for item in review_items
            ],
            [0.9, 1, 3.2],
            status_columns=(1,),
        )

    else:

        report.paragraph("No items require review.")

    # --------------------------------------------------------
    # G. Evidence
    # --------------------------------------------------------

    report.heading("G. Evidence and Source References")

    evidence = [
        [category, item.get("source"), item.get("page"), item.get("document_type"),
         item.get("shipment_id"), item.get("text")]
        for category in ("Quantity", "Delivery", "Invoice")
        for item in (result.get(category.lower()) or {}).get("evidence") or []
    ]

    if evidence:

        report.table(
            ["Audit", "Source file", "Page", "Type", "Shipment", "Field text"],
            evidence,
            [0.8, 1.6, 0.45, 0.75, 1.05, 2.4],
        )

    else:

        report.paragraph("No evidence was cited by the reconciliation.")

    return report.finish()


def pdf_filename(result):

    return export_filename(result)[: -len(".csv")] + ".pdf"
