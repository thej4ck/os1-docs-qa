"""SQLite FTS5 wrapper for document indexing and BM25 search."""

import sqlite3
from pathlib import Path
from typing import Optional


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
        """BM25-ranked full-text search. Returns list of dicts with doc fields + rank."""
        # Escape FTS5 special characters and build query
        fts_query = self._prepare_query(query)
        if not fts_query:
            return []

        if doc_type:
            sql = """
                SELECT d.id, d.source_file, d.module, d.doc_type, d.title,
                       snippet(docs_fts, 1, '<b>', '</b>', '...', 40) AS snippet,
                       d.content,
                       rank
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
                       rank
                FROM docs_fts
                JOIN documents d ON d.id = docs_fts.rowid
                WHERE docs_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            rows = self.conn.execute(sql, (fts_query, limit)).fetchall()

        return [dict(row) for row in rows]

    def _prepare_query(self, query: str) -> str:
        """Turn user query into an FTS5 query.

        Strategy: split into tokens, join with OR so any matching term scores.
        Tokens with special FTS5 chars are quoted.
        """
        tokens = query.strip().split()
        if not tokens:
            return ""
        # Quote each token to avoid FTS5 syntax errors
        safe = []
        for t in tokens:
            # Remove chars that break FTS5 syntax
            cleaned = t.strip('"\'(){}[]<>*^~')
            if cleaned:
                safe.append(f'"{cleaned}"')
        if not safe:
            return ""
        return " OR ".join(safe)

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
