"""Allowed domains CRUD for access control and limits."""

from datetime import datetime, timedelta, timezone

from app.db import get_conn
from app.models.settings import get_setting

# Usage tier presets — single source of truth for the commercial model.
# The contractual measure is monthly requests; the monthly token limit is a
# cost-protection ceiling. daily_limit=0 means unlimited daily quota (in QUESTIONS).
# daily_chat_limit=0 means unlimited NEW conversations/day (FREE caps it to 1).
TIER_TRIAL = "TRIAL"
TIER_FREE = "FREE"
TIER_BASE = "BASE"
TIER_PLUS = "PLUS"
TIER_POWER = "POWER"
# Fasce a pagamento legate ai PDL OS1 (full-feature; cambiano solo i limiti d'uso).
TIER_STARTER = "STARTER"
TIER_TEAM = "TEAM"
TIER_BUSINESS = "BUSINESS"
TIER_PRO = "PRO"
TIER_PREMIUM = "PREMIUM"
TIER_ENTERPRISE = "ENTERPRISE"
FREEMIUM_TIERS = (TIER_TRIAL, TIER_FREE)

# Stato commerciale (colonna billing_status), separato dal tier applicato.
BILLING_TRIAL = "trial"
BILLING_FREE = "free"
BILLING_PAID = "paid"
BILLING_PAST_DUE = "past_due"

TIER_PRESETS = {
    TIER_TRIAL: {"monthly_request_limit": 100, "monthly_token_limit": 2_500_000, "daily_limit": 0, "daily_chat_limit": 0},
    # FREE post-trial: 1 nuova chat al giorno (fino al max messaggi/chat), gancio retention.
    TIER_FREE:  {"monthly_request_limit": 30,  "monthly_token_limit": 300_000,   "daily_limit": 0, "daily_chat_limit": 1},
    TIER_BASE:  {"monthly_request_limit": 100, "monthly_token_limit": 2_500_000, "daily_limit": 0, "daily_chat_limit": 0},
    TIER_PLUS:  {"monthly_request_limit": 300, "monthly_token_limit": 7_000_000, "daily_limit": 0, "daily_chat_limit": 0},
    TIER_POWER: {"monthly_request_limit": 800, "monthly_token_limit": 18_000_000,"daily_limit": 0, "daily_chat_limit": 0},
    # Fasce PDL — limiti generosi (fair-use), scalano coi PDL. COGS trascurabile.
    TIER_STARTER:    {"monthly_request_limit": 300,  "monthly_token_limit": 7_000_000,  "daily_limit": 0, "daily_chat_limit": 0},
    TIER_TEAM:       {"monthly_request_limit": 600,  "monthly_token_limit": 14_000_000, "daily_limit": 0, "daily_chat_limit": 0},
    TIER_BUSINESS:   {"monthly_request_limit": 1_000,"monthly_token_limit": 24_000_000, "daily_limit": 0, "daily_chat_limit": 0},
    TIER_PRO:        {"monthly_request_limit": 1_500,"monthly_token_limit": 36_000_000, "daily_limit": 0, "daily_chat_limit": 0},
    TIER_PREMIUM:    {"monthly_request_limit": 2_000,"monthly_token_limit": 48_000_000, "daily_limit": 0, "daily_chat_limit": 0},
    TIER_ENTERPRISE: {"monthly_request_limit": 3_000,"monthly_token_limit": 72_000_000, "daily_limit": 0, "daily_chat_limit": 0},
}
DEFAULT_TIER = TIER_BASE

# Entitlement per tier: quali funzionalità sblocca. FREE = minimale (1 esperto,
# niente deep/MCP/share); TRIAL e tutte le fasce a pagamento = tutto sbloccato.
_FEAT_FULL = {"experts": True, "deep": True, "mcp": True, "share": True}
_FEAT_MINIMAL = {"experts": False, "deep": False, "mcp": False, "share": False}
# Solo FREE è minimale; ogni altro tier (TRIAL + fasce a pagamento + legacy)
# eredita _FEAT_FULL dal default di domain_features() — niente da aggiornare
# quando si aggiunge una fascia.
TIER_FEATURES = {TIER_FREE: _FEAT_MINIMAL}

# Listino fasce PDL (land-grab ~€5-7/PDL effettivo, scaglioni). Prezzi €/mese e
# €/anno ex IVA, fatturazione annuale. Enterprise = a preventivo (price=None).
PRICING_BANDS = [
    {"band": TIER_STARTER,    "label": "Starter",    "pdl_min": 1,  "pdl_max": 3,    "price_month": 19,  "price_year": 228},
    {"band": TIER_TEAM,       "label": "Team",       "pdl_min": 4,  "pdl_max": 6,    "price_month": 35,  "price_year": 420},
    {"band": TIER_BUSINESS,   "label": "Business",   "pdl_min": 7,  "pdl_max": 10,   "price_month": 49,  "price_year": 588},
    {"band": TIER_PRO,        "label": "Pro",        "pdl_min": 11, "pdl_max": 15,   "price_month": 75,  "price_year": 900},
    {"band": TIER_PREMIUM,    "label": "Premium",    "pdl_min": 16, "pdl_max": 20,   "price_month": 89,  "price_year": 1068},
    {"band": TIER_ENTERPRISE, "label": "Enterprise", "pdl_min": 21, "pdl_max": None, "price_month": None,"price_year": None},
]


def band_from_pdl(pdl: int | None) -> str | None:
    """Fascia (tier) dai PDL OS1. None/<=0 → None (PDL non impostato)."""
    if not pdl or pdl < 1:
        return None
    for b in PRICING_BANDS:
        if pdl >= b["pdl_min"] and (b["pdl_max"] is None or pdl <= b["pdl_max"]):
            return b["band"]
    return TIER_ENTERPRISE


def band_info(band: str | None) -> dict | None:
    """Riga di listino per una fascia (label/range/prezzo), o None."""
    if not band:
        return None
    return next((b for b in PRICING_BANDS if b["band"] == band), None)


def domain_features(domain: dict | None) -> dict:
    """Entitlement (experts/deep/mcp/share) per il dominio. Default full (legacy-safe)."""
    if not domain:
        return dict(_FEAT_FULL)
    return dict(TIER_FEATURES.get(domain.get("tier"), _FEAT_FULL))


def tier_daily_chat_limit(tier: str | None) -> int:
    """Max nuove conversazioni/giorno per il tier (0 = illimitato)."""
    return TIER_PRESETS.get(tier or "", {}).get("daily_chat_limit", 0)

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
    duration_days: int = 7,
) -> int:
    """Insert a TRIAL domain (full-unlock) with expiry and registrant data.

    Il trial dura 7 giorni e sblocca tutte le funzionalità; una drip email
    giornaliera (scheduler) invita a provarle. billing_status='trial'.
    """
    preset = TIER_PRESETS[TIER_TRIAL]
    expires_at = _to_iso_z(_now_utc() + timedelta(days=duration_days))
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO allowed_domains "
        "(pattern, tier, monthly_request_limit, monthly_token_limit, daily_limit, "
        " expires_at, company_name, vat_number, contact_first_name, "
        " contact_last_name, contact_email, billing_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            BILLING_TRIAL,
        ),
    )
    conn.commit()
    return cur.lastrowid


def _tier_limits(tier: str) -> tuple[int, int, int]:
    """(monthly_request, monthly_token, daily) per il preset del tier normalizzato."""
    p = TIER_PRESETS[_normalize_tier(tier)]
    return p["monthly_request_limit"], p["monthly_token_limit"], p["daily_limit"]


def apply_tier(domain_id: int, tier: str):
    """Atomically apply a tier preset to an existing domain."""
    tier = _normalize_tier(tier)
    req, tok, daily = _tier_limits(tier)
    conn = get_conn()
    conn.execute(
        "UPDATE allowed_domains SET tier = ?, monthly_request_limit = ?, "
        "monthly_token_limit = ?, daily_limit = ? WHERE id = ?",
        (tier, req, tok, daily, domain_id),
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
    # Richiede sia il toggle admin per-dominio sia l'entitlement di tier
    # (FREE non ha MCP anche con mcp_enabled=1).
    return bool(d.get("mcp_enabled", 1)) and domain_features(d)["mcp"]


def set_domain_pdl(domain_id: int, pdl: int | None) -> None:
    """Registra il numero di PDL OS1 e la fascia derivata (senza attivare il pagamento)."""
    band = band_from_pdl(pdl)
    conn = get_conn()
    conn.execute(
        "UPDATE allowed_domains SET os1_pdl_count = ?, pricing_band = ? WHERE id = ?",
        (pdl, band, domain_id),
    )
    conn.commit()


def activate_paid(domain_id: int, pdl: int) -> str | None:
    """Attiva un abbonamento a pagamento in un solo UPDATE: tier+limiti della
    fascia (dai PDL), billing_status='paid', azzera la scadenza trial.
    Ritorna la fascia applicata, o None se i PDL non mappano una fascia."""
    band = band_from_pdl(pdl)
    if not band:
        return None
    req, tok, daily = _tier_limits(band)
    conn = get_conn()
    conn.execute(
        "UPDATE allowed_domains SET tier = ?, monthly_request_limit = ?, "
        "monthly_token_limit = ?, daily_limit = ?, os1_pdl_count = ?, "
        "pricing_band = ?, billing_status = ?, expires_at = NULL WHERE id = ?",
        (band, req, tok, daily, pdl, band, BILLING_PAID, domain_id),
    )
    conn.commit()
    return band


def get_daily_conversation_count(user_id: int) -> int:
    """Conta le conversazioni create oggi dall'utente (per il limite FREE 1 chat/giorno)."""
    row = get_conn().execute(
        "SELECT COUNT(*) AS cnt FROM conversations "
        "WHERE user_id = ? AND date(created_at) = date('now')",
        (user_id,),
    ).fetchone()
    return row["cnt"] if row else 0


def list_active_trials() -> list[dict]:
    """Domini abilitati in stato trial (per lo scheduler drip + downgrade)."""
    rows = get_conn().execute(
        "SELECT * FROM allowed_domains WHERE enabled = 1 AND billing_status = ?",
        (BILLING_TRIAL,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_trial_drip_day(domain_id: int, day: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE allowed_domains SET trial_drip_day = ? WHERE id = ?",
        (int(day), domain_id),
    )
    conn.commit()


_BILLING_STATES = {BILLING_TRIAL, BILLING_FREE, BILLING_PAID, BILLING_PAST_DUE}


def set_billing_status(domain_id: int, status: str) -> None:
    """Imposta lo stato commerciale (validato). No-op se sconosciuto."""
    if status not in _BILLING_STATES:
        return
    conn = get_conn()
    conn.execute(
        "UPDATE allowed_domains SET billing_status = ? WHERE id = ?",
        (status, domain_id),
    )
    conn.commit()


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
    set_billing_status(domain["id"], BILLING_FREE)
    free_preset = TIER_PRESETS[TIER_FREE]
    downgraded = {
        **domain,
        "tier": TIER_FREE,
        "billing_status": BILLING_FREE,
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
