"""Public (unauthenticated) landing for shared answers + lightweight tracking.

A dedicated router with NO session auth — a clear security boundary. It only
reads the immutable snapshot in `shares`; it never joins live conversation data,
and `/api/doc` stays auth-gated so the knowledge base is not exposed to anonymous
visitors (citations render as static chips with a login CTA instead).
"""

import base64
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.config import settings
from app.models.share import (
    get_share,
    increment_cta_click,
    increment_open,
    increment_view,
)
from app.util.email_md import md_to_html
from app.util.ratelimit import allow
from app.version import PRODUCT_NAME

router = APIRouter()

# 1x1 transparent GIF (tracking pixel)
_PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def _templates():
    from app.main import templates
    return templates


def _base_url() -> str:
    return settings.base_url.rstrip("/") if settings.base_url else "https://os1docs.ai.scao.it"


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


@router.get("/prezzi", response_class=HTMLResponse)
async def pricing_page(request: Request):
    """Listino pubblico (no auth): fasce PDL + freemium + CTA prova/iscrizione."""
    from app.models.domain import PRICING_BANDS, TIER_PRESETS, TIER_FREE
    from app.auth.email_sender import get_trial_duration_days
    return _templates().TemplateResponse(request, "public_pricing.html", {
        "request": request,
        "product_name": PRODUCT_NAME,
        "bands": PRICING_BANDS,
        "free_preset": TIER_PRESETS[TIER_FREE],
        "trial_days": get_trial_duration_days(),
        "base_url": _base_url(),
    })


@router.get("/s/{token}", response_class=HTMLResponse)
async def view_share(request: Request, token: str):
    if not allow(f"share-view:{_client_ip(request)}", 120, 60):
        return Response("Troppe richieste. Riprova tra poco.", status_code=429)

    from app.auth.email_sender import get_trial_duration_days
    trial_days = get_trial_duration_days()
    base_url = _base_url()
    share = get_share(token)
    if not share:
        return _templates().TemplateResponse(
            request,
            "public_share.html",
            {"request": request, "not_found": True, "base_url": base_url, "product": PRODUCT_NAME, "trial_days": trial_days},
            status_code=404,
        )

    increment_view(token)
    resp = _templates().TemplateResponse(request, "public_share.html", {
        "request": request,
        "not_found": False,
        "base_url": base_url,
        "product": PRODUCT_NAME,
        "trial_days": trial_days,
        "token": token,
        "answer_html": md_to_html(share["snap_content_md"], base_url),
        "sources": json.loads(share["snap_sources"]) if share["snap_sources"] else [],
        "screenshots": json.loads(share["snap_screenshots"]) if share["snap_screenshots"] else [],
        "question": share["snap_question"] or "",
        "agent": share["snap_agent"] or "",
    })
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@router.get("/s/{token}/pixel.gif")
async def share_pixel(token: str):
    increment_open(token)
    return Response(content=_PIXEL, media_type="image/gif", headers={"Cache-Control": "no-store"})


@router.get("/s/{token}/go")
async def share_cta(token: str):
    increment_cta_click(token)
    return RedirectResponse(url=f"/signup?ref=share&s={token}", status_code=302)
