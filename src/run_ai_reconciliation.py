import io
from contextlib import redirect_stdout

from llm_analyzer import analyze_reconciliation
from reconciliation import reconcile_shipment


def main():
    print("=" * 70)
    print("LOGIDOC AI RECONCILIATION")
    print("=" * 70)

    print("\nRunning shipment reconciliation...\n")

    # Capture the output produced by reconciliation.py
    captured_output = io.StringIO()

    with redirect_stdout(captured_output):
        result = reconcile_shipment()

    reconciliation_output = captured_output.getvalue().strip()

    # If the function returns text instead of printing it
    if not reconciliation_output and isinstance(result, str):
        reconciliation_output = result.strip()

    if not reconciliation_output:
        print("Reconciliation produced no output.")
        return

    print(reconciliation_output)

    print("\n" + "=" * 70)
    print("AI LOGISTICS ANALYSIS")
    print("=" * 70)

    analysis = analyze_reconciliation(reconciliation_output)

    print(analysis)

    print("\n" + "=" * 70)
    print("AI ANALYSIS COMPLETED")
    print("=" * 70)


if __name__ == "__main__":
    main()