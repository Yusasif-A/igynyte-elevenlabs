"""
The bank's tools, served over HTTP to an ElevenLabs-hosted agent.

The bank logic lives in its own modules -- bank_api for accounts and cards,
verification.ChallengeFlow for the approved challenge flow, card_authorization
for the held payment. This server exposes them to the agent, and:

  - Sessions. ElevenLabs carries dynamic variables from dial time into every
    tool call, so each request names its own session_id; the model never
    supplies it.
  - The opening line. It is the agent's first message, built here and passed
    in at dial time, instead of a tool the agent had to call after hello.
  - Audit. ElevenLabs posts the transcript, analysis and full audio when a call
    ends; /webhooks/elevenlabs verifies and files them under audit/.

THREE LAYERS, AS BEFORE
The prompt tells the model what it may do. This server refuses to act before
verification passes and counts the attempts. bank_api refuses human-only
operations outright. A model that ignores its instructions still cannot freeze
a card for an unverified caller, and cannot reverse a transaction at all.

Run with ONE worker: sessions live in this process.
    uvicorn el_tools:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
from dotenv import load_dotenv

load_dotenv(override=True)

from fastapi import Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import card_authorization  # noqa: E402
import el_client as el  # noqa: E402
import el_prompts as P  # noqa: E402
from bank_api import NotFound, PermissionDenied, bank  # noqa: E402
from config import settings  # noqa: E402
from el_dashboard import CHECKOUT_HTML, CONSOLE_HTML  # noqa: E402
from verification import ChallengeFlow, mentions_forbidden_credential  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(stream=sys.stdout)],
    force=True,
)
logger = logging.getLogger("fraud_agent.el_tools")

EL = settings.elevenlabs
AUDIT_DIR = Path(__file__).with_name("audit")
AUDIT_DIR.mkdir(exist_ok=True)

# Set ROOT_PATH=/noba when a reverse proxy serves this app under a path prefix
# (https://comnote.publicaai.com/noba/...) and strips it before forwarding. Empty
# means the app owns the whole host, as it does locally.
ROOT = os.getenv("ROOT_PATH", "").strip().rstrip("/")

app = FastAPI(
    root_path=ROOT,
    title=f"{settings.bank.name} fraud agent tools",
    description="Bank operations exposed to an ElevenLabs-hosted agent.",
    version="2.0.0",
)

# The checkout and console are also served from Firebase Hosting, which calls
# this API cross-origin. Only those origins; the tool endpoints still require
# the bearer token whatever the origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=EL.allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# --------------------------------------------------------------------------
# Request logging
# --------------------------------------------------------------------------


@app.middleware("http")
async def log_tool_requests(request: Request, call_next):
    """
    Log both halves of every tool exchange, with timing.

    A slow tool reads to the caller as dead air, and the agent may give up on
    it, so latency is logged on every call and warned on above three seconds.
    """
    if not request.url.path.startswith("/tools/"):
        return await call_next(request)

    body = await request.body()
    logger.info("-> %s %s\n   body: %s", request.method, request.url.path,
                body.decode("utf-8", "replace")[:2000] or "(empty)")
    started = time.monotonic()
    response = await call_next(request)
    elapsed_ms = (time.monotonic() - started) * 1000
    chunks = [chunk async for chunk in response.body_iterator]
    payload = b"".join(chunks)
    logger.log(
        logging.INFO if response.status_code < 400 else logging.ERROR,
        "<- %s %s in %.0fms\n   returned: %s",
        request.url.path, response.status_code, elapsed_ms,
        payload.decode("utf-8", "replace")[:1500] or "(empty)",
    )
    if elapsed_ms > 3000:
        logger.warning("Tool %s took %.1fs -- heard as silence on the call",
                       request.url.path, elapsed_ms / 1000)
    return Response(content=payload, status_code=response.status_code,
                    headers=dict(response.headers), media_type=response.media_type)


@app.exception_handler(RequestValidationError)
async def log_validation_error(request: Request, exc: RequestValidationError):
    logger.error("422 on %s: %s", request.url.path, json.dumps(exc.errors(), default=str)[:1500])
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    """
    Refuse anything without the shared bearer token. Fails closed.

    ElevenLabs sends it from a workspace secret (see el_setup.py), so it never
    appears in the agent's config. It is the only thing between the internet
    and freeze_card.
    """
    if not EL.tool_token:
        raise HTTPException(503, "EL_TOOL_TOKEN is not set; refusing to serve bank tools unauthenticated.")
    if authorization != f"Bearer {EL.tool_token}":
        raise HTTPException(401, "bad or missing bearer token")


# --------------------------------------------------------------------------
# Per-call state
# --------------------------------------------------------------------------


@dataclass
class Session:
    """One call's worth of state."""

    session_id: str
    customer: dict
    signal: dict | None
    card: dict | None
    challenge: ChallengeFlow
    language: str = "en"
    channel: str = "phone"  # "phone" or "web"
    actions_taken: list[str] = field(default_factory=list)
    handed_to_human: bool = False
    # The customer has been told the real details of the held payment. Freeze
    # and approve wait for it, so a model that skips the description and
    # invents one cannot get a decision out of the customer on false facts.
    described: bool = False
    conversation_id: str | None = None
    call_state: str = "briefed"  # briefed, ringing, awaiting_browser, in_call, ended, failed
    call_error: str | None = None
    case_id: str | None = None
    outcome: str | None = None
    officer_reported: bool = False
    started_at: float = field(default_factory=time.monotonic)

    def dynamic_variables(self) -> dict[str, Any]:
        """The briefing the agent hears before its first word."""
        first = self.customer.get("firstName") or "there"
        last4 = str((self.card or {}).get("last4", ""))
        flagged = self.signal is not None
        return {
            "session_id": self.session_id,
            "customer_first_name": first,
            "card_last4": last4,
            "has_flagged_activity": "true" if flagged else "false",
            "channel": self.channel,
            "opening_line": P.opening_line_en(first, last4) if flagged else P.opening_line_no_alert_en(first),
            "opening_line_ar": P.opening_line_ar(first, last4) if flagged else P.opening_line_no_alert_ar(first),
        }


_SESSIONS: dict[str, Session] = {}


def _new_session(session_id: str, customer: dict, signal: dict | None, card: dict | None,
                 *, language: str, channel: str) -> Session:
    # One live call per customer: a stale session would let a new call inherit
    # an old verification.
    for key in [k for k, v in _SESSIONS.items() if v.customer.get("customerId") == customer["customerId"]]:
        _SESSIONS.pop(key, None)
    session = Session(
        session_id=session_id, customer=customer, signal=signal, card=card,
        challenge=ChallengeFlow(customer=customer, max_attempts=settings.bank.max_verification_attempts),
        language=language, channel=channel,
    )
    _SESSIONS[session_id] = session
    return session


def _resolve(session_id: str | None, conversation_id: str | None = None) -> Session:
    """
    Find the session a tool call belongs to.

    ElevenLabs binds session_id from the dial-time dynamic variables, so this
    is normally a direct lookup. The fallback covers the dashboard's test chat,
    where the placeholder "test-session" arrives: it attaches to the single
    live session, and refuses if two different customers are in flight rather
    than risk reading one customer's details to another.
    """
    session = _SESSIONS.get(session_id or "")
    if session is None:
        distinct = list({id(s): s for s in _SESSIONS.values()}.values())
        if not distinct:
            raise HTTPException(409, "No call is briefed. Start one from the checkout page.")
        if len({s.customer.get("customerId") for s in distinct}) > 1:
            raise HTTPException(409, "Several customers are in flight and this call named no session.")
        session = max(distinct, key=lambda s: s.started_at)
    if conversation_id and not conversation_id.startswith("{{") and not session.conversation_id:
        session.conversation_id = conversation_id
    if session.call_state in {"briefed", "ringing", "awaiting_browser"}:
        session.call_state = "in_call"
    return session


def _require_described(session: Session) -> str | None:
    """Verified AND told the real details -- the precondition for a decision on the payment."""
    if blocked := _require_verified(session):
        return blocked
    if session.signal is not None and not session.described:
        return ("Not yet. Call describe_suspicious_activity, tell the customer exactly what it says, "
                "and ask whether it was them. Never state payment details from anywhere else.")
    return None


def _require_verified(session: Session) -> str | None:
    """
    The refusal returned as tool output, naming the next step.

    A bare "no" gets retried; one that says what to do instead is acted on.
    """
    if session.challenge.verified:
        return None
    if session.challenge.exhausted:
        return "Verification has failed. Do not act on the account. Call transfer_to_human_agent."
    if session.challenge.pending_unanswered:
        return (f"Not yet. You asked: {session.challenge.pending_question}. "
                "Pass their reply to check_security_answer.")
    return "Not yet. Call ask_security_question first."


class ToolBody(BaseModel):
    session_id: str | None = None
    conversation_id: str | None = None


# --------------------------------------------------------------------------
# The trigger: a payment at the merchant checkout
# --------------------------------------------------------------------------


class PaymentAttempt(BaseModel):
    card_last4: str = Field(description="Last four digits of the card used.")
    amount: str = Field(description='Amount, e.g. "250.00".')
    currency: str = Field(default="USD")
    merchant: str = Field(default="Online Store")
    country_code: str = Field(default="UA")
    channel: str = Field(default="WEB")
    # How the bank reaches the cardholder: "phone" rings them through the
    # imported number, "browser" lets them answer on this page. "auto" rings
    # when a number is configured and falls back to the browser otherwise.
    contact: str = Field(default="auto")
    language: str = Field(default="en", description='"en" or "ar"')
    destination: str | None = Field(default=None, description="E.164 number to ring instead of the customer's.")


def _find_card(last4: str) -> dict | None:
    for customer_id in ("CUS-100001", "CUS-100002", "CUS-100003", "CUS-100004", "CUS-100005"):
        for card in bank.get_cards_by_customer(customer_id):
            if str(card.get("last4")) == last4.strip():
                return card
    return None


@app.post("/api/payments/attempt")
def payment_attempt(body: PaymentAttempt) -> dict:
    """
    Hold a card payment and, if it looks like fraud, reach the cardholder.

    The authorization stays PENDING throughout -- the call decides it, not this
    endpoint. Unauthenticated like the rest of the demo UI; it only touches the
    simulated bank, and every destructive step still runs behind /tools/.
    """
    card = _find_card(body.card_last4)
    if card is None:
        raise HTTPException(404, f"no card ending {body.card_last4}")
    if card.get("status") != "ACTIVE":
        return {"status": "DECLINED", "reason": "card is frozen", "reference": None}

    try:
        account = bank.get_account(card["accountNumber"])
        auth = card_authorization.authorize(
            card=card, account=account, amount=body.amount, currency=body.currency,
            merchant=body.merchant, country_code=body.country_code, channel=body.channel,
        )
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    if not auth.flagged:
        card_authorization.approve(auth.id, "passed risk checks")
        return {"status": "APPROVED", "reference": auth.reference_id}

    language = "ar" if body.language.lower().startswith("ar") else "en"
    use_phone = body.contact == "phone" or (body.contact == "auto" and bool(el.phone_number_id()))
    if use_phone:
        # A public page must not ring arbitrary numbers on the bank's line.
        target = _e164(body.destination or EL.demo_phone)
        if target not in {_e164(n) for n in EL.callable_numbers}:
            card_authorization.decline_pending_for_card(card["cardId"], "demo call not allowed")
            raise HTTPException(403, "That number is not approved for demo calls. Choose 'This browser' "
                                     "to take the bank's call here instead.")
    customer = bank.get_customer_by_id(auth.customer_id)
    session = _new_session(
        f"live-{auth.id[:8]}", customer, card_authorization.signal_from(auth, card), card,
        language=language, channel="phone" if use_phone else "web",
    )
    logger.info("Authorization %s flagged (%s) -- session %s, contact=%s, language=%s",
                auth.reference_id, auth.severity, session.session_id,
                session.channel, language)

    if use_phone:
        session.call_state = "ringing"
        # Off the request thread: the browser gets its answer now, and a slow
        # telephony round trip cannot time the checkout out.
        threading.Thread(target=_place_call, args=(session, auth, body.destination),
                         daemon=True, name=f"call-{auth.reference_id}").start()
    else:
        session.call_state = "awaiting_browser"
        auth.call_placed = True

    return {
        "status": "PENDING",
        "reference": auth.reference_id,
        "severity": auth.severity,
        "reasons": auth.reasons,
        "session_id": session.session_id,
        "contact": session.channel,
    }


def _e164(number: str | None) -> str:
    """Nigerian local format (080...) to E.164; spaces dropped."""
    n = (number or "").strip().replace(" ", "")
    if n.startswith("0") and len(n) == 11:
        n = "+234" + n[1:]
    return n


def _place_call(session: Session, auth: card_authorization.PendingAuthorization,
                destination: str | None) -> None:
    number = _e164(destination or EL.demo_phone or session.customer.get("phoneNumber", ""))
    try:
        result = el.outbound_call(to_number=number, dynamic_variables=session.dynamic_variables(),
                                  language=session.language)
    except el.ElevenLabsError as exc:
        logger.error("Could not place call: %s", exc)
        session.call_state, session.call_error = "failed", str(exc)
        auth.call_placed, auth.call_error = False, str(exc)
        return
    session.conversation_id = result.get("conversation_id")
    auth.call_placed = result["placed"]
    if not result["placed"]:
        session.call_state, session.call_error = "failed", result.get("detail")
        auth.call_error = result.get("detail")
    logger.info("Call to ***%s: %s (conversation %s)", number[-4:], result.get("detail"),
                session.conversation_id)


@app.get("/api/web-session/{session_id}")
def web_session(session_id: str) -> dict:
    """
    Everything the browser needs to take the call itself.

    A signed URL keeps the agent private -- the page never holds an API key --
    and the dynamic variables are the same briefing a phone call would carry.
    """
    session = _SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, "no such call")
    try:
        url = el.signed_url()
    except el.ElevenLabsError as exc:
        raise HTTPException(503, str(exc)) from exc
    session.channel = "web"
    return {"signed_url": url, "dynamic_variables": session.dynamic_variables(),
            "language": session.language}


@app.post("/api/web-session/{session_id}/started")
def web_session_started(session_id: str, body: dict) -> dict:
    """The browser reports the conversation id so the recording can be filed against the case."""
    session = _SESSIONS.get(session_id)
    if session is not None:
        session.conversation_id = session.conversation_id or body.get("conversation_id")
        session.call_state = "in_call"
    return {"ok": True}


@app.get("/api/payments/{reference}")
def payment_status(reference: str) -> dict:
    """What the checkout shows while the bank decides."""
    rows = [r for c in ("CUS-100001", "CUS-100002", "CUS-100003", "CUS-100004", "CUS-100005")
            for r in card_authorization.for_customer(c) if r["referenceId"] == reference]
    if not rows:
        raise HTTPException(404, "unknown payment")
    row = rows[0]
    session = next((s for s in _SESSIONS.values()
                    if s.signal and reference in s.signal.get("transactionRefs", [])), None)
    return {
        "status": row["status"],
        "stage": row["stage"],
        "call_state": session.call_state if session else "ended",
        "call_error": (session.call_error if session else None) or row.get("call_error"),
        "verified": session.challenge.verified if session else None,
        "handed_to_human": session.handed_to_human if session else False,
    }


# --------------------------------------------------------------------------
# Tools: briefing and verification
# --------------------------------------------------------------------------


@app.post("/tools/get_call_briefing", dependencies=[Depends(require_token)])
def get_call_briefing(body: ToolBody) -> dict:
    session = _resolve(body.session_id, body.conversation_id)
    return {"result": {
        "first_name": session.customer.get("firstName"),
        "card_last4": (session.card or {}).get("last4"),
        "has_flagged_activity": session.signal is not None,
        "verified": session.challenge.verified,
        "attempts_remaining": session.challenge.attempts_remaining,
        "actions_taken": session.actions_taken,
    }}


@app.post("/tools/ask_security_question", dependencies=[Depends(require_token)])
def ask_security_question(body: ToolBody) -> dict:
    """Return the question to put to the customer. The answer never leaves this process."""
    session = _resolve(body.session_id, body.conversation_id)
    c = session.challenge
    if c.verified:
        return {"result": "Already verified. Do not ask again."}
    if c.exhausted:
        return {"result": "No attempts remain. Call transfer_to_human_agent."}
    if c.pending_unanswered:
        return {"result": f"You already asked: {c.pending_question}. Pass their reply to check_security_answer."}
    try:
        question = c.next_question()
    except RuntimeError:
        return {"result": "No security questions on file; this customer cannot be verified. "
                          "Call transfer_to_human_agent."}
    return {"result": f"Ask exactly this (in the customer's language): {question}\n"
                      f"Attempts remaining: {c.attempts_remaining}."}


class CheckAnswer(ToolBody):
    answer: str
    # Booleans sometimes arrive quoted from templated bodies; accept both.
    matches: bool | str

    @property
    def matched(self) -> bool:
        if isinstance(self.matches, bool):
            return self.matches
        return self.matches.strip().lower() in {"true", "yes", "1", "match"}


@app.post("/tools/check_security_answer", dependencies=[Depends(require_token)])
def check_security_answer(body: CheckAnswer) -> dict:
    """The model judges the match; this counts the attempt. See ChallengeFlow.record."""
    session = _resolve(body.session_id, body.conversation_id)
    if session.challenge.verified:
        return {"result": "Already verified. Call describe_suspicious_activity."}
    if not session.challenge.pending_unanswered:
        # The model asked a question the bank never issued -- on a live call it
        # once invented "your mother's maiden name". An answer to a question we
        # did not set proves nothing, so it cannot verify anyone, and it does
        # not cost the customer an attempt either.
        return {"result": "You have not been given a security question. Do not make one up. Call "
                          "ask_security_question and ask exactly what it returns."}
    if session.challenge.record(body.matched, body.answer):
        if session.card:
            for auth in card_authorization.pending_for_card(session.card["cardId"]):
                auth.verified = True
        return {"result": "Correct. Say 'thank you, that is correct', then call describe_suspicious_activity."}
    if session.challenge.exhausted:
        return {"result": "That was the last attempt and verification has failed. Do not act on the "
                          "account. Call transfer_to_human_agent now."}
    return {"result": "That did not match. Call ask_security_question for a DIFFERENT question. "
                      f"Attempts remaining: {session.challenge.attempts_remaining}."}


@app.post("/tools/describe_suspicious_activity", dependencies=[Depends(require_token)])
def describe_suspicious_activity(body: ToolBody) -> dict:
    """
    The held payment, for the agent to describe -- only once verified.

    Gated here as well as in the prompt: the merchant and amount are exactly
    what someone holding a stolen phone would want confirmed.
    """
    session = _resolve(body.session_id, body.conversation_id)
    if session.signal is None:
        return {"result": "Nothing was flagged on this account. Do not describe anything; ask what they need."}
    if blocked := _require_verified(session):
        return {"result": blocked}
    session.described = True
    lines = [_name_countries(session.signal.get("summary", ""))]
    for trx in session.signal.get("transactions", []):
        lines.append(f"- {trx.get('referenceId')}: {trx.get('amount')} at {trx.get('merchant')} "
                     f"in {_COUNTRIES.get(str(trx.get('location')), trx.get('location'))}")
    return {"result": "\n".join(line for line in lines if line)}


# Countries by name, never by code: given "UA", the model told an Arabic-speaking
# customer the payment was in Uganda. A name leaves nothing to guess.
_COUNTRIES = {"UA": "Ukraine", "AE": "the United Arab Emirates", "US": "the United States",
              "NG": "Nigeria", "GB": "the United Kingdom", "SA": "Saudi Arabia", "EG": "Egypt"}


def _name_countries(text: str) -> str:
    return re.sub(r"\bin ([A-Z]{2})\b", lambda m: f"in {_COUNTRIES.get(m.group(1), m.group(1))}", text)


# --------------------------------------------------------------------------
# Tools: pre-approved, reversible actions
# --------------------------------------------------------------------------


class FreezeCard(ToolBody):
    reason: str


@app.post("/tools/freeze_card", dependencies=[Depends(require_token)])
def freeze_card(body: FreezeCard) -> dict:
    """Temporarily block the card and decline what it was paying. Pre-approved, reversible by the bank."""
    session = _resolve(body.session_id, body.conversation_id)
    if blocked := _require_described(session):
        return {"result": blocked}
    if not session.card:
        return {"result": "There is no card on this account to freeze."}
    if session.card.get("status") != "ACTIVE":
        return {"result": "Already frozen and the payment is already declined. Say so, then wrap up."}
    try:
        card = bank.block_card(session.card["cardId"], reason=body.reason, actor="agent")
    except (PermissionDenied, NotFound) as exc:
        return {"result": f"The bank refused that: {exc}"}
    session.actions_taken.append(f"froze card {card['cardId']}: {body.reason}")
    # A freeze that left the held payment authorized would let the fraud
    # complete anyway -- the one outcome the call exists to prevent.
    declined = card_authorization.decline_pending_for_card(card["cardId"], f"card frozen: {body.reason}")
    if declined:
        session.actions_taken.append(f"declined pending payment {', '.join(declined)}")
    session.outcome = "card_frozen"
    _schedule_officer_report(session)
    return {"result": f"Frozen. Tell them: the card ending {card.get('last4')} is now frozen and the "
                      "payment has been declined. A person can lift the freeze later. Then wrap up."}


class ReduceLimit(ToolBody):
    new_daily_limit: str


@app.post("/tools/reduce_card_limit", dependencies=[Depends(require_token)])
def reduce_card_limit(body: ReduceLimit) -> dict:
    """Lower the daily limit. Pre-approved; it can only ever go down."""
    session = _resolve(body.session_id, body.conversation_id)
    if blocked := _require_verified(session):
        return {"result": blocked}
    if not session.card:
        return {"result": "There is no card on this account."}
    try:
        bank.set_card_limit(session.card["cardId"], daily_limit=body.new_daily_limit, actor="agent")
    except PermissionDenied as exc:
        return {"result": f"The bank refused that: {exc}"}
    except (NotFound, ValueError) as exc:
        return {"result": f"That did not work: {exc}"}
    session.actions_taken.append(f"limit lowered to {body.new_daily_limit}")
    return {"result": f"The daily limit is now {body.new_daily_limit}. Only a person can raise it again."}


class FlagTransaction(ToolBody):
    reference_id: str
    note: str


@app.post("/tools/flag_transaction", dependencies=[Depends(require_token)])
def flag_transaction(body: FlagTransaction) -> dict:
    """Record a dispute for the fraud team. Moves no money."""
    session = _resolve(body.session_id, body.conversation_id)
    if blocked := _require_verified(session):
        return {"result": blocked}
    ref = body.reference_id.strip()
    if ref.startswith("AUTH-"):
        # A held authorization is not a booked transaction; the freeze already
        # declined it, so record the dispute on the case instead.
        session.actions_taken.append(f"disputed {ref}: {body.note}")
        return {"result": f"{ref} is recorded as disputed for the fraud team. Do not promise a refund."}
    try:
        bank.flag_transaction(ref, note=body.note, actor="agent")
    except PermissionDenied as exc:
        return {"result": f"The bank refused that: {exc}"}
    except NotFound as exc:
        return {"result": f"No such transaction: {exc}"}
    session.actions_taken.append(f"flagged {ref}")
    return {"result": f"{ref} is flagged for the fraud team. Do not promise a refund; a person will review it."}


class ApproveTransaction(ToolBody):
    reference_id: str | None = None


@app.post("/tools/approve_transaction", dependencies=[Depends(require_token)])
def approve_transaction(body: ApproveTransaction) -> dict:
    """Let a held payment through because the verified cardholder says it was them."""
    session = _resolve(body.session_id, body.conversation_id)
    if blocked := _require_described(session):
        return {"result": blocked}
    if not session.card:
        return {"result": "There is no card on this account."}
    wanted = (body.reference_id or "").strip()
    approved = [
        a.reference_id for a in card_authorization.pending_for_card(session.card["cardId"])
        if (not wanted or a.reference_id == wanted)
        and card_authorization.approve(a.id, "customer confirmed on the call")
    ]
    if not approved:
        return {"result": "There is no payment waiting on this card."}
    session.actions_taken.append(f"approved payment {', '.join(approved)}")
    session.outcome = "payment_approved"
    _schedule_officer_report(session)
    return {"result": f"{', '.join(approved)} is approved and the card stays active. Tell them the payment "
                      "will complete, then wrap up."}


# --------------------------------------------------------------------------
# Tools: hand-over and close
# --------------------------------------------------------------------------


class Transfer(ToolBody):
    reason: str


@app.post("/tools/transfer_to_human_agent", dependencies=[Depends(require_token)])
def transfer_to_human_agent(body: Transfer) -> dict:
    """
    Open a case for the human fraud team with everything done so far.

    The case is what lets the person who picks up start from context instead of
    asking the customer to repeat everything. The live transfer itself is the
    platform's transfer_to_number system tool, on a phone call.
    """
    session = _resolve(body.session_id, body.conversation_id)
    if not session.case_id:
        case = bank.create_fraud_case(
            customer_id=session.customer["customerId"],
            signal_id=session.signal["signalId"] if session.signal else None,
            summary=f"Escalated by agent: {body.reason}",
            actions_taken=session.actions_taken,
            needs_human=True,
        )
        session.case_id = case.get("caseId")
    session.handed_to_human = True
    session.outcome = session.outcome or "escalated_to_human"
    _schedule_officer_report(session, escalation=body.reason)
    live = session.channel == "phone" and "transfer_to_number" in _transfer_tools_enabled()
    next_step = ("Now call transfer_to_number to put them through to the fraud desk."
                 if live else
                 "Tell them a person from the fraud team will call them back, confirm anything already "
                 "done on the card, and wrap up.")
    return {"result": f"Case {session.case_id} is open for the fraud team. {next_step} "
                      "Do not promise a refund or a timescale.", "case_id": session.case_id}


def _transfer_tools_enabled() -> set[str]:
    desk = (settings.bank.fraud_desk_number or "").strip()
    return {"transfer_to_number"} if desk and desk != "+2348000000000" else set()


class Outcome(ToolBody):
    summary: str


@app.post("/tools/log_call_outcome", dependencies=[Depends(require_token)])
def log_call_outcome(body: Outcome) -> dict:
    """
    Close the call out in the bank's records.

    A case is written for every call, not only escalations: a call that froze a
    card acted on a real account, and the audit trail must show it.
    """
    session = _resolve(body.session_id, body.conversation_id)
    if not session.case_id:
        case = bank.create_fraud_case(
            customer_id=session.customer["customerId"],
            signal_id=session.signal["signalId"] if session.signal else None,
            summary=body.summary,
            actions_taken=session.actions_taken,
            needs_human=False,
        )
        session.case_id = case.get("caseId")
    session.outcome = session.outcome or "no_action"
    session.actions_taken.append(f"outcome: {body.summary}")
    _schedule_officer_report(session)
    return {"result": "Recorded. Thank them and call end_call."}


# --------------------------------------------------------------------------
# Officer report: the person who owns the relationship hears it from the bank
# --------------------------------------------------------------------------


def _schedule_officer_report(session: Session, escalation: str | None = None) -> None:
    """Once per call, after the call ends, so the report describes the final state."""
    if session.officer_reported:
        return
    session.officer_reported = True
    threading.Thread(target=_send_officer_report, args=(session, escalation),
                     daemon=True, name="officer-report").start()


def _send_officer_report(session: Session, escalation: str | None) -> None:
    try:
        if session.conversation_id:
            el.wait_for_end(session.conversation_id, timeout_s=300)
        session.call_state = "ended"
        text = _summary(session, escalation)
        logger.info("Officer report:\n%s", text)
        _whatsapp_officer(text)
    except Exception as exc:  # noqa: BLE001 - a lost report must not crash the server
        logger.error("Officer report failed: %s: %s", type(exc).__name__, exc)


def _summary(session: Session, escalation: str | None) -> str:
    customer, card = session.customer, session.card or {}
    name = f"{customer.get('firstName', '')} {customer.get('lastName', '')}".strip()
    head = f"{settings.bank.name.upper()} - " + (
        f"ESCALATION {session.case_id}" if escalation else "fraud call handled")
    parts = [head, ""]
    if escalation:
        parts.append(f"Reason: {escalation}")
    parts += [
        f"Customer: {name}, {customer.get('phoneNumber')}",
        f"Card ending {card.get('last4', '?')}: {card.get('status', '?')}",
        f"Verified on call: {'yes' if session.challenge.verified else 'NO - verify before acting'}",
        f"Language: {session.language}",
        "Agent did: " + ("; ".join(session.actions_taken) or "nothing"),
    ]
    if session.conversation_id:
        parts.append(f"Recording: conversation {session.conversation_id}")
    if escalation:
        parts += ["", "Please call the customer back."]
    return "\n".join(parts)


def _whatsapp_officer(text: str) -> None:
    """Meta Cloud API text message to the officer. Best effort; logged either way."""
    wa = settings.whatsapp
    number = wa.officer_phone
    if not (number and wa.token and wa.phone_number_id):
        logger.info("Officer WhatsApp not configured; report kept in the log and the case")
        return
    try:
        r = httpx.post(
            f"{wa.graph_url}/messages",
            headers={"Authorization": f"Bearer {wa.token}"},
            json={"messaging_product": "whatsapp", "to": number.lstrip("+"),
                  "type": "text", "text": {"body": text[:4096]}},
            timeout=20,
        )
        logger.info("Officer WhatsApp -> %s %s", r.status_code, r.text[:200])
    except httpx.RequestError as exc:
        logger.warning("Officer WhatsApp failed: %s", exc)


# --------------------------------------------------------------------------
# Audit: post-call webhook from ElevenLabs
# --------------------------------------------------------------------------


@app.post("/webhooks/elevenlabs")
async def post_call_webhook(request: Request) -> dict:
    """
    File every finished call: transcript, analysis, evaluation and audio.

    Signed by ElevenLabs with the workspace webhook secret; anything unsigned
    is refused, because an audit trail anyone can write to is not one.
    Deduplicated by conversation id -- the platform retries deliveries.
    """
    raw = await request.body()
    if not el.verify_webhook(raw, request.headers.get("elevenlabs-signature")):
        raise HTTPException(401, "bad signature")
    event = json.loads(raw)
    kind, data = event.get("type"), event.get("data") or {}
    cid = data.get("conversation_id") or "unknown"

    if kind == "post_call_audio":
        import base64
        (AUDIT_DIR / f"{cid}.mp3").write_bytes(base64.b64decode(data.get("full_audio") or ""))
        logger.info("Audit audio filed for %s", cid)
        return {"ok": True}

    if kind == "post_call_transcription":
        session = next((s for s in _SESSIONS.values() if s.conversation_id == cid), None)
        dyn = ((data.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {})
        record = {
            "conversation_id": cid,
            "received_at": datetime.now(UTC).isoformat(),
            "session_id": dyn.get("session_id") or (session.session_id if session else None),
            "customer_first_name": dyn.get("customer_first_name"),
            "case_id": session.case_id if session else None,
            "actions_taken": session.actions_taken if session else None,
            "status": data.get("status"),
            "metadata": data.get("metadata"),
            "analysis": data.get("analysis"),
            "transcript": data.get("transcript"),
        }
        (AUDIT_DIR / f"{cid}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        if session:
            session.call_state = "ended"
        # The transcript is re-checked against the credential guard after the
        # fact, so a breach is visible in the audit record even if the live
        # guardrail somehow let one through.
        breaches = [t.get("message") for t in record["transcript"] or []
                    if t.get("role") == "agent" and mentions_forbidden_credential(t.get("message") or "")]
        if breaches:
            logger.error("CREDENTIAL REQUEST in %s: %s", cid, breaches)
        logger.info("Audit transcript filed for %s", cid)
        return {"ok": True}

    logger.info("Webhook %s for %s ignored", kind, cid)
    return {"ok": True}


@app.get("/api/audit")
def audit_list() -> list[dict]:
    out = []
    for path in sorted(AUDIT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
        rec = json.loads(path.read_text(encoding="utf-8"))
        analysis = rec.get("analysis") or {}
        out.append({
            "conversation_id": rec["conversation_id"],
            "received_at": rec.get("received_at"),
            "customer": rec.get("customer_first_name"),
            "case_id": rec.get("case_id"),
            "summary": analysis.get("transcript_summary"),
            "call_successful": analysis.get("call_successful"),
            "evaluation": {k: (v or {}).get("result")
                           for k, v in (analysis.get("evaluation_criteria_results") or {}).items()},
            "data": {k: (v or {}).get("value")
                     for k, v in (analysis.get("data_collection_results") or {}).items()},
            "has_audio": (AUDIT_DIR / f"{rec['conversation_id']}.mp3").exists(),
            "turns": len(rec.get("transcript") or []),
        })
    return out


@app.get("/api/audit/{conversation_id}")
def audit_record(conversation_id: str) -> dict:
    path = AUDIT_DIR / f"{Path(conversation_id).name}.json"
    if not path.exists():
        # No webhook yet (e.g. none configured): read it from the platform.
        try:
            conv = el.get_conversation(conversation_id)
        except el.ElevenLabsError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"conversation_id": conversation_id, "transcript": conv.get("transcript"),
                "analysis": conv.get("analysis"), "source": "elevenlabs api"}
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/audit/{conversation_id}/audio")
def audit_audio(conversation_id: str):
    path = AUDIT_DIR / f"{Path(conversation_id).name}.mp3"
    if path.exists():
        return FileResponse(path, media_type="audio/mpeg")
    try:
        audio = el.request("GET", f"v1/convai/conversations/{conversation_id}/audio", timeout=60)
    except el.ElevenLabsError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(content=audio, media_type="audio/mpeg")


# --------------------------------------------------------------------------
# Custom LLM pass-through
# --------------------------------------------------------------------------
#
# The agent's own model (EL_LLM=custom) is a Gemma deployment on vLLM that
# returns its empty thought channel inside `content` --
# "<|channel>thought\n<channel|>" before every reply that follows a tool result
# -- and the voice reads it aloud. chat_template_kwargs.enable_thinking=false
# does not stop it; the proper fix is vLLM's reasoning parser on that server.
# Until then ElevenLabs calls this endpoint, which forwards to the model and
# strips the marker. It also keeps the model's key on this server rather than
# on the voice platform.

# The full marker, and the bare fragments the model emits around tool calls.
_THOUGHT_RE = re.compile(r"<\|channel>(thought)?\s*(<channel\|>)?|<channel\|>")
_OPENERS = ("<|channel>thought", "<channel|>")


def _strip_thought(text: str) -> str:
    return _THOUGHT_RE.sub("", text)


def _unsafe_tail(text: str) -> int:
    """
    Length of a trailing fragment that might still grow into a marker.

    Covers a partial opener ("<|chan") and a complete marker that may yet be
    followed by "<channel|>" -- e.g. "<|channel>thought" with whitespace after.
    """
    m = re.search(r"<\|channel>(thought)?\s*$", text)
    if m:
        return len(text) - m.start()
    for n in range(min(len(text), max(map(len, _OPENERS))), 0, -1):
        tail = text[-n:]
        if any(o.startswith(tail) for o in _OPENERS):
            return n
    return 0


@app.post("/llm/v1/chat/completions", dependencies=[Depends(require_token)])
async def llm_proxy(request: Request):
    from fastapi.responses import StreamingResponse

    body = await request.json()
    upstream = f"{EL.custom_llm_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {EL.custom_llm_key}"}
    body["model"] = EL.custom_llm_model or body.get("model")

    if not body.get("stream"):
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(upstream, json=body, headers=headers)
        data = r.json()
        for choice in data.get("choices") or []:
            msg = choice.get("message") or {}
            if isinstance(msg.get("content"), str):
                msg["content"] = _strip_thought(msg["content"])
        return JSONResponse(data, status_code=r.status_code)

    async def stream():
        # A marker can arrive split across chunks, anywhere in the reply, so
        # each choice keeps back only a tail that could still become one; the
        # rest is released as soon as it arrives.
        held: dict[int, str] = {}
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", upstream, json=body, headers=headers) as r:
                async for line in r.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        if line:
                            yield line + "\n\n"
                        continue
                    chunk = json.loads(line[6:])
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        idx = choice.get("index", 0)
                        text = delta.get("content")
                        finishing = choice.get("finish_reason") is not None or delta.get("tool_calls")
                        if not isinstance(text, str) and not (finishing and held.get(idx)):
                            continue
                        # Decide what to hold back on the RAW text: stripping a
                        # lone "<|channel>" first would release the "thought"
                        # that follows it in the next chunk.
                        buf = held.pop(idx, "") + (text or "")
                        keep = 0 if finishing else _unsafe_tail(buf)
                        if keep:
                            held[idx] = buf[-keep:]
                            buf = buf[:-keep]
                        delta["content"] = _strip_thought(buf)
                    yield "data: " + json.dumps(chunk) + "\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# --------------------------------------------------------------------------
# Guardrail check, kept for auditing transcripts and tests
# --------------------------------------------------------------------------


class GuardCheck(BaseModel):
    text: str


@app.post("/tools/check_utterance", dependencies=[Depends(require_token)])
def check_utterance(body: GuardCheck) -> dict:
    found = mentions_forbidden_credential(body.text)
    return {"forbidden": found is not None, "credential": found}


# --------------------------------------------------------------------------
# Demo plumbing
# --------------------------------------------------------------------------


@app.post("/api/demo/reset")
def demo_reset() -> dict:
    """Back to a quiet account. Only touches the simulated bank and this process."""
    card_authorization.reset()
    _SESSIONS.clear()
    restored = []
    for customer_id in ("CUS-100001", "CUS-100002", "CUS-100003", "CUS-100004", "CUS-100005"):
        for card in bank.get_cards_by_customer(customer_id):
            if card.get("status") != "ACTIVE":
                # Straight through the data layer: unblock_card is human-only in
                # bank_api and must stay that way for the agent.
                card["status"] = "ACTIVE"
                restored.append(card["cardId"])
    return {"status": "reset", "cards_reactivated": restored}


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "sessions": len(_SESSIONS),
        "token_configured": bool(EL.tool_token),
        "agent_id": el.agent_id() or None,
        "telephony": bool(el.phone_number_id()),
        "webhook_secret": bool(EL.webhook_secret or (el.load_state().get("webhook") or {}).get("secret")),
        "time": datetime.now(UTC).isoformat(),
    }


def _mask_phone(number: str | None) -> str:
    if not number or len(number) < 7:
        return "***"
    return f"{number[:4]}***{number[-3:]}"


@app.get("/api/dashboard/{customer_id}")
def dashboard_data(customer_id: str) -> dict:
    """Everything the bank console shows about one customer, read fresh each poll."""
    try:
        customer = bank.get_customer_by_id(customer_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    accounts = bank.get_accounts_by_customer(customer_id)
    account = accounts[0] if accounts else None
    transactions = ([t for t in bank.get_statement(account["accountNumber"], last_n=8) if not t.get("flagged")]
                    if account else [])
    sessions = [s for s in {id(v): v for v in _SESSIONS.values()}.values()
                if s.customer.get("customerId") == customer_id]
    return {
        "customer": {
            "id": customer["customerId"],
            "name": f"{customer.get('firstName', '')} {customer.get('lastName', '')}".strip(),
            "phone": _mask_phone(customer.get("phoneNumber")),
            "city": customer.get("city"),
        },
        "account": account,
        "cards": bank.get_cards_by_customer(customer_id),
        "transactions": transactions,
        "pending": card_authorization.for_customer(customer_id),
        "signals": [s.signal for s in sessions if s.signal],
        "cases": [k for k in bank.get_fraud_cases() if k["customerId"] == customer_id],
        "live_calls": [{
            "session_id": s.session_id,
            "call_state": s.call_state,
            "channel": s.channel,
            "language": s.language,
            "conversation_id": s.conversation_id,
            "verified": s.challenge.verified,
            "attempts_remaining": s.challenge.attempts_remaining,
            "actions_taken": s.actions_taken,
            "handed_to_human": s.handed_to_human,
        } for s in sessions],
        "fetched_at": datetime.now(UTC).isoformat(),
    }


def _bank_brand() -> str:
    """'Sora Bank' -> 'Sora<span>Bank</span>': the header's two-tone wordmark."""
    head, _, tail = settings.bank.name.partition(" ")
    return f"{head}<span>{tail}</span>" if tail else f"{head}<span>Bank</span>"


def _brand(html: str, customer_id: str = "CUS-100001") -> str:
    page = (html.replace("__BANK_BRAND__", _bank_brand())
                .replace("__BANK_NAME__", settings.bank.name)
                .replace("__AGENT_NAME__", settings.bank.agent_display_name)
                .replace("__CUSTOMER_ID__", customer_id))
    if ROOT:
        # Behind a path prefix the page's API calls must carry it: they go to
        # NOBA_API_BASE + "/api/...", so set the base to the prefix.
        page = page.replace("<script>", f'<script>window.NOBA_API_BASE = "{ROOT}";</script>\n<script>', 1)
        for attr in ('href="/', 'src="/'):
            page = page.replace(attr, attr[:-1] + ROOT + "/")
    return page


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(f"{ROOT}/checkout")


@app.get("/checkout", response_class=HTMLResponse)
def checkout() -> HTMLResponse:
    """The operations screen, opened at its checkout: where the payment is attempted."""
    return HTMLResponse(_brand(CHECKOUT_HTML))


@app.get("/ui", response_class=HTMLResponse)
@app.get("/ui/{customer_id}", response_class=HTMLResponse)
def console(customer_id: str = "CUS-100001") -> HTMLResponse:
    """The bank's fraud operations console: the card, the call, the audit trail."""
    return HTMLResponse(_brand(CONSOLE_HTML, customer_id))
