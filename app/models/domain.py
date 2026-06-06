"""Allowed domains CRUD for access control and limits."""

from datetime import datetime, timedelta, timezone

from app.db import get_conn
from app.models.settings import get_setting

# Usage tier presets — single source of truth for the commercial model.
# The contractual measure is monthly requests; the monthly token limit is a
# cost-protection ceiling. daily_limit=0 means unlimited daily quota.
TIER_TRIAL = "TRIAL"
TIER_FREE = "FREE"
TIER_BASE = "BASE"
TIER_PLUS = "PLUS"
TIER_POWER = "POWER"
FREEMIUM_TIERS = (TIER_TRIAL, TIER_FREE)

TIER_PRESETS = {
    TIER_TRIAL: {"monthly_request_limit": 100, "monthly_token_limit": 2_500_000, "daily_limit": 0},
    TIER_FREE:  {"monthly_request_limit": 5,   "monthly_token_limit": 100_000,   "daily_limit": 2},
    TIER_BASE:  {"monthly_request_limit": 100, "monthly_token_limit": 2_500_000, "daily_limit": 0},
    TIER_PLUS:  {"monthly_request_limit": 300, "monthly_token_limit": 7_000_000, "daily_limit": 0},
    TIER_POWER: {"monthly_request_limit": 800, "monthly_token_limit": 18_000_000,"daily_limit": 0},
}
DEFAULT_TIER = TIER_BASE

# Personal / free email providers — never accepted for self-signup.
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "ymail.com",
    "hotmail.com", "hotmail.it", "outlook.com", "outlook.it",
    "live.com", "live.it", "msn.com",
    "icloud.com", "me.com", "mac.com",
    "libero.it", "tiscali.it", "virgilio.it", "alice.it", "tin.it", "tim.it",
    "aol.com", "aol.it",
    "proton.me", "protonmail.com", "tutanota.com", "tuta.io",
    "gmx.com", "gmx.it", "gmx.net",
    "yandex.com", "yandex.ru", "mail.ru",
    "fastmail.com", "zoho.com", "inbox.com", "rocketmail.com",
}


def _normalize_tier(tier: str) -> str:
    """Clamp any input to a known tier. Single owner of tier validation."""
    return tier if tier in TIER_PRESETS else DEFAULT_TIER


def extract_email_domain(email: str) -> str | None:
    if "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower()


def parse_iso_utc(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_iso_date(iso: str | None) -> str:
    dt = parse_iso_utc(iso)
    return dt.strftime("%d/%m/%Y") if dt else "—"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_domains() -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM allowed_domains ORDER BY pattern"
    ).fetchall()
    return [dict(r) for r in rows]


def get_domain(domain_id: int) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM allowed_domains WHERE id = ?", (domain_id,)
    ).fetchone()
    return dict(row) if row else None


def get_domain_by_pattern(pattern: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM allowed_domains WHERE pattern = ?",
        (pattern.strip().lower(),),
    ).fetchone()
    return dict(row) if row else None


def add_domain(pattern: str, tier: str = DEFAULT_TIER) -> int:
    """Insert a domain applying the given tier preset."""
    tier = _normalize_tier(tier)
    preset = TIER_PRESETS[tier]
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO allowed_domains "
        "(pattern, tier, monthly_request_limit, monthly_token_limit, daily_limit) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            pattern.strip().lower(),
            tier,
            preset["monthly_request_limit"],
            preset["monthly_token_limit"],
            preset["daily_limit"],
        ),
    )
    conn.commit()
    return cur.lastrowid


def add_domain_trial(
    pattern: str,
    *,
    company_name: str,
    vat_number: str = "",
    contact_first_name: str,
    contact_last_name: str,
    contact_email: str,
    duration_days: int = 30,
) -> int:
    """Insert a TRIAL domain with expiry and registrant data."""
    preset = TIER_PRESETS[TIER_TRIAL]
    expires_at = _to_iso_z(_now_utc() + timedelta(days=duration_days))
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO allowed_domains "
        "(pattern, tier, monthly_request_limit, monthly_token_limit, daily_limit, "
        " expires_at, company_name, vat_number, contact_first_name, "
        " contact_last_name, contact_email) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pattern.strip().lower(),
            TIER_TRIAL,
            preset["monthly_request_limit"],
            preset["monthly_token_limit"],
            preset["daily_limit"],
            expires_at,
            company_name.strip(),
            vat_number.strip(),
            contact_first_name.strip(),
            contact_last_name.strip(),
            contact_email.strip().lower(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def apply_tier(domain_id: int, tier: str):
    """Atomically apply a tier preset to an existing domain."""
    tier = _normalize_tier(tier)
    preset = TIER_PRESETS[tier]
    conn = get_conn()
    conn.execute(
        "UPDATE allowed_domains SET tier = ?, monthly_request_limit = ?, "
        "monthly_token_limit = ?, daily_limit = ? WHERE id = ?",
        (
            tier,
            preset["monthly_request_limit"],
            preset["monthly_token_limit"],
            preset["daily_limit"],
            domain_id,
        ),
    )
    conn.commit()


def set_domain_enabled(domain_id: int, enabled: bool):
    conn = get_conn()
    conn.execute(
        "UPDATE allowed_domains SET enabled = ? WHERE id = ?",
        (int(enabled), domain_id),
    )
    conn.commit()


def update_domain_metadata(
    domain_id: int,
    *,
    expires_at: str | None = None,
    company_name: str | None = None,
    vat_number: str | None = None,
    contact_first_name: str | None = None,
    contact_last_name: str | None = None,
    contact_email: str | None = None,
):
    """Update freemium/registrant fields. Pass None to leave a field untouched."""
    fields = {
        "expires_at": expires_at,
        "company_name": company_name,
        "vat_number": vat_number,
        "contact_first_name": contact_first_name,
        "contact_last_name": contact_last_name,
        "contact_email": contact_email,
    }
    sets = [(k, v) for k, v in fields.items() if v is not None]
    if not sets:
        return
    clause = ", ".join(f"{k} = ?" for k, _ in sets)
    values = [v for _, v in sets] + [domain_id]
    conn = get_conn()
    conn.execute(f"UPDATE allowed_domains SET {clause} WHERE id = ?", values)
    conn.commit()


def mark_flag(domain_id: int, flag: str):
    """Set a boolean flag (welcome_sent / expiry_reminder_sent / downgrade_notice_sent) to 1."""
    if flag not in {"welcome_sent", "expiry_reminder_sent", "downgrade_notice_sent"}:
        raise ValueError(f"Unknown flag: {flag}")
    conn = get_conn()
    conn.execute(
        f"UPDATE allowed_domains SET {flag} = 1 WHERE id = ?", (domain_id,)
    )
    conn.commit()


def delete_domain(domain_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM allowed_domains WHERE id = ?", (domain_id,))
    conn.commit()
    return cur.rowcount > 0


def _match_pattern(email_lower: str, pattern: str) -> bool:
    pattern = pattern.lower()
    if pattern.startswith("*@"):
        return email_lower.endswith(f"@{pattern[2:]}")
    return pattern == email_lower


def is_email_allowed_by_domains(email: str) -> bool:
    """Check if email matches any enabled domain pattern."""
    domains = get_conn().execute(
        "SELECT pattern FROM allowed_domains WHERE enabled = 1"
    ).fetchall()
    if not domains:
        return False
    email_lower = email.lower()
    return any(_match_pattern(email_lower, d["pattern"]) for d in domains)


def get_domain_for_email(email: str) -> dict | None:
    """Get the matching domain config for an email (for limits).

    Lazy trial check: if the matched domain is a TRIAL past expires_at,
    downgrade it to FREE before returning (one-shot, idempotent).
    """
    domains = get_conn().execute(
        "SELECT * FROM allowed_domains WHERE enabled = 1"
    ).fetchall()
    email_lower = email.lower()
    for d in domains:
        if _match_pattern(email_lower, d["pattern"]):
            return check_and_downgrade_if_expired(dict(d))
    return None


def is_mcp_enabled_for_email(email: str) -> bool:
    """True se i connettori MCP sono abilitati per il dominio dell'utente.

    Nessun dominio specifico → True (l'accesso è già filtrato da is_email_allowed
    al login). Un dominio con mcp_enabled=0 blocca MCP per tutti i suoi utenti.
    """
    d = get_domain_for_email(email)
    if d is None:
        return True
    return bool(d.get("mcp_enabled", 1))


def set_domain_mcp(domain_id: int, enabled: bool) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE allowed_domains SET mcp_enabled = ? WHERE id = ?",
        (1 if enabled else 0, domain_id),
    )
    conn.commit()


def trial_days_left(expires_at: str | None) -> int | None:
    dt = parse_iso_utc(expires_at)
    if not dt:
        return None
    return max(0, (dt - _now_utc()).days)


def get_trial_banner_info(domain: dict | None) -> dict | None:
    """Compute banner context for chat.html. Returns None if no banner needed."""
    if not domain or domain.get("tier") not in FREEMIUM_TIERS:
        return None
    tier = domain["tier"]
    expires_at = domain.get("expires_at")
    return {
        "tier": tier,
        "days_left": trial_days_left(expires_at) if tier == TIER_TRIAL else None,
        "expires_at": format_iso_date(expires_at) if tier == TIER_TRIAL else None,
    }


def check_and_downgrade_if_expired(domain: dict) -> dict:
    """If domain is a TRIAL past expires_at, downgrade to FREE and notify once.

    Returns the (possibly updated) domain dict. Safe to call repeatedly.
    """
    if domain.get("tier") != TIER_TRIAL:
        return domain
    expires_dt = parse_iso_utc(domain.get("expires_at"))
    if not expires_dt or _now_utc() <= expires_dt:
        return domain

    apply_tier(domain["id"], TIER_FREE)
    free_preset = TIER_PRESETS[TIER_FREE]
    downgraded = {
        **domain,
        "tier": TIER_FREE,
        "monthly_request_limit": free_preset["monthly_request_limit"],
        "monthly_token_limit": free_preset["monthly_token_limit"],
        "daily_limit": free_preset["daily_limit"],
    }

    if not domain.get("downgrade_notice_sent"):
        any_sent = _send_downgrade_notices(downgraded)
        if any_sent:
            mark_flag(domain["id"], "downgrade_notice_sent")
            downgraded["downgrade_notice_sent"] = 1
    return downgraded


def _send_downgrade_notices(domain: dict) -> bool:
    """Best-effort send of customer + admin downgrade notices. Returns True if any succeeded."""
    try:
        from app.auth.email_sender import get_admin_notification_email, send_email
        from app.auth.email_templates import admin_trial_expired, trial_downgraded
    except Exception:
        return False

    any_sent = False
    if domain.get("contact_email"):
        try:
            subject, html = trial_downgraded(
                first_name=domain.get("contact_first_name") or "",
                domain_pattern=domain["pattern"],
            )
            if send_email(domain["contact_email"], subject, html):
                any_sent = True
        except Exception as e:
            print(f"[trial] customer downgrade email failed: {e}", flush=True)
    admin_to = get_admin_notification_email()
    if admin_to:
        try:
            subject, html = admin_trial_expired(domain)
            if send_email(admin_to, subject, html):
                any_sent = True
        except Exception as e:
            print(f"[trial] admin downgrade email failed: {e}", flush=True)
    return any_sent


def _get_extra_blocked_domains() -> set[str]:
    raw = get_setting("extra_blocked_email_domains", "")
    if not raw:
        return set()
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def is_personal_email_domain(email: str) -> bool:
    """True if email's domain is in the hardcoded personal set or admin override."""
    domain = extract_email_domain(email)
    if not domain:
        return False
    return domain in PERSONAL_EMAIL_DOMAINS or domain in _get_extra_blocked_domains()


def get_daily_question_count(user_id: int) -> int:
    """Count user questions today."""
    row = get_conn().execute(
        "SELECT COUNT(*) as cnt FROM messages m "
        "JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.user_id = ? AND m.role = 'user' "
        "AND date(m.created_at) = date('now')",
        (user_id,),
    ).fetchone()
    return row["cnt"] if row else 0
