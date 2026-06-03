"""Esperti specialisti (personas) per il Q&A OS1.

Selezione SOLO manuale dall'UI: nessuna classificazione di intent, nessun
query-rewriter. Ogni esperto condivide gli stessi chunk RAG: cambia solo lo
*stile* del prompt. Il system prompt è composto come CORE + STILE, dove il CORE
(grounding) vive in query.py e non contiene regole di formato: così il prompt
base non può mai contraddire l'assistant prompt.
"""

from __future__ import annotations

# ── Stili degli esperti (testi del brief) ──

IL_MANUALE_STYLE = """\
Sei un esperto di ERP con 20 anni di esperienza nella consulenza a PMI italiane.
Rispondi SOLO a domande concettuali: cosa significa un termine, come funziona un processo, perché esiste una regola.
NON fornire mai istruzioni operative passo-passo: se la domanda è procedurale, di' all'utente di usare la modalità "Guidami".
Stile: chiaro, professionale, mai tecnico oltre il necessario. Usa analogie pratiche legate al contesto PMI italiano.
Lunghezza: 3-6 frasi. Mai elenchi puntati lunghi."""

GUIDAMI_STYLE = """\
Sei un assistente operativo per utenti ERP. Guidi l'utente in procedure specifiche in modo COMPLETO e accurato.
Struttura ogni risposta:
1. Prerequisiti (cosa serve prima di iniziare)
2. Passi numerati: per ciascuno indica menu/pulsante esatto -> azione -> risultato atteso. Elenca TUTTI i passi necessari per portare a termine la procedura, senza ometterne; aggiungi sotto-punti per i campi da compilare quando utile.
3. Verifica finale (come l'utente sa di aver avuto successo)
4. Se qualcosa va storto (rimanda all'esperto "Ho un problema")
Resta operativo e concreto (poca teoria), ma NON limitare artificialmente la lunghezza: meglio una guida completa fino in fondo che una sbrigativa. Copri ogni passaggio della procedura."""

PROBLEMA_STYLE = """\
Sei un tecnico di supporto ERP specializzato nella diagnosi di problemi per PMI italiane.
Quando ricevi un problema:
1. Chiedi UNA sola domanda di chiarimento se indispensabile, altrimenti procedi direttamente.
2. Elenca le cause probabili in ordine di frequenza (di norma 2-4, ma includi tutte quelle rilevanti per il caso).
3. Per ogni causa: sintomo riconoscibile -> verifica rapida -> soluzione passo-passo completa.
4. Se il problema richiede intervento tecnico o consulente, dillo esplicitamente e non improvvisare.
Tono: calmo, rassicurante, mai allarmista. L'utente è già stressato. Sii completo: copri verifiche e soluzioni fino in fondo, senza troncare."""

ONBOARDING_STYLE = """\
Sei un formatore ERP paziente e incoraggiante. L'utente è alle prime armi con il sistema.
Regole assolute:
- Zero acronimi non spiegati
- Ogni risposta include un'analogia con un processo aziendale comune (fattura cartacea, registro, ecc.)
- Dopo ogni risposta, suggerisci UNA cosa correlata da esplorare come prossimo passo
- Se l'utente sembra confuso, semplifica ulteriormente senza mai farglielo notare
Obiettivo: l'utente deve sentirsi capace, non giudicato."""


# ── Registry esperti ──

AGENTS: dict[str, dict] = {
    "virgilio": {
        "id": "virgilio",
        "label": "Il Manuale",
        "display_name": "Virgilio",
        "tagline": "l'esperto",
        "emoji": "📖",
        "role": "Spiega concetti e significati. Chiedigli cosa vuol dire un "
                "termine o come funziona un processo, non i passaggi operativi.",
        "avatar": "/static/img/agents/virgilio.png",
        "style": IL_MANUALE_STYLE,
        "brief": True,  # risposta breve -> sopprime il deep addendum
    },
    "pilota": {
        "id": "pilota",
        "label": "Guidami",
        "display_name": "Pilota",
        "tagline": "la guida",
        "emoji": "🧭",
        "role": "Ti guida passo-passo. Chiedigli come fare un'operazione "
                "concreta in OS1 e segui le istruzioni.",
        "avatar": "/static/img/agents/pilota.png",
        "style": GUIDAMI_STYLE,
        "brief": False,
    },
    "doc": {
        "id": "doc",
        "label": "Ho un problema",
        "display_name": "Doc",
        "tagline": "il tecnico",
        "emoji": "🔧",
        "role": "Diagnostica errori e blocchi. Raccontagli cosa non funziona "
                "e trova la causa con te, con calma.",
        "avatar": "/static/img/agents/doc.png",
        "style": PROBLEMA_STYLE,
        "brief": False,
    },
    "stella": {
        "id": "stella",
        "label": "Sono nuovo",
        "display_name": "Stella",
        "tagline": "la tutor",
        "emoji": "☀️",
        "role": "Per chi inizia. Spiega tutto con calma e analogie, senza dare "
                "nulla per scontato.",
        "avatar": "/static/img/agents/stella.png",
        "style": ONBOARDING_STYLE,
        "brief": False,
    },
}

# Ordine di visualizzazione nelle card (Standard mostrato dal frontend a parte).
AGENT_ORDER = ["virgilio", "pilota", "doc", "stella"]


# Riuso del wrapper single-key di query.py (stessa semantica: vuoto -> default).
from app.search.query import _get_prompt_setting


def _get_prompt_settings(keys: list[str]) -> dict[str, str]:
    """Legge più chiavi app_settings in UNA query; valori vuoti/whitespace omessi."""
    out: dict[str, str] = {}
    if not keys:
        return out
    try:
        from app.db import get_conn
        placeholders = ",".join("?" * len(keys))
        rows = get_conn().execute(
            f"SELECT key, value FROM app_settings WHERE key IN ({placeholders})",
            keys,
        ).fetchall()
        for r in rows:
            if r["value"] and r["value"].strip():
                out[r["key"]] = r["value"]
    except Exception:
        pass
    return out


def get_agent(agent_id: str | None) -> dict | None:
    """Esperto per id, o None per il default generico (id sconosciuto -> None)."""
    if not agent_id:
        return None
    return AGENTS.get(agent_id)


def is_brief_agent(agent_id: str | None) -> bool:
    """True se l'esperto impone risposte brevi (per sopprimere il deep addendum)."""
    ag = get_agent(agent_id)
    return bool(ag and ag.get("brief"))


def build_system_prompt(
    agent_id: str | None,
    *,
    module: str | None = None,
    role: str | None = None,
) -> str:
    """Compone CORE (grounding) + STILE (esperto o standard) + contesto sessione."""
    from app.search.query import CORE_SYSTEM_PROMPT, STYLE_STANDARD_DEFAULT

    ag = get_agent(agent_id)
    style_key = "system_prompt" if ag is None else f"agent_prompt_{ag['id']}"
    style_default = STYLE_STANDARD_DEFAULT if ag is None else ag["style"]
    # core + stile in una sola lettura (hot path /api/ask)
    overrides = _get_prompt_settings(["core_system_prompt", style_key])
    core = overrides.get("core_system_prompt", CORE_SYSTEM_PROMPT)
    style = overrides.get(style_key, style_default)

    prompt = f"{core}\n\n{style}"

    ctx_parts = []
    if module:
        ctx_parts.append(f"modulo OS1 = {module}")
    if role:
        ctx_parts.append(f"ruolo utente = {role}")
    if ctx_parts:
        prompt += "\n\nCONTESTO SESSIONE: " + "; ".join(ctx_parts) + "."
    return prompt


def _public_view(ag: dict, role: str | None = None) -> dict:
    """Proiezione pubblica di un esperto (MAI i prompt). `role` override opzionale."""
    return {
        "id": ag["id"],
        "label": ag["label"],
        "display_name": ag["display_name"],
        "tagline": ag["tagline"],
        "emoji": ag["emoji"],
        "role": role if role is not None else ag["role"],
        "avatar": ag["avatar"],
    }


def list_agents_public() -> list[dict]:
    """Metadati per il template (MAI i prompt). Una sola lettura settings."""
    overrides = _get_prompt_settings([f"agent_role_{aid}" for aid in AGENT_ORDER])
    return [
        _public_view(AGENTS[aid], overrides.get(f"agent_role_{aid}"))
        for aid in AGENT_ORDER
    ]


def get_agent_public(agent_id: str | None) -> dict | None:
    """Metadati pubblici di un singolo esperto (per badge a chat attiva)."""
    ag = get_agent(agent_id)
    return _public_view(ag) if ag else None


# Parole-spia di formato: se compaiono nel prompt CORE, segnalano un possibile
# conflitto con lo stile degli esperti (la guardia è advisory, non bloccante).
import re as _re

_FORMAT_HINTS = _re.compile(
    r"\b(elenc|punti|passi numerati|\d+\s*frasi|schema|tabella|grassetto|"
    r"emoji|paragraf|h1|h2|###|lunghezza)\b",
    _re.IGNORECASE,
)


def check_prompt_coherence(core_text: str) -> list[str]:
    """Avvisi se il CORE impone regole di formato (che gli esperti sovrascrivono)."""
    warnings = []
    for m in _FORMAT_HINTS.finditer(core_text or ""):
        snippet = core_text[max(0, m.start() - 25): m.end() + 25].replace("\n", " ").strip()
        warnings.append(snippet)
        if len(warnings) >= 3:
            break
    return warnings
