"""
Card authorizations, and the rules that decide whether one is suspicious.

WHY THIS IS SEPARATE FROM bank_api
A pending authorization is not a booked transaction. The card network has asked
"may this go through?" and the bank has not yet answered, so it must not appear
in a statement as money that moved. `bank_api` owns settled history; this owns
the in-flight moment, and the dashboard shows the two together with the pending
one clearly marked.

That distinction is the whole point of the demo. A fraud agent that rings you
after the money is gone is a receipt. One that rings you while the authorization
is still held can actually stop it -- so a freeze here declines a payment that
had not yet completed.

State is in-process and dies with the server, like the verification sessions in
el_tools. Same reasoning: a pending authorization that silently survives a
restart would be a payment nobody is holding open any more.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

logger = logging.getLogger("fraud_agent.card_authorization")

# Indicative rate, used only so a dollar amount can be compared against a Naira
# limit and read aloud in Naira on the call. A real system would take the rate
# from the scheme's settlement feed.
USD_TO_NAIRA = Decimal("1650")

PENDING = "PENDING"
DECLINED = "DECLINED"
APPROVED = "APPROVED"

# What the screen should say about a held authorization, which is not the same
# as its status. "PENDING" alone tells an operator nothing about whether anyone
# is doing something about it; these describe where in the flow it has reached.
STAGE_HELD = "Pending - held by bank"
STAGE_VERIFYING = "Suspicious - verifying with cardholder"
STAGE_VERIFIED = "Verified - awaiting cardholder's answer"
STAGE_DECLINED = "Declined - card frozen"
STAGE_APPROVED = "Approved - cardholder confirmed"


@dataclass
class PendingAuthorization:
    """One authorization the bank has been asked to approve."""

    id: str
    card_id: str
    customer_id: str
    account_number: str
    amount: Decimal
    currency: str
    merchant: str
    country_code: str
    channel: str
    created_at: str
    status: str = PENDING
    flagged: bool = False
    risk_type: str | None = None
    severity: str | None = None
    reasons: list[str] = field(default_factory=list)
    resolution: str | None = None
    # Set by the tool server as the call progresses, so the screen can show the
    # agent working rather than a static "PENDING".
    call_placed: bool = False
    call_error: str | None = None
    verified: bool = False

    @property
    def stage(self) -> str:
        if self.status == DECLINED:
            return STAGE_DECLINED
        if self.status == APPROVED:
            return STAGE_APPROVED
        if self.verified:
            return STAGE_VERIFIED
        if self.call_placed:
            return STAGE_VERIFYING
        return STAGE_HELD

    @property
    def naira_amount(self) -> Decimal:
        if self.currency.upper() in {"NGN", "NAIRA"}:
            return self.amount
        if self.currency.upper() == "USD":
            return self.amount * USD_TO_NAIRA
        return self.amount

    @property
    def reference_id(self) -> str:
        return f"AUTH-{self.id[:6].upper()}"

    def as_row(self) -> dict:
        """Shaped like a bank_api transaction so the dashboard can list both."""
        return {
            "referenceId": self.reference_id,
            "accountNumber": self.account_number,
            "amount": f"{self.amount:.2f}",
            "currency": self.currency.upper(),
            "naira_amount": f"{self.naira_amount:.2f}",
            "narration": f"CARD PURCHASE {self.merchant.upper()}",
            "channel": self.channel,
            "countryCode": self.country_code.upper(),
            "debitOrCredit": "DEBIT",
            "bookDate": self.created_at,
            "status": self.status,
            "stage": self.stage,
            "call_error": self.call_error,
            "flagged": self.flagged,
            "risk_type": self.risk_type,
            "severity": self.severity,
            "reasons": self.reasons,
            "resolution": self.resolution,
            "pending": self.status == PENDING,
        }


_AUTHORIZATIONS: dict[str, PendingAuthorization] = {}


def _parse_amount(raw: str | float | int) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not an amount: {raw!r}") from exc
    if value <= 0:
        raise ValueError("amount must be positive")
    return value


def evaluate(
    *,
    card: dict,
    amount_naira: Decimal,
    currency: str,
    country_code: str,
    home_countries: set[str],
) -> tuple[bool, str | None, str | None, list[str]]:
    """
    Decide whether this authorization should be challenged.

    Three signals, each individually weak and jointly strong -- which is why
    they are reported as a list rather than collapsed into a score. The agent
    reads these reasons aloud, and "a card that has only ever been used in
    Nigeria" is a sentence a customer can confirm or deny. "Risk score 0.87" is
    not.
    """
    reasons: list[str] = []
    country = country_code.upper()
    foreign = country not in home_countries

    if foreign:
        reasons.append(
            f"card-not-present purchase in {country}, on a card only ever "
            f"used in {'/'.join(sorted(home_countries))}"
        )
    if currency.upper() not in {"NGN", "NAIRA"}:
        reasons.append(f"charged in {currency.upper()}, not Naira")

    try:
        limit = Decimal(str(card.get("dailyLimit", "0")))
    except (InvalidOperation, ValueError):
        limit = Decimal("0")
    if limit > 0 and amount_naira > limit / 2:
        reasons.append(
            f"{amount_naira:,.0f} Naira is more than half the card's daily limit"
        )

    if not foreign:
        # Domestic spending over the limit is a limits problem, not fraud.
        return False, None, None, reasons

    severity = "HIGH" if len(reasons) >= 2 else "MEDIUM"
    return True, "CARD_NOT_PRESENT_FOREIGN", severity, reasons


def authorize(
    *,
    card: dict,
    account: dict,
    amount: str | float | int,
    currency: str,
    merchant: str,
    country_code: str,
    channel: str = "WEB",
    home_countries: set[str] | None = None,
) -> PendingAuthorization:
    """
    Hold an authorization and say whether it looks like fraud.

    Returns it PENDING either way. Nothing is auto-declined: the point of the
    agent is that a human being decides, and declining before they are asked
    would strand a customer abroad whose own purchase tripped a rule.
    """
    value = _parse_amount(amount)
    auth = PendingAuthorization(
        id=uuid.uuid4().hex,
        card_id=card["cardId"],
        customer_id=card["customerId"],
        account_number=account["accountNumber"],
        amount=value,
        currency=currency.upper(),
        merchant=merchant,
        country_code=country_code.upper(),
        channel=channel,
        created_at=datetime.now(UTC).isoformat(),
    )
    flagged, risk_type, severity, reasons = evaluate(
        card=card,
        amount_naira=auth.naira_amount,
        currency=auth.currency,
        country_code=auth.country_code,
        home_countries=home_countries or {"NG"},
    )
    auth.flagged = flagged
    auth.risk_type = risk_type
    auth.severity = severity
    auth.reasons = reasons
    _AUTHORIZATIONS[auth.id] = auth
    logger.info(
        "Authorization %s held: %s %s at %s (%s) flagged=%s",
        auth.reference_id,
        auth.currency,
        auth.amount,
        auth.merchant,
        auth.country_code,
        flagged,
    )
    return auth


def signal_from(auth: PendingAuthorization, card: dict) -> dict:
    """
    Describe this authorization the way bank_data describes a stored signal.

    Built here rather than written into bank_data because the signal did not
    exist until the customer pressed Send -- it is the live event, not a fixture.
    Shaped identically so the briefing and `describe_suspicious_activity` need no
    special case for it.
    """
    amount_words = f"{auth.naira_amount:,.0f} Naira"
    if auth.currency != "NGN":
        amount_words = f"{auth.amount:,.2f} {auth.currency} (about {amount_words})"
    return {
        "signalId": f"LIVE-{auth.reference_id}",
        "riskType": auth.risk_type or "CARD_NOT_PRESENT_FOREIGN",
        "severity": auth.severity or "HIGH",
        "customerId": auth.customer_id,
        "accountNumber": auth.account_number,
        "cardId": auth.card_id,
        "summary": (
            f"A payment of {amount_words} is being attempted right now at "
            f"{auth.merchant} in {auth.country_code} on the card ending "
            f"{card.get('last4', '')}. It has not gone through yet -- the bank "
            f"is holding it while we confirm with the customer."
        ),
        "transactionRefs": [auth.reference_id],
        "transactions": [
            {
                "referenceId": auth.reference_id,
                "amount": amount_words,
                "merchant": auth.merchant,
                "location": auth.country_code,
                "timestamp": auth.created_at,
            }
        ],
        "recommendedAction": "FREEZE_CARD",
        "reasons": auth.reasons,
        "live_authorization_id": auth.id,
    }


def pending_for_card(card_id: str) -> list[PendingAuthorization]:
    return [
        a
        for a in _AUTHORIZATIONS.values()
        if a.card_id == card_id and a.status == PENDING
    ]


def decline_pending_for_card(card_id: str, reason: str) -> list[str]:
    """
    Refuse every held authorization on a card.

    Called when the card is frozen: a freeze that left an in-flight payment
    still authorized would let the fraud complete anyway, which is the one
    outcome the whole call exists to prevent.
    """
    declined: list[str] = []
    for auth in pending_for_card(card_id):
        auth.status = DECLINED
        auth.resolution = reason
        declined.append(auth.reference_id)
        logger.info("Authorization %s DECLINED: %s", auth.reference_id, reason)
    return declined


def approve(auth_id: str, reason: str = "customer confirmed the purchase") -> bool:
    auth = _AUTHORIZATIONS.get(auth_id)
    if auth is None or auth.status != PENDING:
        return False
    auth.status = APPROVED
    auth.resolution = reason
    logger.info("Authorization %s APPROVED: %s", auth.reference_id, reason)
    return True


def for_customer(customer_id: str) -> list[dict]:
    rows = [
        a.as_row()
        for a in _AUTHORIZATIONS.values()
        if a.customer_id == customer_id
    ]
    return sorted(rows, key=lambda r: r["bookDate"], reverse=True)


def get(auth_id: str) -> PendingAuthorization | None:
    return _AUTHORIZATIONS.get(auth_id)


def reset() -> int:
    """Forget every authorization. For returning a demo to a quiet state."""
    count = len(_AUTHORIZATIONS)
    _AUTHORIZATIONS.clear()
    logger.info("Cleared %d authorization(s)", count)
    return count
