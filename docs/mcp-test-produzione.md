# Test in produzione — Connettore MCP OS1 Virgilio

Guida passo-passo per verificare la funzionalità **MCP** (Model Context Protocol) in
produzione e collegarla da **Claude** (claude.ai / Claude Desktop — *non* Claude Code).

Il server MCP espone **solo retrieval** (tool `search` + `fetch`) sulla documentazione OS1.
Endpoint pubblico: **`https://os1.ai.scao.it/mcp`** (l'endpoint reale è `/mcp/`; `/mcp` fa 307).

---

## 0. Prerequisiti

- Deploy su Railway andato a buon fine con **build ≥ 79** (verifica sotto).
- Variabili d'ambiente Railway:
  - **`BASE_URL = https://os1.ai.scao.it`** ← **obbligatoria**: senza, gli URL di discovery OAuth sono sbagliati e i connettori non si collegano.
  - `RESEND_API_KEY` configurata (serve a inviare le **OTP via email** durante il login del connettore).
  - `PRODUCTION = true`.
- Piano Claude che supporta i **custom connector** (Pro / Max / Team / Enterprise).
- L'utente che collega il connettore deve avere un'**email di un dominio abilitato** in OS1.

---

## 1. Lato piattaforma — controlli admin (prima del test)

### 1.1 Deploy attivo
```
GET https://os1.ai.scao.it/healthz   → {"status":"ok","version":"2.1.0","build":79}
```

### 1.2 Pagina admin MCP
Vai su **`https://os1.ai.scao.it/admin/mcp`** e verifica:
- **Stato attuale**: `attivo` + `auth: oauth`.
- **Master (risponde?)**: ON. (Se OFF → l'endpoint risponde 503; accendilo, effetto immediato.)
- **Modalità auth**: `oauth`. (Se la cambi, **applica al riavvio** del servizio.)
- **Endpoint** mostrato: `https://os1.ai.scao.it/mcp/`.

> In produzione MCP è **on di default** (master + oauth) anche senza variabili d'ambiente,
> purché `BASE_URL` sia settata.

### 1.3 Dominio dell'utente abilitato a MCP
Vai su **`/admin/domains`**: il dominio dell'utente di test (es. `*@aiwonder.it`) deve avere
il toggle **MCP** = ON (default abilitato). Se OFF, gli utenti di quel dominio ricevono 401 dai tool.

### 1.4 Discovery OAuth raggiungibile (a root del dominio)
```
GET https://os1.ai.scao.it/.well-known/oauth-protected-resource/mcp
GET https://os1.ai.scao.it/.well-known/oauth-authorization-server/mcp
```
Entrambe devono restituire **JSON** (200). Nella seconda, controlla che:
- `authorization_endpoint` = `https://os1.ai.scao.it/mcp/authorize`
- `token_endpoint` = `https://os1.ai.scao.it/mcp/token`
- `registration_endpoint` = `https://os1.ai.scao.it/mcp/register`

Se questi URL puntano a `localhost` o porte strane → `BASE_URL` non è settata correttamente.

---

## 2. Lato utente — aggiungere il connettore su Claude

> Funziona su **claude.ai** (web) e **Claude Desktop**. NON è Claude Code.

1. Apri **Claude** → **Settings** (Impostazioni) → **Connectors** (Connettori).
2. **Add custom connector** / "Aggiungi connettore personalizzato".
3. **Name**: `OS1 Virgilio` (a piacere).
   **Remote MCP server URL**: `https://os1.ai.scao.it/mcp`
4. Conferma. Claude scopre automaticamente l'auth (OAuth) e avvia il flusso.
5. Si apre una finestra del browser sulla **pagina di login OS1** (`/mcp-login`):
   - inserisci la tua **email aziendale** (dominio abilitato) → **Continua**;
   - ti arriva un **codice OTP via email** → inseriscilo → **Autorizza**;
   - (consenso allo scope `docs:read`).
6. Torni su Claude: il connettore risulta **collegato**.

---

## 3. Verifica che funzioni

### 3.1 Da Claude
In una **nuova chat**, attiva il connettore OS1 e fai una domanda sulla documentazione OS1,
es. *"Come funziona l'anagrafica articoli in OS1?"*. Claude deve:
- chiamare i tool **`search`** e **`fetch`** del connettore;
- rispondere citando i documenti (con URL `…/api/doc?file=…`).

### 3.2 Da admin (riscontro)
Su **`/admin/mcp`** dopo il collegamento:
- **Client OAuth registrati**: compare una riga (il client di Claude, registrato via DCR);
- **Token attivi**: compare un token `access` per la tua email.

Da qui puoi **revocare** un client (stacca il connettore + invalida i suoi token) o un singolo token.

---

## 4. Troubleshooting

| Sintomo | Causa probabile | Fix |
|---|---|---|
| Claude non si collega / discovery fallisce | `BASE_URL` non settata o sbagliata | setta `BASE_URL=https://os1.ai.scao.it` su Railway, redeploy |
| Endpoint risponde **503** | master switch OFF | `/admin/mcp` → Master ON (immediato) |
| `.well-known` 404 | deploy vecchio o auth mode ≠ oauth | verifica build ≥ 79 e auth mode `oauth` (riavvia se cambiata) |
| OTP non arriva | `RESEND_API_KEY` mancante o email non autorizzata | configura Resend; verifica dominio in `/admin/domains` |
| **401** dopo il login | dominio con **MCP off** o token revocato | `/admin/domains` → MCP ON per il dominio |
| **429** | rate-limit per-IP (DCR 10/h, login 20/min, tool 120/min) | attendi la finestra |
| Connettore assente nelle impostazioni Claude | piano senza custom connector | usa piano Pro/Max/Team/Enterprise |

---

## 5. Nota — ChatGPT

Stesso endpoint `https://os1.ai.scao.it/mcp`. Su ChatGPT (Developer Mode / connettori, piani
a pagamento) il flusso è identico: aggiunta del server remoto → OAuth (login OTP) → tool
`search`/`fetch` in Deep Research. Richiede sempre `BASE_URL` corretta + dominio abilitato.

---

## 6. Rollback rapido

- **Spegnere MCP** senza deploy: `/admin/mcp` → Master OFF (immediato, risponde 503).
- **Disabilitare per un dominio**: `/admin/domains` → toggle MCP OFF.
- **Staccare un client**: `/admin/mcp` → Revoca sul client/token.
