"""The QR code of an article (Spec 8.4): the fixed address `/a/<id>` it carries and its pictures.

`/a/<id>` sits behind the sign-in like every page: whoever scans it without a session is sent to
the login page and, after signing in, back to this address (`next`, kept local by `safe_next`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from reprintbuddy.domain import inspection
from reprintbuddy.models import Article
from reprintbuddy.web import listing, qr
from reprintbuddy.web.deps import get_db
from reprintbuddy.web.routes.inspection import ASSEMBLY_URL, INSPECTION_URL  # the lists' own paths

router = APIRouter()

# The target changes with every booking (never cached),
# the pictures change with the setting.
_NO_STORE = {"Cache-Control": "no-store"}
_PICTURES = {"svg": ("image/svg+xml", qr.svg), "png": ("image/png", qr.png)}


def target_of(db: Session, article: Article) -> str:
    """Where a scan of the article's code leads (Spec 8.4), decided by the lists themselves so the
    link never ends on an empty list: prints waiting for inspection, else units waiting for
    assembly (the list shows no archived article), else the article's page."""
    if inspection.list_open(db, article.id):
        return listing.with_article(INSPECTION_URL, article.id)
    if inspection.list_awaiting_assembly(db, article.id):
        return listing.with_article(ASSEMBLY_URL, article.id)
    return f"/articles/{article.id}"


def _article_or_404(db: Session, article_id: int) -> Article:
    article = db.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404)
    return article


@router.get("/a/{article_id:int}")
async def scan(article_id: int, db: Session = Depends(get_db)):
    article = _article_or_404(db, article_id)
    return RedirectResponse(target_of(db, article), status_code=303, headers=_NO_STORE)


@router.get("/articles/{article_id:int}/qr.{kind}")
async def picture(request: Request, article_id: int, kind: str, download: str | None = None,
                  db: Session = Depends(get_db)):
    if kind not in _PICTURES:
        raise HTTPException(status_code=404)
    _article_or_404(db, article_id)
    content_type, draw = _PICTURES[kind]
    headers = dict(_NO_STORE)
    if download == "1":
        # The name holds the id only: no article name, nothing that needs quoting.
        headers["Content-Disposition"] = f'attachment; filename="qr-article-{article_id}.{kind}"'
    body = draw(qr.article_address(qr.base_address(request, db), article_id))
    return Response(body, media_type=content_type, headers=headers)
