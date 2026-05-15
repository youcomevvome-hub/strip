"""API routers — auth, sources, articles, posts."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from . import schemas, social
from .auth import (create_token, get_current_principal, hash_password,
                   verify_password)
from .db import get_db
from .models import Article, Delivery, Post, Source, User
from .pipeline import run_pipeline_all, run_pipeline_for_source

PrincipalDep = Annotated[object, Depends(get_current_principal)]
DbDep = Annotated[Session, Depends(get_db)]

# ---------------- auth ----------------
auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


@auth_router.post("/register", response_model=schemas.UserOut)
def register(payload: schemas.UserCreate, db: DbDep):
    if db.query(User).first():
        # only allow first user via open register; further must be created by admin
        raise HTTPException(403, "Registration closed. Ask an admin.")
    user = User(email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@auth_router.post("/login", response_model=schemas.Token)
def login(payload: schemas.UserLogin, db: DbDep):
    user = db.query(User).filter_by(email=payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    return schemas.Token(access_token=create_token(user.email))


@auth_router.get("/me", response_model=schemas.UserOut)
def me(principal: PrincipalDep):
    if isinstance(principal, User):
        return principal
    raise HTTPException(400, "API-key principal has no user record")


# ---------------- sources ----------------
sources_router = APIRouter(prefix="/api/sources", tags=["sources"],
                            dependencies=[Depends(get_current_principal)])


@sources_router.get("", response_model=list[schemas.SourceOut])
def list_sources(db: DbDep):
    return db.query(Source).order_by(Source.id.desc()).all()


@sources_router.post("", response_model=schemas.SourceOut)
def create_source(payload: schemas.SourceCreate, db: DbDep):
    s = Source(
        name=payload.name,
        url=str(payload.url),
        rss_url=str(payload.rss_url) if payload.rss_url else None,
        scrape_mode=payload.scrape_mode,
        tags=payload.tags,
        enabled=payload.enabled,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@sources_router.patch("/{sid}", response_model=schemas.SourceOut)
def update_source(sid: int, payload: schemas.SourceUpdate, db: DbDep):
    s = db.get(Source, sid)
    if not s:
        raise HTTPException(404)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(s, k, str(v) if k in ("url", "rss_url") and v else v)
    db.commit()
    db.refresh(s)
    return s


@sources_router.delete("/{sid}")
def delete_source(sid: int, db: DbDep):
    s = db.get(Source, sid)
    if not s:
        raise HTTPException(404)
    db.delete(s)
    db.commit()
    return {"ok": True}


@sources_router.post("/{sid}/scrape")
def scrape_now(sid: int, db: DbDep, bg: BackgroundTasks):
    s = db.get(Source, sid)
    if not s:
        raise HTTPException(404)
    bg.add_task(run_pipeline_for_source, db, s)
    return {"ok": True, "queued": True}


@sources_router.post("/scrape-all")
def scrape_all(bg: BackgroundTasks):
    bg.add_task(run_pipeline_all)
    return {"ok": True, "queued": True}


# ---------------- articles ----------------
articles_router = APIRouter(prefix="/api/articles", tags=["articles"],
                             dependencies=[Depends(get_current_principal)])


@articles_router.get("", response_model=list[schemas.ArticleOut])
def list_articles(db: DbDep, status: str | None = Query(None),
                   limit: int = 50, offset: int = 0):
    q = db.query(Article)
    if status:
        q = q.filter_by(status=status)
    return q.order_by(Article.fetched_at.desc()).offset(offset).limit(limit).all()


@articles_router.delete("/{aid}")
def delete_article(aid: int, db: DbDep):
    a = db.get(Article, aid)
    if not a:
        raise HTTPException(404)
    db.delete(a)
    db.commit()
    return {"ok": True}


# ---------------- posts ----------------
posts_router = APIRouter(prefix="/api/posts", tags=["posts"],
                          dependencies=[Depends(get_current_principal)])


@posts_router.get("", response_model=list[schemas.PostOut])
def list_posts(db: DbDep, status: str | None = Query(None),
                limit: int = 50, offset: int = 0):
    q = db.query(Post)
    if status:
        q = q.filter_by(status=status)
    return q.order_by(Post.created_at.desc()).offset(offset).limit(limit).all()


@posts_router.get("/{pid}", response_model=schemas.PostOut)
def get_post(pid: int, db: DbDep):
    p = db.get(Post, pid)
    if not p:
        raise HTTPException(404)
    return p


@posts_router.patch("/{pid}", response_model=schemas.PostOut)
def update_post(pid: int, payload: schemas.PostUpdate, db: DbDep):
    p = db.get(Post, pid)
    if not p:
        raise HTTPException(404)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return p


@posts_router.post("/{pid}/approve", response_model=schemas.PostOut)
def approve(pid: int, db: DbDep):
    p = db.get(Post, pid)
    if not p:
        raise HTTPException(404)
    p.status = "approved"
    p.article.status = "approved"
    db.commit()
    db.refresh(p)
    return p


@posts_router.post("/{pid}/reject", response_model=schemas.PostOut)
def reject(pid: int, db: DbDep):
    p = db.get(Post, pid)
    if not p:
        raise HTTPException(404)
    p.status = "rejected"
    p.article.status = "rejected"
    db.commit()
    db.refresh(p)
    return p


@posts_router.post("/{pid}/publish", response_model=schemas.PostOut)
def publish(pid: int, payload: schemas.PublishRequest, db: DbDep):
    p = db.get(Post, pid)
    if not p:
        raise HTTPException(404)
    platforms = payload.platforms or social.PLATFORM_KEYS
    any_ok = False
    for platform in platforms:
        if platform not in social.PUBLISHERS:
            continue
        result = social.publish_to(platform, p)
        d = Delivery(
            post_id=p.id,
            platform=platform,
            status=result["status"],
            external_id=result.get("external_id"),
            external_url=result.get("external_url"),
            error=result.get("error"),
            delivered_at=datetime.utcnow() if result["status"] == "ok" else None,
        )
        db.add(d)
        if result["status"] == "ok":
            any_ok = True
    if any_ok:
        p.status = "published"
        p.article.status = "published"
    db.commit()
    db.refresh(p)
    return p


# ---------------- meta ----------------
meta_router = APIRouter(prefix="/api/meta", tags=["meta"])


@meta_router.get("/platforms")
def platforms():
    from .config import get_settings
    s = get_settings()
    configured = {
        "twitter": bool(s.TWITTER_API_KEY and s.TWITTER_ACCESS_TOKEN),
        "linkedin": bool(s.LINKEDIN_ACCESS_TOKEN),
        "facebook": bool(s.FACEBOOK_PAGE_TOKEN),
        "instagram": bool(s.INSTAGRAM_ACCESS_TOKEN),
        "whatsapp": bool(s.WHATSAPP_ACCESS_TOKEN),
        "reddit": bool(s.REDDIT_CLIENT_ID and s.REDDIT_USERNAME),
        "telegram": bool(s.TELEGRAM_BOT_TOKEN),
        "discord": bool(s.DISCORD_WEBHOOK_URL),
        "mastodon": bool(s.MASTODON_ACCESS_TOKEN),
    }
    return {"platforms": social.PLATFORM_KEYS, "configured": configured}
