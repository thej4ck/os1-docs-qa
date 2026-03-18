"""SQLite FTS5 wrapper for document indexing and BM25 search."""

import sqlite3
from pathlib import Path
from typing import Optional

# Italian stopwords — articles, prepositions, conjunctions, pronouns, common verbs.
# Intentionally excludes short terms that could be OS1 module codes or technical terms.
ITALIAN_STOPWORDS = frozenset({
    # Articles
    "il", "lo", "la", "i", "gli", "le", "l", "un", "uno", "una",
    # Prepositions
    "di", "a", "da", "in", "con", "su", "per", "tra", "fra",
    # Articulated prepositions
    "del", "dello", "della", "dei", "degli", "delle",
    "al", "allo", "alla", "ai", "agli", "alle",
    "dal", "dallo", "dalla", "dai", "dagli", "dalle",
    "nel", "nello", "nella", "nei", "negli", "nelle",
    "sul", "sullo", "sulla", "sui", "sugli", "sulle",
    # Conjunctions
    "e", "o", "ma", "che", "se", "come", "quando", "anche", "dove",
    # Pronouns / determiners
    "mi", "ti", "si", "ci", "vi", "ne", "me", "te", "lui", "lei",
    "noi", "voi", "loro", "questo", "questa", "questi", "queste",
    "quello", "quella", "quelli", "quelle", "quale", "quali",
    # Common auxiliary/copula verbs
    "è", "sono", "ha", "hanno", "essere", "avere",
    "sia", "può", "fare", "fatto", "viene",
    # Frequent functional words
    "non", "più", "già", "ancora", "solo", "ogni", "tutto", "tutti",
    "dopo", "prima", "altro", "altri", "altra", "altre",
    "molto", "poco", "tanto", "quanto", "così", "però",
})

# Minimum results for AND before falling back to OR
_MIN_AND_RESULTS = 3


class SearchIndex:
    """Read/write wrapper around SQLite FTS5."""

    def __init__(self, db_path: str, read_only: bool = False):
        self.db_path = db_path
        if read_only:
            uri = f"file:{db_path}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        if not read_only:
            self._create_schema()

    def _create_schema(self):
        cur = self.conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY,
                source_file TEXT NOT NULL,
                module TEXT,
                doc_type TEXT,
                title TEXT,
                content TEXT NOT NULL,
                html_content TEXT,
                indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
                title,
                content,
                content=documents,
                content_rowid=id,
                tokenize='unicode61 remove_diacritics 2'
            );

            CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON documents BEGIN
                INSERT INTO docs_fts(rowid, title, content)
                VALUES (new.id, new.title, new.content);
            END;

            CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON documents BEGIN
                INSERT INTO docs_fts(docs_fts, rowid, title, content)
                VALUES ('delete', old.id, old.title, old.content);
            END;
        """)
        self.conn.commit()

    def index_document(
        self,
        content: str,
        source_file: str,
        title: str = "",
        module: str = "",
        doc_type: str = "",
        html_content: str = "",
    ):
        self.conn.execute(
            "INSERT INTO documents (source_file, module, doc_type, title, content, html_content) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source_file, module, doc_type, title, content, html_content or None),
        )

    def commit(self):
        self.conn.commit()

    def search(
        self, query: str, limit: int = 10, doc_type: Optional[str] = None
    ) -> list[dict]:
        """BM25-ranked full-text search with AND-first, OR-fallback strategy."""
        tokens = self._clean_tokens(query)
        if not tokens:
            return []

        # Try AND (all terms must match) first
        and_query = " AND ".join(tokens)
        results = self._execute_search(and_query, limit, doc_type)

        # Fall back to OR if AND returns too few results
        if len(results) < _MIN_AND_RESULTS and len(tokens) > 1:
            or_query = " OR ".join(tokens)
            results = self._execute_search(or_query, limit, doc_type)

        return results

    def _execute_search(
        self, fts_query: str, limit: int, doc_type: Optional[str] = None
    ) -> list[dict]:
        """Run a single FTS5 MATCH query with title-boosted BM25 ranking."""
        if doc_type:
            sql = """
                SELECT d.id, d.source_file, d.module, d.doc_type, d.title,
                       snippet(docs_fts, 1, '<b>', '</b>', '...', 40) AS snippet,
                       d.content,
                       bm25(docs_fts, 10.0, 1.0) AS rank
                FROM docs_fts
                JOIN documents d ON d.id = docs_fts.rowid
                WHERE docs_fts MATCH ?
                  AND d.doc_type = ?
                ORDER BY rank
                LIMIT ?
            """
            rows = self.conn.execute(sql, (fts_query, doc_type, limit)).fetchall()
        else:
            sql = """
                SELECT d.id, d.source_file, d.module, d.doc_type, d.title,
                       snippet(docs_fts, 1, '<b>', '</b>', '...', 40) AS snippet,
                       d.content,
                       bm25(docs_fts, 10.0, 1.0) AS rank
                FROM docs_fts
                JOIN documents d ON d.id = docs_fts.rowid
                WHERE docs_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            rows = self.conn.execute(sql, (fts_query, limit)).fetchall()

        return [dict(row) for row in rows]

    def _clean_tokens(self, query: str) -> list[str]:
        """Tokenize query, remove stopwords, quote for FTS5 safety."""
        raw_tokens = query.strip().split()
        if not raw_tokens:
            return []

        safe = []
        for t in raw_tokens:
            cleaned = t.strip('"\'(){}[]<>*^~?!.,;:').lower()
            if cleaned and cleaned not in ITALIAN_STOPWORDS:
                safe.append(f'"{cleaned}"')

        # If all tokens were stopwords, fall back to original tokens
        if not safe:
            safe = []
            for t in raw_tokens:
                cleaned = t.strip('"\'(){}[]<>*^~')
                if cleaned:
                    safe.append(f'"{cleaned}"')

        return safe

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()
        return row[0]

    def rebuild(self):
        """Drop all data and recreate the schema."""
        cur = self.conn.cursor()
        cur.executescript("""
            DROP TRIGGER IF EXISTS docs_ai;
            DROP TRIGGER IF EXISTS docs_ad;
            DROP TABLE IF EXISTS docs_fts;
            DROP TABLE IF EXISTS documents;
        """)
        self.conn.commit()
        self._create_schema()

    def close(self):
        self.conn.close()
