"""Shared input validators."""

import re

# Pragmatic email shape check (not full RFC 5321). Single source of truth used by
# signup and answer-sharing flows.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
