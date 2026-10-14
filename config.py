"""
Configuration for the fraud-intervention agent, read from .env exactly once.

Three groups: the bank's identity and limits, the ElevenLabs platform, and the
WhatsApp number the officer reports go out on.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# override=True: the .env file on disk is the single source of truth. Without
# it a stale variable exported in the shell silently beats the file, and the
# tool server ends up pointed at the wrong agent or tunnel.
load_dotenv(override=True)

logger = logging.getLogger("fraud_agent.config")


def _env(key: str, default: str = "") -> str:
    # Values in .env are quoted inconsistently, so strip stray quotes.
    return (os.getenv(key) or default).strip().strip('"').strip("'")


@dataclass
class BankSettings:
    """Identity the agent presents, and the limits it operates under."""

    name: str = field(default_factory=lambda: _env("BANK_NAME", "Sora Bank"))
    agent_display_name: str = field(default_factory=lambda: _env("AGENT_DISPLAY_NAME", "Ada"))
    # Live hand-over target for transfer_to_number. The default is a
    # placeholder; el_setup only enables the transfer when it is changed.
    fraud_desk_number: str = field(default_factory=lambda: _env("FRAUD_DESK_NUMBER", "+2348000000000"))
    # A fraud call that drags on is a failed fraud call -- cap it.
    max_call_seconds: int = field(default_factory=lambda: int(_env("MAX_CALL_SECONDS", "300")))
    max_verification_attempts: int = field(
        default_factory=lambda: int(_env("MAX_VERIFICATION_ATTEMPTS", "3"))
    )


@dataclass
class WhatsAppSettings:
    """Meta WhatsApp Cloud API, used to send the officer a report of each call."""

    token: str = field(default_factory=lambda: _env("WHATSAPP_TOKEN"))
    phone_number_id: str = field(default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID"))
    graph_version: str = field(default_factory=lambda: _env("WHATSAPP_GRAPH_VERSION", "v23.0"))
    # The account officer who receives the report.
    officer_phone: str = field(default_factory=lambda: _env("OFFICER_PHONE"))

    @property
    def graph_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_version}/{self.phone_number_id}"


@dataclass
class ElevenLabsSettings:
    """
    The ElevenLabs Agents platform: STT, TTS, the LLM loop and telephony.

    PAID_ELEVENLABS_API_KEY is read first: the key needs ElevenAgents
    read/write permission, and a speech-only key is refused by every agents
    endpoint with "missing the permission convai_read".
    """

    api_key: str = field(
        default_factory=lambda: _env("PAID_ELEVENLABS_API_KEY") or _env("ELEVENLABS_API_KEY")
    )
    base_url: str = field(
        default_factory=lambda: _env("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io").rstrip("/")
    )
    # Written by el_setup.py into .elevenlabs_state.json; the env var wins so a
    # deployment can pin an agent without the state file.
    agent_id: str = field(default_factory=lambda: _env("ELEVENLABS_AGENT_ID"))
    # An imported Twilio or SIP-trunk number. Empty means no telephony yet: the
    # checkout offers the call in the browser instead of ringing a phone.
    phone_number_id: str = field(default_factory=lambda: _env("ELEVENLABS_PHONE_NUMBER_ID"))
    # Optional: el_setup creates the post-call webhook and keeps its secret in
    # the state file; set this only to use a webhook made in the dashboard.
    webhook_secret: str = field(default_factory=lambda: _env("ELEVENLABS_WEBHOOK_SECRET"))
    # The bearer token ElevenLabs sends on every server-tool call.
    tool_token: str = field(default_factory=lambda: _env("EL_TOOL_TOKEN"))
    # Public https URL of el_tools.py -- ElevenLabs calls the tools from its servers.
    tools_base_url: str = field(default_factory=lambda: _env("EL_TOOLS_BASE_URL").rstrip("/"))
    # Dial this instead of the number on file -- bank_data's numbers are fixtures.
    demo_phone: str = field(default_factory=lambda: _env("EL_DEMO_PHONE"))
    # The checkout is public, so it may only ring numbers on this list (comma
    # separated, E.164). Defaults to the demo and officer phones. Anyone else
    # takes the call in their browser.
    callable_numbers: list[str] = field(
        default_factory=lambda: [
            n.strip() for n in
            (_env("EL_CALLABLE_NUMBERS") or f"{_env('EL_DEMO_PHONE')},{_env('OFFICER_PHONE')}").split(",")
            if n.strip()
        ]
    )
    # Origins allowed to call the API from a browser: the Firebase-hosted pages.
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            o.strip() for o in
            _env("EL_ALLOWED_ORIGINS", "https://noba-bank.web.app,https://noba-bank.firebaseapp.com").split(",")
            if o.strip()
        ]
    )

    # A Twilio Elastic SIP trunk. When set and ELEVENLABS_PHONE_NUMBER_ID is
    # not, el_setup imports TWILIO_PHONE_NUMBER into ElevenLabs as an outbound
    # SIP number -- no Twilio account SID or token needed.
    sip_address: str = field(default_factory=lambda: _env("TWILIO_TERMINATION_URI"))
    sip_username: str = field(default_factory=lambda: _env("TWILIO_SIP_USERNAME"))
    sip_password: str = field(default_factory=lambda: _env("TWILIO_SIP_PASSWORD"))
    sip_number: str = field(default_factory=lambda: _env("TWILIO_PHONE_NUMBER"))

    # "custom" routes the agent through the OpenAI-compatible endpoint below
    # (via el_tools' /llm proxy); any other value is an ElevenLabs-hosted model.
    llm: str = field(default_factory=lambda: _env("EL_LLM", "custom"))
    custom_llm_url: str = field(default_factory=lambda: _env("PUBLICA_LLM_ENDPOINT"))
    custom_llm_model: str = field(default_factory=lambda: _env("PUBLICA_LLM_MODEL"))
    custom_llm_key: str = field(default_factory=lambda: _env("PUBLICA_LLM_API_KEY"))
    # Cascade order if the primary model errors or times out.
    llm_fallbacks: list[str] = field(
        default_factory=lambda: [
            m.strip()
            for m in _env("EL_LLM_FALLBACKS", "claude-haiku-5-5,gemini-3.5-flash").split(",")
            if m.strip()
        ]
    )

    tts_model: str = field(default_factory=lambda: _env("EL_TTS_MODEL", "eleven_v3_conversational"))
    # Chosen from the Voice Library for a calm, unhurried delivery: a fraud
    # call is frightening, and a bright or salesy voice reads as a scam.
    voice_en: str = field(default_factory=lambda: _env("EL_VOICE_EN", "OYTbf65OHHFELVut7v2H"))
    voice_ar: str = field(default_factory=lambda: _env("EL_VOICE_AR", "XTa3iQyMA6f1qrI4F6kZ"))


@dataclass
class Settings:
    bank: BankSettings = field(default_factory=BankSettings)
    whatsapp: WhatsAppSettings = field(default_factory=WhatsAppSettings)
    elevenlabs: ElevenLabsSettings = field(default_factory=ElevenLabsSettings)


settings = Settings()
