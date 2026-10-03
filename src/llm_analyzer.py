import ollama

MODEL_NAME = "nemotron-3-nano:4b"


def analyze_reconciliation(reconciliation_text):
    """
    Analyze the reconciliation result using the local Nemotron model.
    """

    prompt = f"""
You are a logistics operations analyst.

Analyze the following shipment reconciliation result.

RECONCILIATION RESULT:
{reconciliation_text}

Provide the response in this format:

SHIPMENT SUMMARY
- Shipment ID:
- Overall Status:

DOCUMENT MATCHES
- Origin:
- Destination:
- Quantity:

DELIVERY
- Expected Date:
- Actual Date:
- Status:

INVOICE
- Total Amount:

DISCREPANCIES
- List any discrepancies.
- If none exist, say "No major discrepancies found."

FINAL ANALYSIS
Give a short professional summary for a logistics operations team.

Use ONLY the information provided in the reconciliation result.
Do not invent or assume information.
"""

    response = ollama.chat(
        model=MODEL_NAME,
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    return response["message"]["content"]


# Compatibility alias
generate_llm_analysis = analyze_reconciliation


if __name__ == "__main__":
    print("LLM Analyzer module loaded successfully.")
    print(f"Model: {MODEL_NAME}")