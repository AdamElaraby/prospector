"""Resend email sending.

Before the first send, verify your sending domain in Resend so SPF, DKIM and
DMARC all pass. Cold mail from an unauthenticated domain goes straight to spam
and burns the domain permanently.

Use a dedicated domain for outreach, not the one your real mail runs on.
"""

from __future__ import annotations

import httpx

from ..config import Config, env

ENDPOINT = "https://api.resend.com/emails"


class SendError(RuntimeError):
    pass


class ResendSender:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.api_key = env("RESEND_API_KEY", required=True)
        sender = cfg.get("sender", {}) or {}
        self.from_email = cfg.get("email_channel.from_email")
        # Your own name is the right From when there is no company behind it.
        self.from_name = sender.get("business_name") or sender.get("name") or ""
        self.reply_to = cfg.get("email_channel.reply_to") or self.from_email
        if not self.from_email or "@" not in str(self.from_email):
            raise SendError(
                "email_channel.from_email in config.local.yaml is not a valid address"
            )

    @property
    def from_header(self) -> str:
        return f"{self.from_name} <{self.from_email}>" if self.from_name else self.from_email

    def send(self, to: str, subject: str, text: str, html: str) -> str:
        """Send one email. Returns the Resend message id."""
        payload = {
            "from": self.from_header,
            "to": [to],
            "subject": subject,
            "text": text,
            "html": html,
            "reply_to": self.reply_to,
            # One-click unsubscribe. Required by Gmail/Yahoo bulk sender rules
            # and it keeps complaints from turning into spam reports.
            "headers": {
                "List-Unsubscribe": f"<mailto:{self.reply_to}?subject=unsubscribe>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                ENDPOINT,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        if resp.status_code >= 300:
            raise SendError(f"Resend {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("id", "")
