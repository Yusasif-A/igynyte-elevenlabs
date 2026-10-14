"""
Render the operations page and the demo-video page as static files for
Firebase Hosting.

The operations page is the same template el_tools serves locally
(el_dashboard.py), so there is one source for both. The only difference is
where it finds the API: served by el_tools it calls its own host; hosted on
Firebase it calls the backend URL written into config.js here.

    python build_web.py --api https://your-backend.example   # page + video
    python build_web.py                                      # keep the current API URL
    cd web && firebase deploy --only hosting

start.ps1 -Deploy does both with the tunnel URL it just opened.

The demo video (fraud-agent-demo.mp4 in the project root) is copied into the
Firebase build and played at /demo. It is git-ignored: it lives on Firebase
Hosting, not in the repository.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from config import settings
from el_dashboard import CHECKOUT_HTML, CONSOLE_HTML

ROOT = Path(__file__).parent
PUBLIC = ROOT / "web" / "public"
VIDEO = ROOT / "fraud-agent-demo.mp4"
REPO = "https://github.com/Yusasif-A/igynyte-elevenlabs"
CONFIG_TAG = '<script src="/config.js"></script>\n'


def _brand() -> str:
    head, _, tail = settings.bank.name.partition(" ")
    return f"{head}<span>{tail}</span>" if tail else f"{head}<span>Bank</span>"


def _render(html: str) -> str:
    html = (html.replace("__BANK_BRAND__", _brand())
                .replace("__BANK_NAME__", settings.bank.name)
                .replace("__AGENT_NAME__", settings.bank.agent_display_name)
                .replace("__CUSTOMER_ID__", "CUS-100001"))
    # config.js must run before the page's own script reads NOBA_API_BASE.
    return html.replace("<script", CONFIG_TAG + "<script", 1)


DEMO_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__BANK_NAME__ Fraud Agent Demo</title>
<meta name="description" content="Demo: an ElevenLabs voice agent that calls the cardholder in English or Arabic while a suspicious payment is held.">
<style>
  :root { --bg:#0b1014; --panel:#141b21; --line:#223039; --ink:#e8eef2; --muted:#8da2b0; --accent:#3d8bfd; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:18px 22px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
  .brand { font-weight:700; font-size:18px; } .brand span { color:var(--accent); }
  .spacer { flex:1; }
  a.pill { font-size:13px; padding:6px 12px; border-radius:999px; border:1px solid var(--line);
    color:var(--ink); text-decoration:none; }
  a.pill:hover { border-color:var(--accent); }
  .wrap { max-width:1100px; margin:0 auto; padding:22px 16px 40px; display:grid; gap:18px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px 18px; }
  h1 { font-size:22px; margin:0 0 6px; }
  .sub { color:var(--muted); margin:0; }
  video { width:100%; border-radius:10px; background:#000; display:block; aspect-ratio:16/9; }
  h2 { margin:0 0 10px; font-size:13px; text-transform:uppercase; letter-spacing:.09em; color:var(--muted); font-weight:600; }
  ol { margin:0; padding-left:20px; } li { margin:4px 0; }
  .grid2 { display:grid; gap:18px; grid-template-columns:1fr 1fr; }
  @media (max-width:820px) { .grid2 { grid-template-columns:1fr; } }
  a { color:var(--accent); }
</style>
</head>
<body>
<header>
  <div class="brand">__BANK_BRAND__ &middot; Fraud Agent Demo</div>
  <div class="spacer"></div>
  <a class="pill" href="/">Try the live demo</a>
  <a class="pill" href="__REPO__">GitHub</a>
</header>
<div class="wrap">
  <div>
    <h1>Real-time fraud intervention, in English and Arabic</h1>
    <p class="sub">A suspicious card payment is held, and the bank's ElevenLabs voice agent calls the
      cardholder while it can still be stopped: it verifies them without asking for any secret,
      freezes the card or lets the payment through, and hands anything irreversible to a person.</p>
  </div>
  <video controls playsinline preload="metadata" src="/media/fraud-agent-demo.mp4">
    Your browser cannot play this video. <a href="/media/fraud-agent-demo.mp4">Download it</a>.
  </video>
  <div class="grid2">
    <div class="card">
      <h2>What happens in the video</h2>
      <ol>
        <li>A card payment at a Kyiv merchant is held as suspicious.</li>
        <li>The agent calls, says it is the bank's AI and will never ask for a PIN.</li>
        <li>It verifies the cardholder with their own security question.</li>
        <li>It describes the real payment, the customer says it wasn't them.</li>
        <li>The card is frozen and the payment declined, live on the bank's screen.</li>
        <li>The call is recorded, transcribed and evaluated in the audit trail.</li>
      </ol>
    </div>
    <div class="card">
      <h2>Try it yourself</h2>
      <p style="margin:0 0 8px">Open the <a href="/">live demo</a>, press <b>Send payment</b>, then
        <b>Answer</b> the bank's call in your browser (microphone needed).</p>
      <p style="margin:0 0 8px">Yusuf's security answers: food <b>amala</b>, colour <b>green</b>,
        first school <b>green flower school</b>.</p>
      <p style="margin:0">Code, tests, transcripts and architecture:
        <a href="__REPO__">github.com/Yusasif-A/igynyte-elevenlabs</a></p>
    </div>
  </div>
</div>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", help="Public base URL of el_tools. Omit to keep the URL already in config.js.")
    args = ap.parse_args()

    PUBLIC.mkdir(parents=True, exist_ok=True)
    (PUBLIC / "index.html").write_text(_render(CHECKOUT_HTML), encoding="utf-8")
    (PUBLIC / "console.html").write_text(_render(CONSOLE_HTML), encoding="utf-8")

    config = PUBLIC / "config.js"
    if args.api:
        api = args.api.rstrip("/")
        if not api.startswith("https://"):
            raise SystemExit("--api must be an https URL: a hosted page cannot call an http backend")
        config.write_text(f"window.NOBA_API_BASE = {json.dumps(api)};\n", encoding="utf-8")
        print(f"API: {api}")
    elif not config.exists():
        config.write_text('window.NOBA_API_BASE = "";\n', encoding="utf-8")
        print("API: none yet (the page will show the bank as offline)")
    else:
        print("API: unchanged")

    demo = (DEMO_HTML.replace("__BANK_BRAND__", _brand())
                     .replace("__BANK_NAME__", settings.bank.name)
                     .replace("__REPO__", REPO))
    (PUBLIC / "demo.html").write_text(demo, encoding="utf-8")
    if VIDEO.exists():
        target = PUBLIC / "media" / VIDEO.name
        target.parent.mkdir(exist_ok=True)
        if not target.exists() or target.stat().st_size != VIDEO.stat().st_size \
                or target.stat().st_mtime < VIDEO.stat().st_mtime:
            shutil.copy2(VIDEO, target)
        print(f"Demo video: /demo ({VIDEO.stat().st_size / 1e6:.1f} MB)")
    else:
        print(f"Demo video not found at {VIDEO.name}; /demo will have no video")
    print("Built web/public")


if __name__ == "__main__":
    main()
