"""
Render the checkout and the bank console as static files for Firebase Hosting.

The pages are the same templates el_tools serves locally (el_dashboard.py), so
there is one source for both. The only difference is where they find the API:
served by el_tools they call their own host; hosted on Firebase they call the
backend URL written into config.js here.

    python build_web.py --api https://your-backend.example
    cd web && firebase deploy --only hosting

start.ps1 -Deploy does both with the tunnel URL it just opened.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import settings
from el_dashboard import CHECKOUT_HTML, CONSOLE_HTML

PUBLIC = Path(__file__).with_name("web") / "public"
CONFIG_TAG = '<script src="/config.js"></script>\n'


def _render(html: str) -> str:
    head, _, tail = settings.bank.name.partition(" ")
    brand = f"{head}<span>{tail}</span>" if tail else f"{head}<span>Bank</span>"
    html = (html.replace("__BANK_BRAND__", brand)
                .replace("__BANK_NAME__", settings.bank.name)
                .replace("__AGENT_NAME__", settings.bank.agent_display_name)
                .replace("__CUSTOMER_ID__", "CUS-100001"))
    # config.js must run before the page's own script reads NOBA_API_BASE.
    return html.replace("<script", CONFIG_TAG + "<script", 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", required=True, help="Public base URL of el_tools, e.g. https://x.trycloudflare.com")
    args = ap.parse_args()

    api = args.api.rstrip("/")
    if not api.startswith("https://"):
        raise SystemExit("--api must be an https URL: a hosted page cannot call an http backend")

    PUBLIC.mkdir(parents=True, exist_ok=True)
    (PUBLIC / "index.html").write_text(_render(CHECKOUT_HTML), encoding="utf-8")
    (PUBLIC / "console.html").write_text(_render(CONSOLE_HTML), encoding="utf-8")
    (PUBLIC / "config.js").write_text(f"window.NOBA_API_BASE = {json.dumps(api)};\n", encoding="utf-8")
    print(f"Built web/public for API {api}")


if __name__ == "__main__":
    main()
