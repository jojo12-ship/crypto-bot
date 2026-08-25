"""One-way, non-polling Telegram delivery for validated scanner alerts."""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
from typing import Any, Callable
from urllib.request import Request, urlopen


HttpPost = Callable[[str, dict[str, Any]], dict[str, Any]]


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 -- Telegram HTTPS API only
            parsed = json.loads(response.read().decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {"ok": False, "description": "Telegram returned malformed JSON"}
    except Exception as exc:
        return {"ok": False, "description": f"Telegram delivery failed: {type(exc).__name__}"}


@dataclass
class TelegramNotifier:
    """Delivers only scanner-provided, already-deduplicated alerts.

    The notifier deliberately has no Telegram polling code. A failed send is not
    retried: the scanner reserves its durable alert key first, preferring a
    possible missed message over duplicate time-sensitive arbitrage notices.
    """

    token: str
    chat_id: str
    enabled: bool
    post_json: HttpPost = _post_json

    def status(self) -> dict[str, Any]:
        return {
            "channel": "Crypto Bot",
            "enabled": bool(self.enabled),
            "configured": bool(self.token and self.chat_id),
            "polling": False,
        }

    def deliver(self, opportunity: dict[str, Any]) -> bool:
        if not self.enabled or not self.token or not self.chat_id:
            return False
        payload = {
            "chat_id": self.chat_id,
            "text": self.format_alert(opportunity),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        result = self.post_json(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            payload,
        )
        return result.get("ok") is True

    @staticmethod
    def format_alert(opportunity: dict[str, Any]) -> str:
        first = opportunity.get("leg_a", {})
        second = opportunity.get("leg_b", {})
        title = escape(str(opportunity.get("market", "Reviewed prediction market")))
        reason = escape(str(opportunity.get("validation_reason", "Reviewed equivalence verified")))
        return "\n".join(
            [
                "🔎 <b>Prediction Market Arb — Alert Only</b>",
                f"<b>{title}</b>",
                "",
                f"• {escape(str(first.get('venue', 'Kalshi')))} {escape(str(first.get('outcome', '?')))}: "
                f"{first.get('ask_cents', '?')}¢",
                f"• {escape(str(second.get('venue', 'Polymarket US')))} {escape(str(second.get('outcome', '?')))}: "
                f"{second.get('ask_cents', '?')}¢",
                f"• Available depth: {opportunity.get('available_contracts', '?')} contracts",
                f"• Gross gap: {opportunity.get('gross_gap_cents', '?')}¢",
                f"• Estimated fees/slippage: "
                f"{int(opportunity.get('estimated_fees_cents', 0)) + int(opportunity.get('estimated_slippage_cents', 0))}¢",
                f"• <b>Net edge: {opportunity.get('net_edge_cents', '?')}¢</b>",
                "",
                f"✅ {reason}",
                "⚠️ Informational only — no order has been placed.",
            ]
        )