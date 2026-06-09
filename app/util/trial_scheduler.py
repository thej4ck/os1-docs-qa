"""Background scheduler per i trial freemium.

Un task asyncio (avviato nel lifespan) gira periodicamente e, per ogni dominio
in stato `trial`:
  1. invia l'email-drip del giorno corrente (1 funzione/giorno, idempotente via
     `trial_drip_day`);
  2. al superamento di `expires_at` esegue il downgrade a FREE (anche senza che
     l'utente faccia login — il downgrade lazy resta come fallback).

In-memory, single-process: coerente col resto dell'infra (rate-limit, ecc.).
"""

import asyncio
from datetime import datetime, timezone

from app.config import settings
from app.models.domain import (
    check_and_downgrade_if_expired,
    list_active_trials,
    parse_iso_utc,
    set_trial_drip_day,
    trial_days_left,
)

_DEFAULT_INTERVAL_S = 6 * 3600  # ogni 6 ore (la drip avanza di 1 giorno/giorno)


def _app_url() -> str:
    base = (settings.base_url or "").rstrip("/")
    return f"{base}/chat" if base else "https://os1.ai.scao.it/chat"


def run_trial_maintenance() -> None:
    """Un passaggio sincrono su tutti i trial attivi. Best-effort, idempotente."""
    from app.auth.email_sender import send_email
    from app.auth.email_templates import trial_drip

    app_url = _app_url()
    now = datetime.now(timezone.utc)

    for domain in list_active_trials():
        created = parse_iso_utc(domain.get("created_at"))
        if created is None:
            continue
        days_left = trial_days_left(domain.get("expires_at"))

        # Giorno-drip dai giorni trascorsi dalla creazione (robusto a durate
        # diverse dal setting corrente). All'ultimo giorno forziamo il pitch
        # finale (day=7) anche se il trial fosse più corto.
        target = (now - created).days
        if days_left is not None and days_left <= 1:
            target = 7  # pitch finale garantito vicino alla scadenza
        target = max(0, min(7, target))

        sent = domain.get("trial_drip_day", 0) or 0
        if target >= 1 and target > sent and domain.get("contact_email"):
            try:
                subject, html = trial_drip(
                    day=target,
                    first_name=domain.get("contact_first_name") or "",
                    app_url=app_url,
                )
                if send_email(domain["contact_email"], subject, html):
                    set_trial_drip_day(domain["id"], target)
            except Exception as e:  # noqa: BLE001 — non bloccare gli altri domini
                print(f"[trial_scheduler] drip day={target} failed for "
                      f"{domain.get('pattern')}: {e}", flush=True)

        # Downgrade a scadenza superata (check interno: solo se now > expires_at).
        if days_left == 0:
            try:
                check_and_downgrade_if_expired(domain)
            except Exception as e:  # noqa: BLE001
                print(f"[trial_scheduler] downgrade failed for "
                      f"{domain.get('pattern')}: {e}", flush=True)

    # Pulizia token MCP scaduti/revocati: evita che le righe restino a lungo.
    try:
        from app.models.oauth import purge_expired_tokens
        purge_expired_tokens()
    except Exception as e:  # noqa: BLE001
        print(f"[trial_scheduler] purge MCP tokens failed: {e}", flush=True)


async def trial_scheduler_loop(interval_seconds: int = _DEFAULT_INTERVAL_S) -> None:
    """Loop infinito: maintenance + sleep. Cancellabile (lifespan shutdown)."""
    # Piccolo ritardo iniziale: lascia completare l'avvio dell'app.
    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.to_thread(run_trial_maintenance)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[trial_scheduler] maintenance pass failed: {e}", flush=True)
        await asyncio.sleep(interval_seconds)
