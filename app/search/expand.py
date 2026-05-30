"""Espansione terminologica deterministica per il retrieval OS1.

Mappa curata di sinonimi/acronimi del gergo ERP italiano. Zero LLM, zero
latenza: `expand_terms` estrae i lemmi noti dalla query e restituisce i termini
correlati presenti nel corpus.

Strategia anti-drift: i termini espansi NON sostituiscono la query. Vengono
usati solo nel ramo OR-fallback di SearchIndex._search (quando l'AND sui termini
originali rende pochi risultati), così le query ben formate non vengono mai
spostate di vocabolario — cfr. "Not All Queries Need Rewriting" (arXiv 2603.13301):
su corpus a gergo stabile, espandere query già buone degrada il recall.

La mappa è chiusa sul vocabolario reale dei docs (HELP_MODULE_LABELS + termini
verificati nel corpus) per evitare drift verso termini fuori-corpus.
"""

from __future__ import annotations

from app.search.signals import _get_stemmer, _tokens
from app.search.fts import ITALIAN_STOPWORDS

# Lemma/acronimo ERP → termini correlati nel corpus OS1.
# Chiavi e valori in minuscolo; il matching avviene per stem italiano, quindi
# "fattura" copre anche "fatture", "fatturazione", "fatturare".
ERP_SYNONYMS: dict[str, list[str]] = {
    # Ciclo attivo / passivo
    "fattura": ["documento fiscale", "ciclo attivo"],
    "fatturazione": ["documento fiscale", "ciclo attivo"],
    "ddt": ["documento di trasporto", "bolla"],
    "bolla": ["ddt", "documento di trasporto"],
    "ordine": ["ordine cliente", "ordine fornitore", "commessa"],
    "preventivo": ["offerta", "quotazione"],
    "nota": ["nota di credito", "nota di debito", "accredito"],
    "scadenzario": ["scadenze", "partite aperte", "incassi", "pagamenti"],
    "ritenuta": ["ritenuta acconto", "imposta"],
    "iva": ["imposta", "aliquota", "imponibile"],
    # Magazzino / logistica
    "magazzino": ["giacenze", "movimentazione", "inventario"],
    "giacenza": ["disponibilità", "scorte"],
    "carico": ["movimento di carico", "entrata merce"],
    "scarico": ["movimento di scarico", "uscita merce"],
    "lotto": ["partita", "tracciabilità"],
    "ubicazione": ["locazione", "scaffale", "deposito"],
    # Anagrafiche
    "cliente": ["anagrafica cliente", "ciclo attivo"],
    "fornitore": ["anagrafica fornitore", "ciclo passivo"],
    "articolo": ["prodotto", "anagrafica articolo", "codice articolo"],
    "listino": ["prezzo", "listino prezzi", "tariffa"],
    # Contabilità
    "contabilita": ["prima nota", "registrazione contabile", "partita doppia"],
    "registrazione": ["prima nota", "movimento contabile"],
    "cespite": ["immobilizzazione", "ammortamento"],
    "bilancio": ["conto economico", "stato patrimoniale"],
    "incasso": ["pagamento", "scadenzario", "partite aperte"],
    "pagamento": ["incasso", "scadenzario", "partite aperte"],
    # Produzione
    "distinta": ["distinta base", "bom", "componenti"],
    "commessa": ["ordine di produzione", "lavorazione"],
}


def _build_stemmed_map() -> dict[str, list[str]]:
    """Pre-stemma le chiavi una volta sola (lazy, a import-time)."""
    stemmer = _get_stemmer()
    out: dict[str, list[str]] = {}
    for key, syns in ERP_SYNONYMS.items():
        stem = stemmer.stemWord(key) if stemmer else key
        # Più chiavi possono collassare sullo stesso stem: uniscile.
        out.setdefault(stem, [])
        for s in syns:
            if s not in out[stem]:
                out[stem].append(s)
    return out


_STEMMED_MAP = _build_stemmed_map()


def expand_terms(query: str) -> list[str]:
    """Termini correlati extra per la query (vuoto se nessun lemma noto).

    Stateless, in-memory. I termini originali NON sono inclusi: il chiamante li
    usa solo per arricchire il ramo OR-fallback della ricerca BM25.
    """
    stemmer = _get_stemmer()
    extra: list[str] = []
    seen: set[str] = set()
    for tok in _tokens(query):
        low = tok.lower()
        if len(low) < 3 or low in ITALIAN_STOPWORDS:
            continue
        stem = stemmer.stemWord(low) if stemmer else low
        for syn in _STEMMED_MAP.get(stem, ()):
            if syn not in seen:
                seen.add(syn)
                extra.append(syn)
    return extra
