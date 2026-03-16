"""Cookie-based session management using itsdangerous."""

from typing import Optional

from fastapi import Request, Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.config import settings

COOKIE_NAME = "os1_session"
MAX_AGE = 86400  # 24 hours


def _get_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key)


def create_session(response: Response, email: str):
    """Set a signed session cookie with the user's email."""
    s = _get_serializer()
    token = s.dumps({"email": email})
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def get_session_email(request: Request) -> Optional[str]:
    """Extract the email from the session cookie, or None if invalid/expired."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    s = _get_serializer()
    try:
        data = s.loads(token, max_age=MAX_AGE)
        return data.get("email")
    except (BadSignature, SignatureExpired):
        return None


def clear_session(response: Response):
    """Delete the session cookie."""
    response.delete_cookie(key=COOKIE_NAME)
