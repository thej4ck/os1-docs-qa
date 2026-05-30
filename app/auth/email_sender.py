"""Generic Resend email wrapper and shared sender/admin helpers."""

import resend

from app.config import settings
from app.models.settings import get_int_setting, get_setting
from app.version import PRODUCT_NAME

DEFAULT_TRIAL_DAYS = 30
MAX_TRIAL_DAYS = 365


def _get_sender() -> str:
    """Get email sender 'Name <email>' from app_settings or default."""
    name = get_setting("otp_sender_name", PRODUCT_NAME)
    email_addr = get_setting("otp_sender_email", "noreply@ai.scao.it")
    return f"{name} <{email_addr}>"


def get_admin_notification_email() -> str | None:
    """Internal SCAO recipient for signup / upgrade / expiry notifications."""
    value = get_setting("admin_notification_email", "").strip()
    return value or None


def get_trial_duration_days() -> int:
    return get_int_setting(
        "trial_duration_days", DEFAULT_TRIAL_DAYS, lo=1, hi=MAX_TRIAL_DAYS
    )


def send_email(to: str | list[str], subject: str, html: str) -> bool:
    """Generic Resend wrapper. Falls back to stdout in dev (no API key)."""
    if not settings.resend_api_key:
        recipients = to if isinstance(to, list) else [to]
        print(
            f"[DEV MODE] Email to {recipients} | subject={subject!r}\n{html[:400]}...",
            flush=True,
        )
        return True

    resend.api_key = settings.resend_api_key
    try:
        resend.Emails.send({
            "from": _get_sender(),
            "to": to if isinstance(to, list) else [to],
            "subject": subject,
            "html": html,
        })
        return True
    except Exception as e:
        print(f"Failed to send email: {e}", flush=True)
        return False
