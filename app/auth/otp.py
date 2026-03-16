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
    """Check if an email matches allowed_domains table or fallback to config."""
    try:
        from app.models.domain import is_email_allowed_by_domains
        # If there are domains in the DB, use those
        from app.db import get_conn
        count = get_conn().execute("SELECT COUNT(*) as c FROM allowed_domains").fetchone()
        if count and count["c"] > 0:
            return is_email_allowed_by_domains(email)
    except Exception:
        pass

    # Fallback to config (for first boot before admin configures domains)
    patterns = [p.strip() for p in settings.allowed_emails.split(",") if p.strip()]
    email_lower = email.lower()
    for pattern in patterns:
        pattern = pattern.lower()
        if pattern.startswith("*@"):
            if email_lower.endswith(f"@{pattern[2:]}"):
                return True
        elif pattern == email_lower:
            return True
    return False


def _get_sender() -> str:
    """Get OTP email sender from app_settings or default."""
    name = "OS1 Docs"
    email_addr = "noreply@ai.scao.it"
    try:
        from app.db import get_conn
        conn = get_conn()
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'otp_sender_name'").fetchone()
        if row and row["value"]:
            name = row["value"]
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'otp_sender_email'").fetchone()
        if row and row["value"]:
            email_addr = row["value"]
    except Exception:
        pass
    return f"{name} <{email_addr}>"


def send_otp_email(email: str, code: str) -> bool:
    """Send OTP via Resend. Returns True on success."""
    if not settings.resend_api_key:
        print(f"[DEV MODE] OTP for {email}: {code}", flush=True)
        return True

    resend.api_key = settings.resend_api_key
    try:
        resend.Emails.send({
            "from": _get_sender(),
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
