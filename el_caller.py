"""
Ring a customer about a stored fraud signal, from the terminal.

The checkout page is the normal trigger; this is for
the pre-seeded signals in bank_data (FRD-CARD-FOREIGN and friends) and for
testing telephony on its own.

The briefing travels as dynamic variables, so the agent opens with the right
name and card. Tool calls from this call reach el_tools without a checkout
session behind them, so use it to test telephony and the conversation; use the
checkout page to exercise the full verify-and-freeze path.

    python el_caller.py --list-signals
    python el_caller.py FRD-CARD-FOREIGN --to +2348012345678 [--arabic]
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

import el_client as el  # noqa: E402
import el_prompts as P  # noqa: E402
from bank_api import NotFound, bank  # noqa: E402
from config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(stream=sys.stdout)], force=True)
logger = logging.getLogger("fraud_agent.el_caller")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("signal_id", nargs="?")
    ap.add_argument("--to", help="Dial this number instead of the customer on file (testing only).")
    ap.add_argument("--arabic", action="store_true", help="Open the call in Arabic.")
    ap.add_argument("--list-signals", action="store_true")
    args = ap.parse_args()

    if args.list_signals or not args.signal_id:
        for s in bank.list_fraud_signals():
            print(f"{s['signalId']:24} {s['customerId']:12} {s.get('summary', '')[:60]}")
        return

    try:
        signal = bank.get_fraud_signal(args.signal_id)
        customer = bank.get_customer_by_id(signal["customerId"])
        card = bank.get_card(signal["cardId"]) if signal.get("cardId") else None
    except NotFound as exc:
        raise SystemExit(str(exc)) from exc

    first, last4 = customer.get("firstName", "there"), str((card or {}).get("last4", ""))
    number = args.to or settings.elevenlabs.demo_phone or customer["phoneNumber"]
    variables = {
        # Not a live checkout session: el_tools resolves this to the single
        # briefed session, so start one there too when testing tools end to end.
        "session_id": f"signal-{args.signal_id.lower()}",
        "customer_first_name": first,
        "card_last4": last4,
        "has_flagged_activity": "true",
        "channel": "phone",
        "opening_line": P.opening_line_en(first, last4),
        "opening_line_ar": P.opening_line_ar(first, last4),
    }
    try:
        out = el.outbound_call(to_number=number, dynamic_variables=variables,
                               language="ar" if args.arabic else "en")
    except el.ElevenLabsError as exc:
        raise SystemExit(f"call failed: {exc}") from exc
    logger.info("Call to ***%s: %s", number[-4:], out)


if __name__ == "__main__":
    main()
