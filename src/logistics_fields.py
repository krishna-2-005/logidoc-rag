"""
Centralized logistics terminology and field aliases.

This file allows the system to understand different terminology
used by different logistics companies and document formats.
"""


# ============================================================
# DOCUMENT TYPE ALIASES
# ============================================================

DOCUMENT_TYPE_ALIASES = {

    "BOL": [
        "bill of lading",
        "bill of lading document",
        "bol",
        "b/l",
    ],

    "POD": [
        "proof of delivery",
        "pod",
        "delivery receipt",
        "delivery confirmation",
    ],

    "INVOICE": [
        "invoice",
        "freight invoice",
        "carrier invoice",
        "transport invoice",
    ],

    "RATE_CONFIRMATION": [
        "rate confirmation",
        "rate con",
        "rate confirmation sheet",
        "load confirmation",
    ],
}


# ============================================================
# IDENTIFIER ALIASES
# ============================================================

IDENTIFIER_ALIASES = {

    "shipment_id": [
        "shipment id",
        "shipment identifier",
        "shipment number",
        "shipment no",
        "shipment no.",
        "shipment #",
        "shipment reference",
        "shipment ref",
    ],

    "load_id": [
        "load id",
        "load identifier",
        "load number",
        "load no",
        "load no.",
        "load #",
        "load reference",
        "load ref",
    ],

    "bol_number": [
        "bol number",
        "bol no",
        "bol no.",
        "bol #",
        "bill of lading number",
        "bill of lading no",
        "bill of lading no.",
        "b/l number",
        "b/l no",
    ],

    "pro_number": [
        "pro number",
        "pro no",
        "pro no.",
        "pro #",
    ],

    "po_number": [
        "po number",
        "po no",
        "po no.",
        "po #",
        "purchase order number",
        "purchase order no",
        "purchase order no.",
    ],

    "invoice_number": [
        "invoice number",
        "invoice no",
        "invoice no.",
        "invoice #",
    ],

    "tracking_number": [
        "tracking number",
        "tracking no",
        "tracking no.",
        "tracking #",
        "tracking id",
        "tracking identifier",
    ],

    "booking_number": [
        "booking number",
        "booking no",
        "booking no.",
        "booking #",
        "booking id",
    ],

    "reference_number": [
        "reference number",
        "reference no",
        "reference no.",
        "reference #",
        "reference id",
        "reference",
        "ref number",
        "ref no",
        "ref no.",
        "ref #",
    ],

    "consignment_number": [
        "consignment number",
        "consignment no",
        "consignment no.",
        "consignment #",
        "consignment id",
    ],

    "waybill_number": [
        "waybill number",
        "waybill no",
        "waybill no.",
        "waybill #",
        "waybill id",
    ],

    "carrier_reference": [
        "carrier reference",
        "carrier ref",
        "carrier reference number",
        "carrier ref number",
    ],

    "customer_reference": [
        "customer reference",
        "customer ref",
        "customer reference number",
        "customer ref number",
    ],
}


# ============================================================
# GENERAL LOGISTICS FIELD ALIASES
# ============================================================

FIELD_ALIASES = {

    "carrier": [
        "carrier",
        "carrier name",
        "transporter",
        "transport company",
        "transportation company",
        "motor carrier",
        "scac",
    ],

    "origin": [
        "origin",
        "pickup location",
        "pickup",
        "ship from",
        "ship-from",
        "shipper location",
        "origin location",
    ],

    "destination": [
        "destination",
        "delivery location",
        "delivery address",
        "ship to",
        "ship-to",
        "consignee location",
        "destination location",
    ],

    "expected_delivery_date": [
        "expected delivery",
        "expected delivery date",
        "estimated delivery",
        "estimated delivery date",
        "eta",
        "estimated time of arrival",
        "scheduled delivery",
        "scheduled delivery date",
        "due date",
    ],

    "actual_delivery_date": [
        "actual delivery",
        "actual delivery date",
        "delivered date",
        "delivery date",
        "pod date",
        "proof of delivery date",
    ],

    "quantity": [
        "number of pieces",
        "pieces",
        "piece count",
        "total pieces",
        "packages",
        "package count",
        "units",
        "unit count",
        "qty",
        "quantity",
        "cartons",
        "carton count",
        "cases",
        "case count",
    ],

    "weight": [
        "weight",
        "total weight",
        "shipment weight",
        "gross weight",
        "net weight",
        "freight weight",
    ],

    "delivery_status": [
        "delivery status",
        "status",
        "shipment status",
        "delivery condition",
    ],

    "damage": [
        "damage",
        "damage report",
        "visible damage",
        "damages",
        "condition",
    ],

    "receiver": [
        "received by",
        "receiver",
        "consignee",
        "recipient",
        "delivered to",
    ],
}


# ============================================================
# INVOICE FIELD ALIASES
# ============================================================

INVOICE_FIELD_ALIASES = {

    "base_freight": [
        "base freight",
        "freight charge",
        "linehaul",
        "line haul",
        "transportation charge",
    ],

    "fuel_surcharge": [
        "fuel surcharge",
        "fuel charge",
        "fuel",
    ],

    "detention_charge": [
        "detention",
        "detention charge",
        "detention fee",
    ],

    "other_charges": [
        "other charges",
        "other charge",
        "accessorial",
        "accessorial charges",
        "miscellaneous charges",
    ],

    "invoice_total": [
        "total invoice amount",
        "invoice total",
        "total amount",
        "total due",
        "amount due",
        "grand total",
    ],
}