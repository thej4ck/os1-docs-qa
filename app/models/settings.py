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
