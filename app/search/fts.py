"""SQLite FTS5 wrapper for document indexing and BM25 search.

Also stores per-document semantic embeddings (model2vec) in a separate
`embeddings` table so the FTS-critical insert path stays untouched.
"""

import sqlite3
from collections.abc import Iterator
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

            -- Semantic embeddings (model2vec). Separate table: the FTS triggers
            -- above insert explicit columns, so they are unaffected by this.
            CREATE TABLE IF NOT EXISTS embeddings (
                doc_id INTEGER PRIMARY KEY,
                vec    BLOB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS embedding_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
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
        self, query: str, limit: int = 10, doc_type: Optional[str] = None,
        topic_filter: Optional[str] = None, expansion: Optional[list[str]] = None,
    ) -> list[dict]:
        """BM25-ranked full-text search with AND-first, OR-fallback strategy."""
        return self._search(query, limit, doc_type, topic_filter, ids_only=False,
                            expansion=expansion)

    def search_ids(
        self, query: str, limit: int = 10, topic_filter: Optional[str] = None,
        expansion: Optional[list[str]] = None,
    ) -> list[int]:
        """BM25-ranked search returning only doc ids (no content/snippet fetch).

        Used by the hybrid retriever, which needs ranked ids for fusion and
        fetches full rows only for the small fused set.
        """
        return self._search(query, limit, None, topic_filter, ids_only=True,
                            expansion=expansion)

    def _search(
        self, query: str, limit: int, doc_type: Optional[str],
        topic_filter: Optional[str], ids_only: bool,
        expansion: Optional[list[str]] = None,
    ):
        tokens = self._clean_tokens(query)
        if not tokens:
            return []

        # Try AND (all terms must match) first — original terms only, no drift.
        and_query = " AND ".join(tokens)
        results = self._execute_search(and_query, limit, doc_type, topic_filter, ids_only)

        # Fall back to OR if AND returns too few results. Synonyms enter ONLY
        # here: well-formed queries keep their vocabulary; low-recall queries
        # get the curated ERP synonyms (each a quoted FTS phrase) ORed in.
        if len(results) < _MIN_AND_RESULTS and (len(tokens) > 1 or expansion):
            or_terms = list(tokens)
            if expansion:
                or_terms += [f'"{s}"' for s in expansion]
            or_query = " OR ".join(or_terms)
            results = self._execute_search(or_query, limit, doc_type, topic_filter, ids_only)

        return results

    def _execute_search(
        self, fts_query: str, limit: int, doc_type: Optional[str] = None,
        topic_filter: Optional[str] = None, ids_only: bool = False,
    ):
        """Run a single FTS5 MATCH query with title-boosted BM25 ranking."""
        if ids_only:
            select = "SELECT d.id"
        else:
            select = """SELECT d.id, d.source_file, d.module, d.doc_type, d.title,
                   snippet(docs_fts, 1, '<b>', '</b>', '...', 40) AS snippet,
                   d.content,
                   bm25(docs_fts, 10.0, 1.0) AS rank"""
        base_sql = f"""
            {select}
            FROM docs_fts
            JOIN documents d ON d.id = docs_fts.rowid
            WHERE docs_fts MATCH ?
        """
        params: list = [fts_query]

        if doc_type:
            base_sql += " AND d.doc_type = ?"
            params.append(doc_type)

        if topic_filter:
            # Match topic in source_file path OR module field
            base_sql += " AND (REPLACE(d.source_file, '\\', '/') LIKE ? OR d.module = ?)"
            params.append(f"%/{topic_filter}/%")
            params.append(topic_filter)

        base_sql += " ORDER BY bm25(docs_fts, 10.0, 1.0) LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(base_sql, params).fetchall()
        if ids_only:
            return [row[0] for row in rows]
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

    # ── Semantic embeddings ──────────────────────────────────────────────

    def iter_documents_for_embedding(self) -> Iterator[tuple[int, str, str]]:
        """Yield (id, title, content) for every document, ordered by id."""
        cur = self.conn.execute(
            "SELECT id, title, content FROM documents ORDER BY id"
        )
        for row in cur:
            yield row["id"], row["title"] or "", row["content"]

    def store_embedding(self, doc_id: int, blob: bytes):
        """Upsert one document's embedding (float32 little-endian bytes)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO embeddings (doc_id, vec) VALUES (?, ?)",
            (doc_id, blob),
        )

    def clear_embeddings(self):
        self.conn.execute("DELETE FROM embeddings")

    def set_embedding_meta(self, key: str, value: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO embedding_meta (key, value) VALUES (?, ?)",
            (key, str(value)),
        )

    def get_embedding_meta(self, key: str) -> Optional[str]:
        try:
            row = self.conn.execute(
                "SELECT value FROM embedding_meta WHERE key = ?", (key,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return row["value"] if row else None

    def embedding_count(self) -> int:
        try:
            row = self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
        except sqlite3.OperationalError:
            return 0
        return row[0]

    def load_embedding_matrix(self):
        """Load all embeddings into (ids, matrix) numpy arrays.

        Returns (np.ndarray[int64] (n,), np.ndarray[float32] (n, dim)) or
        (None, None) if there are no embeddings. Vectors are stored already
        L2-normalized at build time.
        """
        import numpy as np

        dim_str = self.get_embedding_meta("dim")
        rows = self.conn.execute(
            "SELECT doc_id, vec FROM embeddings ORDER BY doc_id"
        ).fetchall()
        if not rows:
            return None, None
        dim = int(dim_str) if dim_str else len(rows[0]["vec"]) // 4
        ids = np.empty(len(rows), dtype=np.int64)
        mat = np.empty((len(rows), dim), dtype=np.float32)
        for i, row in enumerate(rows):
            ids[i] = row["doc_id"]
            mat[i] = np.frombuffer(row["vec"], dtype=np.float32, count=dim)
        return ids, mat

    def get_documents_by_ids(self, ids: list[int]) -> list[dict]:
        """Fetch full document rows for a list of ids, preserving id order."""
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT id, source_file, module, doc_type, title, content "
            f"FROM documents WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        by_id = {row["id"]: dict(row) for row in rows}
        return [by_id[i] for i in ids if i in by_id]

    def get_document(self, source_file: str) -> dict | None:
        """Fetch one document by source_file, tolerant to slash format / basename.

        Mirrors the /api/doc viewer fallback chain (source_file is stored
        inconsistently — back/forward slash): exact → slash-normalized → basename.
        """
        cols = "id, source_file, module, doc_type, title, content, html_content"
        row = self.conn.execute(
            f"SELECT {cols} FROM documents WHERE source_file = ? LIMIT 1", (source_file,)
        ).fetchone()
        if not row:
            norm = source_file.replace("\\", "/")
            row = self.conn.execute(
                f"SELECT {cols} FROM documents WHERE REPLACE(source_file, '\\', '/') = ? LIMIT 1",
                (norm,),
            ).fetchone()
        if not row:
            base = source_file.replace("\\", "/").rstrip("/").split("/")[-1]
            if base:
                row = self.conn.execute(
                    f"SELECT {cols} FROM documents WHERE source_file LIKE ? LIMIT 1",
                    ("%" + base,),
                ).fetchone()
        return dict(row) if row else None

    def rebuild(self):
        """Drop all data and recreate the schema."""
        cur = self.conn.cursor()
        cur.executescript("""
            DROP TRIGGER IF EXISTS docs_ai;
            DROP TRIGGER IF EXISTS docs_ad;
            DROP TABLE IF EXISTS docs_fts;
            DROP TABLE IF EXISTS documents;
            DROP TABLE IF EXISTS embeddings;
            DROP TABLE IF EXISTS embedding_meta;
        """)
        self.conn.commit()
        self._create_schema()

    def close(self):
        self.conn.close()
