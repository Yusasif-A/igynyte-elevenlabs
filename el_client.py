"""
The few ElevenLabs Agents API calls this project makes, in one place.

Plain HTTP rather than the SDK: the agent definition in el_setup.py is written
as the JSON the API documents, and sending it as-is means what is in the file is
exactly what the platform receives -- no model layer quietly dropping a field
the SDK version does not know about yet.

STATE FILE
el_setup.py creates tools, knowledge-base documents, a secret and the agent, and
records their ids in .elevenlabs_state.json so a second run updates them in
place instead of creating duplicates. The tool server reads the agent id from
there too, so `python el_setup.py` is the only step between a fresh account and
a working call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from config import settings

logger = logging.getLogger("fraud_agent.el_client")

STATE_PATH = Path(__file__).with_name(".elevenlabs_state.json")


class ElevenLabsError(Exception):
    """The platform refused a request. The message carries its reason."""


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def agent_id() -> str:
    """The env var wins, so a deployment can pin an agent without the state file."""
    return settings.elevenlabs.agent_id or load_state().get("agent_id", "")


def phone_number_id() -> str:
    """An imported number to dial from: the env var, else the SIP number el_setup imported."""
    return settings.elevenlabs.phone_number_id or load_state().get("phone_number_id", "")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def request(method: str, path: str, *, json_body: Any = None, params: dict | None = None,
            timeout: float = 30) -> Any:
    key = settings.elevenlabs.api_key
    if not key:
        raise ElevenLabsError("PAID_ELEVENLABS_API_KEY is not set")
    r = httpx.request(
        method,
        f"{settings.elevenlabs.base_url}/{path.lstrip('/')}",
        headers={"xi-api-key": key},
        json=json_body,
        params=params,
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise ElevenLabsError(f"{method} {path} -> {r.status_code}: {r.text[:1500]}")
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return r.content


# --------------------------------------------------------------------------
# Calls
# --------------------------------------------------------------------------


def phone_provider(phone_number_id: str) -> str:
    """'twilio' or 'sip_trunk' -- they are dialled through different endpoints."""
    number = request("GET", f"v1/convai/phone-numbers/{phone_number_id}")
    return (number or {}).get("provider", "twilio")


def outbound_call(*, to_number: str, dynamic_variables: dict[str, Any],
                  language: str = "en") -> dict:
    """
    Ring a customer with the agent, briefed for this call.

    The briefing rides in `dynamic_variables`, so the agent knows who it rang
    from the first word -- the opening line is spoken before any tool runs. The
    `session_id` variable is bound into every server tool's body, so each tool
    call names its own session instead of the server having to guess.
    """
    phone_id = phone_number_id()
    if not phone_id:
        raise ElevenLabsError(
            "ELEVENLABS_PHONE_NUMBER_ID is not set. Import a Twilio or SIP number "
            "in ElevenAgents > Phone Numbers, or take the call in the browser."
        )
    aid = agent_id()
    if not aid:
        raise ElevenLabsError("No agent yet. Run `python el_setup.py` first.")

    provider = phone_provider(phone_id)
    path = "v1/convai/sip-trunk/outbound-call" if provider == "sip_trunk" else "v1/convai/twilio/outbound-call"
    body = {
        "agent_id": aid,
        "agent_phone_number_id": phone_id,
        "to_number": to_number,
        "conversation_initiation_client_data": {
            "dynamic_variables": dynamic_variables,
            "conversation_config_override": {"agent": {"language": language}},
        },
    }
    if provider != "sip_trunk":
        # Recorded at the carrier as well as by the platform: a regulated call
        # needs the audit copy even if the agent-side recording is ever purged.
        body["call_recording_enabled"] = True
    result = request("POST", path, json_body=body)
    return {
        "placed": bool(result.get("success")),
        "conversation_id": result.get("conversation_id"),
        "call_sid": result.get("callSid") or result.get("sip_call_id"),
        "detail": result.get("message"),
    }


def signed_url() -> str:
    """A short-lived URL the browser uses to open a session with the private agent."""
    aid = agent_id()
    if not aid:
        raise ElevenLabsError("No agent yet. Run `python el_setup.py` first.")
    out = request("GET", "v1/convai/conversation/get-signed-url", params={"agent_id": aid})
    return out["signed_url"]


def get_conversation(conversation_id: str) -> dict:
    return request("GET", f"v1/convai/conversations/{conversation_id}")


def conversation_ended(conversation: dict) -> bool:
    return conversation.get("status") in {"done", "failed"}


def wait_for_end(conversation_id: str, timeout_s: float = 300.0) -> dict | None:
    """Poll until the platform marks the conversation finished, or give up."""
    deadline = time.monotonic() + timeout_s
    last: dict | None = None
    while time.monotonic() < deadline:
        try:
            last = get_conversation(conversation_id)
        except ElevenLabsError as exc:
            logger.warning("Could not read conversation %s: %s", conversation_id, exc)
            return last
        if conversation_ended(last):
            return last
        time.sleep(5)
    return last


def transcript_text(conversation: dict) -> str:
    lines = []
    for turn in conversation.get("transcript") or []:
        text = (turn.get("message") or "").strip()
        if text:
            who = "Agent" if turn.get("role") == "agent" else "Customer"
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Post-call webhook signature
# --------------------------------------------------------------------------


def verify_webhook(raw_body: bytes, signature_header: str | None, *,
                   tolerance_s: int = 30 * 60) -> bool:
    """
    Check an `ElevenLabs-Signature: t=<unix>,v0=<hex>` header.

    The digest is HMAC-SHA256 of "<t>.<raw body>" under the webhook secret. The
    timestamp bound stops an old, validly signed delivery being replayed into
    the audit trail.
    """
    secret = settings.elevenlabs.webhook_secret or (load_state().get("webhook") or {}).get("secret")
    if not secret or not signature_header:
        return False
    parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
    ts, sig = parts.get("t"), parts.get("v0")
    if not ts or not sig or not ts.isdigit():
        return False
    if abs(time.time() - int(ts)) > tolerance_s:
        return False
    expected = hmac.new(
        secret.encode(), f"{ts}.{raw_body.decode('utf-8')}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)
