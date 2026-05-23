"""OTP generation, validation, and email sending."""

import secrets
import time

from app.auth.email_sender import send_email
from app.config import settings

# In-memory OTP store: {email: (otp_code, expires_at)}
_otp_store: dict[str, tuple[str, float]] = {}
# Anti-brute-force state per email
_verify_cooldown: dict[str, float] = {}   # email -> earliest next-attempt epoch
_verify_fails: dict[str, int] = {}        # email -> consecutive failures

OTP_TTL = 300  # 5 minutes
VERIFY_COOLDOWN_SEC = 10
MAX_VERIFY_FAILS = 5


def generate_otp(email: str) -> str:
    """Generate a 6-digit OTP and store it. Uses cryptographic randomness."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    _otp_store[email] = (code, time.time() + OTP_TTL)
    _verify_fails.pop(email, None)
    _verify_cooldown.pop(email, None)
    return code


def verify_cooldown_remaining(email: str) -> int:
    """Seconds left until the next /verify attempt is allowed for `email`, or 0."""
    next_at = _verify_cooldown.get(email, 0.0)
    remaining = next_at - time.time()
    return max(0, int(remaining + 0.999))  # ceil


def verify_otp(email: str, code: str) -> bool:
    """Verify an OTP code. Consumes on success.

    Anti-brute-force: each failed attempt sets a `VERIFY_COOLDOWN_SEC` window
    before the next attempt is even considered. After `MAX_VERIFY_FAILS`
    consecutive failures the OTP is invalidated and the user must request
    a new code.
    """
    if verify_cooldown_remaining(email) > 0:
        return False
    entry = _otp_store.get(email)
    if not entry:
        return False
    stored_code, expires_at = entry
    if time.time() > expires_at:
        _otp_store.pop(email, None)
        _verify_fails.pop(email, None)
        return False
    if not secrets.compare_digest(stored_code, code):
        fails = _verify_fails.get(email, 0) + 1
        _verify_fails[email] = fails
        _verify_cooldown[email] = time.time() + VERIFY_COOLDOWN_SEC
        if fails >= MAX_VERIFY_FAILS:
            _otp_store.pop(email, None)
        return False
    _otp_store.pop(email, None)
    _verify_fails.pop(email, None)
    _verify_cooldown.pop(email, None)
    return True


def is_email_allowed(email: str) -> bool:
    """Check if an email matches allowed_domains table or fallback to config."""
    try:
        from app.models.domain import is_email_allowed_by_domains
        from app.db import get_conn
        count = get_conn().execute(
            "SELECT COUNT(*) as c FROM allowed_domains"
        ).fetchone()
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


def send_otp_email(email: str, code: str) -> bool:
    """Send OTP via Resend with SCAO branding. Returns True on success."""
    from app.auth.email_templates import wrap_customer
    body = f"""\
<p style="font-size:15px;line-height:1.6;margin:0 0 20px;color:#1d1d1f;">
  Il codice di accesso per OS1 Docs è:
</p>
<div style="text-align:center;margin:24px 0;">
  <div style="display:inline-block;background:#f5f5f7;border-radius:8px;padding:18px 32px;font-family:'JetBrains Mono','Courier New',monospace;font-size:36px;letter-spacing:10px;font-weight:600;color:#E2231A;">{code}</div>
</div>
<p style="font-size:14px;line-height:1.6;color:#6e6e73;margin:0 0 8px;">Il codice è valido per 5 minuti.</p>
<p style="font-size:13px;line-height:1.6;color:#6e6e73;margin:0;">Se non ha richiesto questo codice può ignorare l'email.</p>"""
    html = wrap_customer("Codice di accesso", body, signature=True, footer=False)
    return send_email(email, f"Codice di accesso OS1 Docs: {code}", html)
