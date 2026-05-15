"""Daily pipeline: scrape -> AI structure -> create draft posts."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import quote_plus

from sqlalchemy.orm import Session

from . import ai, scraper
from .db import SessionLocal
from .models import Article, Post, Source

logger = logging.getLogger(__name__)


def run_pipeline_for_source(db: Session, source: Source) -> dict:
    logger.info("scraping %s (%s)", source.name, source.url)
    raw_items = scraper.scrape_source(
        url=source.url, rss_url=source.rss_url, mode=source.scrape_mode,
    )
    items = list(raw_items)
    # Per-source keyword filter: only keep items whose title + text matches at
    # least one of the source's comma-separated keywords (when configured).
    kw_raw = (source.keywords or "").strip()
    keywords = [k.strip().lower() for k in kw_raw.split(",") if k.strip()] if kw_raw else []
    matched_by_keyword = len(items)
    keyword_fallback_used = False
    if keywords:
        filtered = []
        for it in items:
            hay = ((it.title or "") + " " + (it.text or "")).lower()
            if any(k in hay for k in keywords):
                filtered.append(it)
        matched_by_keyword = len(filtered)
        logger.info("keyword filter on %s: %d -> %d items", source.name, len(items), len(filtered))
        # If keywords are too strict for this run, keep a small fallback slice
        # so scraping still produces reviewable drafts instead of hard-zero.
        if not filtered and items:
            keyword_fallback_used = True
            items = items[: min(5, len(items))]
        else:
            items = filtered
    new_count = 0
    drafted = 0
    redrafted = 0
    skipped_irrelevant = 0
    for it in items:
        existing = db.query(Article).filter_by(url=it.url).first()
        if existing:
            # If this article already has a live post (drafted/scheduled/
            # published), keep skipping. Otherwise re-draft so the user can
            # review fresh content even when all old posts were rejected.
            live_post = (
                db.query(Post)
                .filter(Post.article_id == existing.id, Post.status.in_(("drafted", "scheduled", "published")))
                .first()
            )
            if live_post:
                continue
            article = existing
            # refresh the text in case scraper improvements yield cleaner content
            article.raw_text = it.text or article.raw_text
            article.title = (it.title or article.title)[:500]
            is_new = False
        else:
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
            is_new = True
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
            if not is_new:
                redrafted += 1
        except Exception as e:
            logger.exception("AI failed on %s: %s", it.url, e)
    source.last_scraped_at = datetime.utcnow()
    db.commit()
    return {
        "source_id": source.id,
        "fetched": len(raw_items),
        "matched": matched_by_keyword,
        "new": new_count,
        "drafted": drafted,
        "redrafted": redrafted,
        "skipped": skipped_irrelevant,
        "keyword_fallback": keyword_fallback_used,
    }


def _is_text_clean(text: str) -> bool:
    """Reject pages whose text looks like binary/garbled/markup soup."""
    if not text:
        return False
    sample = text[:4000]
    if not sample.strip():
        return False
    printable = sum(1 for c in sample if c.isprintable() or c in "\n\t\r")
    if printable / len(sample) < 0.95:
        return False
    alnum = sum(1 for c in sample if c.isalnum())
    if alnum / len(sample) < 0.45:
        return False
    words = [w for w in re.findall(r"[A-Za-z]+", sample) if w]
    if len(words) < 30:
        return False
    avg = sum(len(w) for w in words) / len(words)
    # Real prose averages roughly 4-6 chars per word; way outside that range
    # is almost always token salad or stripped markup.
    if avg < 2.5 or avg > 12:
        return False
    return True


def _is_post_worthy(entities: dict, text: str) -> bool:
    """A page is post-worthy when it reads like real prose AND has at least
    one structured fact OR is a substantive long-form article."""
    if not _is_text_clean(text):
        return False
    if not entities:
        return len(text) >= 800
    apply_url = entities.get("apply_url") or ""
    # Reject drafts whose only "apply" link is a social share button.
    if apply_url and re.search(
        r"(?:twitter\.com/intent|x\.com/intent|facebook\.com/(?:sharer|share)|"
        r"linkedin\.com/(?:sharing|share-offsite)|t\.me/share|wa\.me/|reddit\.com/submit|"
        r"pinterest\.com/pin/create)",
        apply_url, re.IGNORECASE,
    ):
        entities.pop("apply_url", None)
        apply_url = ""
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
        words = re.findall(r"[A-Za-z]{3,}", title)
        bits.extend(words[:4])
    if not bits:
        bits = ["news"]
    # Unsplash Source picks a random topical image — no API key required.
    query = quote_plus(",".join(b.lower() for b in bits if b)[:120])
    return f"https://source.unsplash.com/1200x630/?{query}"
