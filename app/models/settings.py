"""Shared accessor for the app_settings key/value store."""

from app.db import get_conn


def get_setting(key: str, default: str = "") -> str:
    try:
        row = get_conn().execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if row and row["value"] is not None:
            return row["value"]
    except Exception:
        pass
    return default


def get_int_setting(key: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
    raw = get_setting(key, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def get_bool_setting(key: str, default: bool) -> bool:
    return get_setting(key, "1" if default else "0") == "1"


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()


def read_setting_standalone(key: str, default: str = "") -> str:
    """Read a setting WITHOUT the shared connection — safe at import time
    (before db.init), e.g. to decide the MCP auth mode while building the app.
    Opens a short-lived read-only connection to APP_DB_PATH; falls back to
    `default` if the DB/table doesn't exist yet."""
    import sqlite3
    from app.config import settings as _s
    try:
        conn = sqlite3.connect(f"file:{_s.app_db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row and row[0] is not None else default
        finally:
            conn.close()
    except Exception:
        return default
