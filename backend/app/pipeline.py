"""Daily pipeline: scrape -> AI structure -> create draft posts."""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from . import ai, scraper
from .db import SessionLocal
from .models import Article, Post, Source

logger = logging.getLogger(__name__)


def run_pipeline_for_source(db: Session, source: Source) -> dict:
    logger.info("scraping %s (%s)", source.name, source.url)
    items = scraper.scrape_source(
        url=source.url, rss_url=source.rss_url, mode=source.scrape_mode,
    )
    # Per-source keyword filter: only keep items whose title + text matches at
    # least one of the source's comma-separated keywords (when configured).
    kw_raw = (source.keywords or "").strip()
    keywords = [k.strip().lower() for k in kw_raw.split(",") if k.strip()] if kw_raw else []
    if keywords:
        filtered = []
        for it in items:
            hay = ((it.title or "") + " " + (it.text or "")).lower()
            if any(k in hay for k in keywords):
                filtered.append(it)
        logger.info("keyword filter on %s: %d -> %d items", source.name, len(items), len(filtered))
        items = filtered
    new_count = 0
    drafted = 0
    skipped_irrelevant = 0
    for it in items:
        if db.query(Article).filter_by(url=it.url).first():
            continue
        article = Article(
            source_id=source.id,
            url=it.url,
            title=it.title[:500],
            author=it.author,
            published_at=it.published_at,
            raw_text=it.text or "",
            image_url=None,  # we no longer surface photo previews — line icons only
            entities={},
            status="pending",
        )
        db.add(article)
        db.flush()
        new_count += 1
        # AI structure + entity extraction
        try:
            data = ai.structure_article(
                url=it.url, title=it.title, text=it.text or "", html=getattr(it, "html", "") or "",
            )
            entities = data.get("entities") or {}
            article.entities = entities

            # Relevance gate: skip pages that have NO useful entity AND aren't long-form
            if not _is_post_worthy(entities, it.text or ""):
                article.status = "skipped"
                skipped_irrelevant += 1
                continue

            post = Post(
                article_id=article.id,
                title=data["title"],
                summary=data["summary"],
                body=data["body"],
                bullets=data["bullets"],
                hashtags=data["hashtags"],
                links=data["links"] or [it.url],
                image_url=None,
                cover_image_url=_pick_cover_image(it, data),
                variants=data["variants"],
                entities=entities,
                status="drafted",
            )
            db.add(post)
            article.status = "drafted"
            drafted += 1
        except Exception as e:
            logger.exception("AI failed on %s: %s", it.url, e)
    source.last_scraped_at = datetime.utcnow()
    db.commit()
    return {
        "source_id": source.id,
        "fetched": len(items),
        "new": new_count,
        "drafted": drafted,
        "skipped": skipped_irrelevant,
    }


def _is_post_worthy(entities: dict, text: str) -> bool:
    """A page is post-worthy when it has at least one structured fact OR is a
    substantive long-form article."""
    if not entities:
        return len(text) >= 800
    kind = entities.get("kind") or "article"
    if kind != "article":
        return True
    for key in ("deadline", "start_date", "apply_url", "amount", "organization", "location"):
        if entities.get(key):
            return True
    return len(text) >= 800


def run_pipeline_all() -> list[dict]:
    db = SessionLocal()
    try:
        sources = db.query(Source).filter_by(enabled=True).all()
        return [run_pipeline_for_source(db, s) for s in sources]
    finally:
        db.close()


def _pick_cover_image(item, data: dict) -> str:
    """Use the scraped page's og:image when present, otherwise build a topical
    Unsplash Source URL from entities/title so every post gets a hero image."""
    if getattr(item, "image_url", None):
        return item.image_url
    ent = (data.get("entities") or {})
    kind = (ent.get("kind") or "article").lower()
    # Build a short, descriptive query string for Unsplash.
    bits: list[str] = []
    if kind and kind != "article":
        bits.append(kind)
    org = ent.get("organization")
    if org:
        bits.append(str(org))
    loc = ent.get("location")
    if loc:
        bits.append(str(loc))
    title = (data.get("title") or item.title or "")[:80]
    if title:
        # Keep only alphanumeric words from the title
        import re as _re
        words = _re.findall(r"[A-Za-z]{3,}", title)
        bits.extend(words[:4])
    if not bits:
        bits = ["news"]
    # Unsplash Source picks a random topical image — no API key required.
    from urllib.parse import quote_plus
    query = quote_plus(",".join(b.lower() for b in bits if b)[:120])
    return f"https://source.unsplash.com/1200x630/?{query}"
