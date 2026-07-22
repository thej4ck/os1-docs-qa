"""Contratto del marcatore screenshot: `[Screenshot: caption | url]`.

Emesso da `scripts/build_index.py` dentro i chunk, consumato app-side da
`app/search/query.py`, `app/mcp/tools.py` e `app/util/email_md.py`.
Single source of truth della regex → un cambio di formato tocca un punto solo.

NB: restano volutamente distinte due varianti che NON sono questa:
- lo strip non-catturante `\\[Screenshot:[^\\]]*\\]` (chat_routes preview, chat.html JS);
- la variante `/help-files/`-only nello script standalone describe_images.py.
"""

import re

# Cattura (caption, url) da un marcatore. Usata per estrarre o riscrivere i marcatori.
SCREENSHOT_RE = re.compile(r'\[Screenshot:\s*(.+?)\s*\|\s*(.+?)\s*\]')
