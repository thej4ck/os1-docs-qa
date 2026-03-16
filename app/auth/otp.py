"""OTP generation, validation, and email sending."""

import random
import time
from typing import Optional

import resend

from app.config import settings

# In-memory OTP store: {email: (otp_code, expires_at)}
_otp_store: dict[str, tuple[str, float]] = {}

OTP_TTL = 300  # 5 minutes


def generate_otp(email: str) -> str:
    """Generate a 6-digit OTP and store it."""
    code = f"{random.randint(0, 999999):06d}"
    _otp_store[email] = (code, time.time() + OTP_TTL)
    return code


def verify_otp(email: str, code: str) -> bool:
    """Verify an OTP code. Consumes it on success."""
    entry = _otp_store.get(email)
    if not entry:
        return False
    stored_code, expires_at = entry
    if time.time() > expires_at:
        _otp_store.pop(email, None)
        return False
    if stored_code != code:
        return False
    _otp_store.pop(email, None)
    return True


def is_email_allowed(email: str) -> bool:
    """Check if an email matches the allowlist patterns."""
    patterns = [p.strip() for p in settings.allowed_emails.split(",") if p.strip()]
    email_lower = email.lower()
    for pattern in patterns:
        pattern = pattern.lower()
        if pattern.startswith("*@"):
            # Domain wildcard
            domain = pattern[2:]
            if email_lower.endswith(f"@{domain}"):
                return True
        elif pattern == email_lower:
            return True
    return False


def send_otp_email(email: str, code: str) -> bool:
    """Send OTP via Resend. Returns True on success."""
    if not settings.resend_api_key:
        print(f"[DEV MODE] OTP for {email}: {code}")
        return True

    resend.api_key = settings.resend_api_key
    try:
        resend.Emails.send({
            "from": "OS1 Docs <noreply@os1docs.scao.it>",
            "to": [email],
            "subject": f"Codice di accesso OS1 Docs: {code}",
            "html": (
                f"<p>Il tuo codice di accesso a OS1 Docs è:</p>"
                f"<h1 style='letter-spacing: 8px; font-size: 36px;'>{code}</h1>"
                f"<p>Il codice scade tra 5 minuti.</p>"
                f"<p>Se non hai richiesto questo codice, ignora questa email.</p>"
            ),
        })
        return True
    except Exception as e:
        print(f"Failed to send OTP email: {e}")
        return False
