"""HTML email templates for freemium flow. Each returns (subject, html).

Customer-facing emails carry the SCAO Informatica signature; internal
notifications are stripped to essentials.
"""

from html import escape

from app.models.domain import (
    TIER_PRESETS,
    TIER_FREE,
    format_iso_date as _fmt_date,
)


_BRAND = "#E2231A"
_BG = "#f5f5f7"
_TEXT = "#1d1d1f"
_MUTED = "#6e6e73"
_BORDER = "#e5e5ea"
_CARD = "#ffffff"

_COMPANY = "SCAO Informatica S.r.l."
_COMPANY_TAGLINE = "Ingegneri della digitalizzazione della produzione industriale — dal 1977"
_COMPANY_WEB = "https://www.scao.it"
_PRODUCT = "OS1 Docs Q&A"
_PRODUCT_HTML = escape(_PRODUCT)  # use this inside HTML markup


def _signature_html() -> str:
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-top:28px;border-top:1px solid {_BORDER};padding-top:20px;width:100%;">
  <tr>
    <td style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
      <div style="font-size:13px;line-height:1.5;color:{_TEXT};">
        <strong style="color:{_BRAND};">{_COMPANY}</strong><br>
        <span style="color:{_MUTED};">{_COMPANY_TAGLINE}</span><br>
        <a href="{_COMPANY_WEB}" style="color:{_BRAND};text-decoration:none;">www.scao.it</a>
      </div>
    </td>
  </tr>
</table>"""


def _customer_footer() -> str:
    return f"""\
<p style="font-size:11px;color:{_MUTED};line-height:1.5;margin-top:24px;text-align:center;">
  Questa email è stata inviata da {_COMPANY} in relazione al servizio {_PRODUCT_HTML}.<br>
  I tuoi dati sono trattati ai sensi del Regolamento UE 2016/679 (GDPR).<br>
  Per assistenza scrivi a <a href="mailto:info@scao.it" style="color:{_BRAND};text-decoration:none;">info@scao.it</a>.
</p>"""


def wrap_customer(
    title: str, body_html: str, *, signature: bool = True, footer: bool = True
) -> str:
    """Customer-facing email shell. Used by templates and OTP delivery alike."""
    sig = _signature_html() if signature else ""
    foot = _customer_footer() if footer else ""
    return f"""\
<!DOCTYPE html>
<html lang="it">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:{_BG};">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:{_BG};padding:32px 16px;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="max-width:580px;width:100%;background:{_CARD};border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.04);overflow:hidden;">
    <tr>
      <td style="background:{_BRAND};padding:20px 32px;">
        <div style="color:#fff;font-size:13px;letter-spacing:0.06em;text-transform:uppercase;font-weight:600;opacity:0.85;">SCAO Informatica · {_PRODUCT_HTML}</div>
      </td>
    </tr>
    <tr>
      <td style="padding:32px;color:{_TEXT};">
        <h1 style="margin:0 0 24px;font-size:22px;font-weight:600;color:{_TEXT};">{title}</h1>
        {body_html}
        {sig}
      </td>
    </tr>
  </table>
  {foot}
</div>
</body>
</html>"""


def _wrap_internal(title: str, body_html: str) -> str:
    return f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:{_BG};padding:24px;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="max-width:580px;width:100%;background:{_CARD};border-radius:8px;">
    <tr><td style="padding:24px 28px;color:{_TEXT};">
      <div style="border-left:3px solid {_BRAND};padding-left:12px;margin-bottom:20px;">
        <h2 style="margin:0;font-size:17px;font-weight:600;">{title}</h2>
        <div style="font-size:12px;color:{_MUTED};margin-top:2px;">Notifica automatica — {_PRODUCT_HTML}</div>
      </div>
      {body_html}
    </td></tr>
  </table>
</div>"""


def _free_limits_text() -> str:
    free = TIER_PRESETS[TIER_FREE]
    return (
        f"{free['monthly_request_limit']} domande al mese, massimo "
        f"{free['daily_limit']} al giorno"
    )


def _kv_table(rows: list[tuple[str, str]]) -> str:
    """Build a key/value HTML table. Values must already be HTML-safe.

    Callers are responsible for escaping any user-controlled text before
    embedding markup (use `escape()` on the raw field, then wrap in `<strong>`).
    """
    cells = "".join(
        f'<tr>'
        f'<td style="padding:8px 10px 8px 0;color:{_MUTED};font-size:13px;width:140px;vertical-align:top;">{escape(k)}</td>'
        f'<td style="padding:8px 0;font-size:14px;color:{_TEXT};">{v or "—"}</td>'
        f'</tr>'
        for k, v in rows
    )
    return f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse;">{cells}</table>'


# ── Customer-facing emails ────────────────────────────────────────────────

def welcome_trial(
    *,
    first_name: str,
    domain_pattern: str,
    expires_at: str,
    monthly_requests: int,
    monthly_tokens: int,
) -> tuple[str, str]:
    subject = f"Benvenuto in {_PRODUCT} — la tua prova gratuita è attiva"
    domain_naked = escape(domain_pattern.lstrip("*@"))
    greeting = f"Gentile {escape(first_name)}" if first_name else "Buongiorno"
    body = f"""\
<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">{greeting},</p>

<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">
  grazie per aver scelto di provare <strong>{_PRODUCT_HTML}</strong>, l'assistente
  basato su intelligenza artificiale per la documentazione del gestionale OS1.
</p>

<p style="font-size:15px;line-height:1.6;margin:0 0 24px;">
  La sua <strong>prova gratuita</strong> è attiva fino al
  <strong style="color:{_BRAND};">{_fmt_date(expires_at)}</strong> ed è abilitata
  per <strong>tutti gli indirizzi del dominio <code style="background:{_BG};padding:2px 6px;border-radius:4px;">@{domain_naked}</code></strong>:
  i suoi colleghi possono accedere usando la stessa procedura di login con OTP.
</p>

<h3 style="font-size:16px;margin:28px 0 12px;color:{_TEXT};">Come ottenere il massimo dal servizio</h3>
<ul style="padding-left:20px;line-height:1.7;font-size:14px;margin:0 0 20px;color:{_TEXT};">
  <li><strong>Domande chiare e specifiche</strong>: indica modulo, tabella o funzione
      (es. <em>"come configuro la causale contabile per gli acconti?"</em>).
      Una domanda alla volta produce risposte più puntuali.</li>
  <li><strong>Richiesta di approfondimento</strong>: se la risposta è generica,
      usa il pulsante <strong>"Approfondisci"</strong> o digita "fammi un esempio pratico":
      il sistema rilegge il contesto con un modello più capace.</li>
  <li><strong>Documenti collegati</strong>: ogni risposta cita i documenti sorgente,
      consultabili nel pannello a destra con la documentazione originale OS1.</li>
</ul>

<h3 style="font-size:16px;margin:28px 0 12px;color:{_TEXT};">Privacy &amp; GDPR</h3>
<p style="font-size:14px;line-height:1.6;margin:0 0 20px;color:{_TEXT};">
  Tutte le conversazioni restano private e accessibili solo agli utenti del suo dominio.
  Nessun dato viene condiviso con terze parti per finalità di training.
  Il servizio è gestito da SCAO Informatica nel pieno rispetto del GDPR (Reg. UE 2016/679).
</p>

<h3 style="font-size:16px;margin:28px 0 12px;color:{_TEXT};">Limiti della prova gratuita</h3>
<div style="background:{_BG};border-radius:8px;padding:16px 20px;margin:0 0 20px;">
  {_kv_table([
    ("Durata", f"fino al <strong>{_fmt_date(expires_at)}</strong>"),
    ("Domande / mese", f"fino a <strong>{monthly_requests}</strong>"),
  ])}
</div>

<p style="font-size:14px;line-height:1.6;margin:0 0 16px;color:{_TEXT};">
  Al termine della prova il dominio passerà automaticamente al piano <strong>FREE</strong>
  ({_free_limits_text()}). In qualsiasi momento — anche durante la prova —
  può richiedere l'attivazione di un abbonamento commerciale dal pulsante
  <strong>"Richiedi attivazione abbonamento"</strong> sempre visibile nell'interfaccia.
</p>

<p style="font-size:14px;line-height:1.6;margin:24px 0 0;color:{_TEXT};">
  Siamo a disposizione per qualsiasi necessità.
</p>
<p style="font-size:14px;line-height:1.6;margin:8px 0 0;color:{_TEXT};">
  Cordiali saluti,
</p>"""
    return subject, wrap_customer("La sua prova gratuita è attiva", body)


def trial_expiry_reminder(
    *, first_name: str, domain_pattern: str, days_left: int
) -> tuple[str, str]:
    subject = f"La sua prova {_PRODUCT} scade tra {int(days_left)} giorni"
    greeting = f"Gentile {escape(first_name)}" if first_name else "Buongiorno"
    safe_pattern = escape(domain_pattern)
    body = f"""\
<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">{greeting},</p>

<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">
  la prova gratuita di <strong>{_PRODUCT_HTML}</strong> attiva sul dominio
  <code style="background:{_BG};padding:2px 6px;border-radius:4px;">{safe_pattern}</code>
  scade tra <strong style="color:{_BRAND};">{days_left} giorni</strong>.
</p>

<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">
  Per non interrompere il servizio può richiedere l'attivazione di un abbonamento
  commerciale direttamente dall'app, cliccando su
  <strong>"Richiedi attivazione abbonamento"</strong>: la nostra area commerciale
  la ricontatterà con una proposta su misura.
</p>

<p style="font-size:14px;line-height:1.6;margin:0 0 16px;color:{_MUTED};">
  Alla scadenza, il dominio passerà automaticamente al piano FREE
  ({_free_limits_text()}).
</p>

<p style="font-size:14px;line-height:1.6;margin:24px 0 0;color:{_TEXT};">
  Restiamo a disposizione.<br>
  Cordiali saluti,
</p>"""
    return subject, wrap_customer("Prova in scadenza", body)


def trial_downgraded(*, first_name: str, domain_pattern: str) -> tuple[str, str]:
    subject = f"La sua prova {_PRODUCT} è terminata"
    greeting = f"Gentile {escape(first_name)}" if first_name else "Buongiorno"
    safe_pattern = escape(domain_pattern)
    body = f"""\
<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">{greeting},</p>

<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">
  la prova gratuita di <strong>{_PRODUCT_HTML}</strong> per il dominio
  <code style="background:{_BG};padding:2px 6px;border-radius:4px;">{safe_pattern}</code>
  è terminata.
</p>

<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">
  Il servizio rimane comunque accessibile con il piano <strong>FREE</strong>:
</p>
<div style="background:{_BG};border-radius:8px;padding:14px 20px;margin:0 0 20px;font-size:14px;">
  • {TIER_PRESETS[TIER_FREE]['monthly_request_limit']} domande al mese<br>
  • massimo {TIER_PRESETS[TIER_FREE]['daily_limit']} domande al giorno
</div>

<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">
  Per ripristinare l'accesso pieno e usare {_PRODUCT_HTML} senza limiti, può richiedere
  l'attivazione di un abbonamento dal pulsante
  <strong>"Richiedi attivazione abbonamento"</strong> presente nell'interfaccia,
  oppure rispondere direttamente a questa email.
</p>

<p style="font-size:14px;line-height:1.6;margin:24px 0 0;color:{_TEXT};">
  La ringraziamo per aver provato il nostro servizio.<br>
  Cordiali saluti,
</p>"""
    return subject, wrap_customer("Prova gratuita terminata", body)


# ── Internal notifications (SCAO admin) ──────────────────────────────────

def _safe(value) -> str:
    return escape(str(value)) if value not in (None, "") else "—"


def admin_new_signup(domain: dict) -> tuple[str, str]:
    company = domain.get("company_name") or domain["pattern"]
    subject = f"[OS1 Docs] Nuova iscrizione TRIAL: {company}"
    referente = f"{domain.get('contact_first_name') or ''} {domain.get('contact_last_name') or ''}".strip()
    body = _kv_table([
        ("Azienda", f"<strong>{_safe(domain.get('company_name'))}</strong>"),
        ("P.IVA", _safe(domain.get("vat_number"))),
        ("Referente", _safe(referente)),
        ("Email", _safe(domain.get("contact_email"))),
        ("Dominio", f"<code>{_safe(domain['pattern'])}</code>"),
        ("Scadenza trial", _fmt_date(domain.get("expires_at"))),
    ])
    return subject, _wrap_internal("Nuova iscrizione TRIAL", body)


def admin_upgrade_request(
    *, user_email: str, domain: dict, current_tier: str
) -> tuple[str, str]:
    company = domain.get("company_name") or domain["pattern"]
    subject = f"[OS1 Docs] Richiesta upgrade abbonamento: {company}"
    referente = f"{domain.get('contact_first_name') or ''} {domain.get('contact_last_name') or ''}".strip()
    body = _kv_table([
        ("Richiedente", f"<strong>{_safe(user_email)}</strong>"),
        ("Azienda", _safe(domain.get("company_name"))),
        ("P.IVA", _safe(domain.get("vat_number"))),
        ("Dominio", f"<code>{_safe(domain['pattern'])}</code>"),
        ("Tier attuale", f"<strong>{_safe(current_tier)}</strong>"),
        ("Scadenza trial", _fmt_date(domain.get("expires_at"))),
        ("Referente registrazione", _safe(referente)),
        ("Email referente", _safe(domain.get("contact_email"))),
    ])
    return subject, _wrap_internal("Richiesta attivazione abbonamento", body)


def admin_trial_expired(domain: dict) -> tuple[str, str]:
    subject = f"[OS1 Docs] Trial scaduta — downgrade FREE: {domain['pattern']}"
    body = _kv_table([
        ("Azienda", _safe(domain.get("company_name"))),
        ("P.IVA", _safe(domain.get("vat_number"))),
        ("Dominio", f"<code>{_safe(domain['pattern'])}</code>"),
        ("Referente", _safe(domain.get("contact_email"))),
        ("Scadenza", _fmt_date(domain.get("expires_at"))),
    ])
    return subject, _wrap_internal("Trial scaduta — passaggio a piano FREE", body)
