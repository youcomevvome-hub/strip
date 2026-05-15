"""Server-rendered HTML UI — WhatsApp-styled, anonymous-first.

- Browsing, scraping, editing, and publishing to RSS works WITHOUT signing in.
- Sign-in is only required to publish to real social platforms (Twitter, LinkedIn, etc.).
- Session uses a signed cookie (itsdangerous), separate from the JSON API JWT.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse
import logging
import secrets
import threading
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from . import social
from .ai import sanitize_html
from .auth import hash_password, verify_password
from .config import get_settings
from .db import get_db, SessionLocal
from .models import Article, Delivery, Post, Source, User
from .pipeline import run_pipeline_all, run_pipeline_for_source

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
settings = get_settings()
signer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="strip-session")
SESSION_COOKIE = "strip_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

# ---------------- publish job tracking (in-memory) ----------------
# Maps post_id -> {"state": "running"|"done", "started": ts, "finished": ts|None,
#                   "platforms": {plat: {"status": "pending"|"running"|"ok"|"error",
#                                          "error": str|None, "url": str|None}}}
PUBLISH_JOBS: dict[int, dict] = {}
_JOB_LOCK = threading.Lock()

ui_router = APIRouter(include_in_schema=False)
logger = logging.getLogger(__name__)


def _favicon(url: str) -> str:
    try:
        host = urlparse(url).netloc or url
        return f"https://www.google.com/s2/favicons?domain={quote_plus(host)}&sz=64"
    except Exception:
        return ""


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url


_KIND_ICON = {
    "scholarship": "i-cap",
    "internship":  "i-briefcase",
    "job":         "i-briefcase",
    "grant":       "i-cash",
    "funding":     "i-cash",
    "contest":     "i-trophy",
    "event":       "i-calendar",
    "course":      "i-book",
    "product":     "i-megaphone",
    "article":     "i-doc",
}


def _kind_icon(kind: str) -> str:
    return _KIND_ICON.get((kind or "article").lower(), "i-doc")


templates.env.filters["favicon"] = _favicon
templates.env.filters["domain"] = _domain
templates.env.filters["kind_icon"] = _kind_icon


def _current_user(request: Request, db: Session) -> User | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        email = signer.loads(raw, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    return db.query(User).filter_by(email=email).first()


def _configured_platforms() -> dict:
    return {
        "twitter": bool(settings.TWITTER_API_KEY and settings.TWITTER_ACCESS_TOKEN),
        "linkedin": bool(settings.LINKEDIN_ACCESS_TOKEN),
        "facebook": bool(settings.FACEBOOK_PAGE_TOKEN),
        "instagram": bool(settings.INSTAGRAM_ACCESS_TOKEN),
        "whatsapp": bool(settings.WHATSAPP_ACCESS_TOKEN),
        "reddit": bool(settings.REDDIT_CLIENT_ID and settings.REDDIT_USERNAME),
        "telegram": bool(settings.TELEGRAM_BOT_TOKEN),
        "discord": bool(settings.DISCORD_WEBHOOK_URL),
        "mastodon": bool(settings.MASTODON_ACCESS_TOKEN),
        "rss": True,
    }


def _ctx(request: Request, db: Session | None = None, **extra) -> dict:
    user = _current_user(request, db) if db is not None else None
    pending_count = 0
    if db is not None:
        try:
            pending_count = db.query(Post).filter_by(status="drafted").count()
        except Exception:
            pending_count = 0
    return {
        "request": request,
        "user": user,
        "platforms": social.PLATFORM_KEYS,
        "configured": _configured_platforms(),
        "pending_count": pending_count,
        **extra,
    }


# ---------------- root ----------------
@ui_router.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/ui/dashboard", status_code=303)


# ---------------- auth (optional) ----------------
@ui_router.get("/ui/signin", response_class=HTMLResponse)
@ui_router.get("/ui/login", response_class=HTMLResponse)
def signin_form(request: Request, db: Session = Depends(get_db), next: str = "/ui/dashboard"):
    return templates.TemplateResponse("signin.html", _ctx(request, db, next=next))


@ui_router.post("/ui/signin")
@ui_router.post("/ui/login")
def signin_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/ui/dashboard"),
    db: Session = Depends(get_db),
):
    email_norm = email.lower().strip()
    user = db.query(User).filter_by(email=email_norm).first()

    # Auto-provision the very first user (no separate registration step).
    if not user and db.query(User).count() == 0:
        if len(password) < 6:
            return templates.TemplateResponse(
                "signin.html", _ctx(request, db, error="Password must be at least 6 characters.", next=next),
                status_code=400,
            )
        user = User(email=email_norm, password_hash=hash_password(password))
        db.add(user); db.commit()
    elif not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "signin.html", _ctx(request, db, error="Invalid email or password.", next=next),
            status_code=401,
        )

    token = signer.dumps(user.email)
    resp = RedirectResponse(next or "/ui/dashboard", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE,
                    httponly=True, samesite="lax")
    return resp


@ui_router.get("/ui/register", response_class=HTMLResponse)
def register_redirect():
    return RedirectResponse("/ui/signin", status_code=303)


@ui_router.get("/ui/logout")
def logout():
    resp = RedirectResponse("/ui/dashboard", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ---------------- dashboard ----------------
@ui_router.get("/ui/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    stats = {
        "sources": db.query(Source).count(),
        "pending": db.query(Post).filter_by(status="drafted").count(),
        "published": db.query(Post).filter_by(status="published").count(),
        "articles": db.query(Article).count(),
    }
    recent_sources = db.query(Source).order_by(Source.id.desc()).limit(6).all()
    recent_posts = db.query(Post).order_by(Post.created_at.desc()).limit(6).all()
    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(request, db, stats=stats, recent_sources=recent_sources, recent_posts=recent_posts),
    )


# ---------------- sources ----------------
@ui_router.get("/ui/sources", response_class=HTMLResponse)
def sources_page(request: Request, db: Session = Depends(get_db)):
    sources = db.query(Source).order_by(Source.id.desc()).all()
    # Build groups for the bulk-scrape buttons
    groups: dict[str, list[Source]] = {}
    for s in sources:
        key = (s.group_name or "").strip()
        if key:
            groups.setdefault(key, []).append(s)
    return templates.TemplateResponse(
        "sources.html", _ctx(request, db, sources=sources, groups=groups),
    )


@ui_router.post("/ui/sources/add")
def sources_add(
    name: str = Form(...),
    url: str = Form(...),
    tags: str = Form(""),
    keywords: str = Form(""),
    group_name: str = Form(""),
    db: Session = Depends(get_db),
):
    s = Source(
        name=name.strip(), url=url.strip(), rss_url=None,
        scrape_mode="auto",
        tags=tags.strip(),
        keywords=keywords.strip(),
        group_name=group_name.strip(),
        enabled=True,
    )
    db.add(s); db.commit()
    return RedirectResponse("/ui/sources", status_code=303)


@ui_router.post("/ui/sources/bulk-add")
def sources_bulk_add(
    urls: str = Form(...),
    group_name: str = Form(""),
    keywords: str = Form(""),
    db: Session = Depends(get_db),
):
    """Paste several URLs (one per line, or "Name | https://..." per line) to add
    them all to a single group, sharing the same keyword filter."""
    added = 0
    for raw in (urls or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "|" in line:
            name, _, link = line.partition("|")
            name, link = name.strip(), link.strip()
        else:
            link = line
            name = urlparse(link).netloc.replace("www.", "") or link
        if not link.startswith("http"):
            continue
        if db.query(Source).filter_by(url=link).first():
            continue
        db.add(Source(
            name=name, url=link, rss_url=None, scrape_mode="auto",
            tags="", keywords=keywords.strip(), group_name=group_name.strip(),
            enabled=True,
        ))
        added += 1
    db.commit()
    return RedirectResponse("/ui/sources", status_code=303)


@ui_router.post("/ui/sources/{sid}/update")
async def sources_update(sid: int, request: Request, db: Session = Depends(get_db)):
    s = db.get(Source, sid)
    if not s:
        raise HTTPException(404)
    form = await request.form()
    s.name = form.get("name", s.name)
    s.keywords = form.get("keywords", s.keywords or "")
    s.group_name = form.get("group_name", s.group_name or "")
    db.commit()
    return RedirectResponse("/ui/sources", status_code=303)


# ---------------- scrape job tracking ----------------
SCRAPE_JOB: dict = {"state": "idle", "started": None, "finished": None,
                    "total": 0, "done": 0, "current": None, "results": []}
_SCRAPE_LOCK = threading.Lock()


def _scrape_worker(source_ids: list[int] | None) -> None:
    """Background scrape worker. If source_ids is None, scrape all enabled."""
    db = SessionLocal()
    try:
        if source_ids is None:
            sources = db.query(Source).filter_by(enabled=True).all()
        else:
            sources = [s for s in (db.get(Source, sid) for sid in source_ids) if s is not None]
        with _SCRAPE_LOCK:
            SCRAPE_JOB.update(total=len(sources), done=0, results=[], state="running",
                              started=time.time(), finished=None, current=None)
        for s in sources:
            with _SCRAPE_LOCK:
                SCRAPE_JOB["current"] = s.name
            try:
                r = run_pipeline_for_source(db, s)
            except Exception as e:
                logger.exception("scrape failed for %s: %s", s.name, e)
                r = {"source_id": s.id, "fetched": 0, "new": 0, "drafted": 0, "error": str(e)}
            with _SCRAPE_LOCK:
                SCRAPE_JOB["done"] += 1
                SCRAPE_JOB["results"].append({"source": s.name, **r})
        with _SCRAPE_LOCK:
            SCRAPE_JOB["state"] = "done"
            SCRAPE_JOB["finished"] = time.time()
            SCRAPE_JOB["current"] = None
    finally:
        db.close()


def _start_scrape(source_ids: list[int] | None) -> bool:
    """Start a scrape job if none is currently running. Returns True if started."""
    with _SCRAPE_LOCK:
        if SCRAPE_JOB["state"] == "running":
            return False
        SCRAPE_JOB.update(state="starting", total=0, done=0, results=[],
                          started=time.time(), finished=None, current=None)
    threading.Thread(target=_scrape_worker, args=(source_ids,), daemon=True).start()
    return True


@ui_router.get("/ui/scrape/status")
def scrape_status():
    with _SCRAPE_LOCK:
        return JSONResponse({
            "state": SCRAPE_JOB["state"],
            "started": SCRAPE_JOB["started"],
            "finished": SCRAPE_JOB["finished"],
            "total": SCRAPE_JOB["total"],
            "done": SCRAPE_JOB["done"],
            "current": SCRAPE_JOB["current"],
            "results": list(SCRAPE_JOB["results"]),
        })


@ui_router.post("/ui/sources/group/{group}/scrape")
def sources_group_scrape(group: str, db: Session = Depends(get_db)):
    sources = db.query(Source).filter_by(group_name=group, enabled=True).all()
    _start_scrape([s.id for s in sources])
    return RedirectResponse("/ui/queue?scraping=1", status_code=303)


@ui_router.post("/ui/sources/{sid}/delete")
def sources_delete(sid: int, db: Session = Depends(get_db)):
    s = db.get(Source, sid)
    if s:
        db.delete(s); db.commit()
    return RedirectResponse("/ui/sources", status_code=303)


@ui_router.post("/ui/sources/{sid}/toggle")
def sources_toggle(sid: int, db: Session = Depends(get_db)):
    s = db.get(Source, sid)
    if s:
        s.enabled = not s.enabled
        db.commit()
    return RedirectResponse("/ui/sources", status_code=303)


@ui_router.post("/ui/sources/{sid}/scrape")
def sources_scrape(sid: int, db: Session = Depends(get_db)):
    s = db.get(Source, sid)
    if s:
        _start_scrape([s.id])
    return RedirectResponse("/ui/queue?scraping=1", status_code=303)


@ui_router.post("/ui/sources/scrape-all")
def sources_scrape_all():
    _start_scrape(None)
    return RedirectResponse("/ui/queue?scraping=1", status_code=303)


# ---------------- queue ----------------
@ui_router.get("/ui/queue", response_class=HTMLResponse)
def queue_page(request: Request, db: Session = Depends(get_db)):
    posts = db.query(Post).filter_by(status="drafted").order_by(Post.created_at.desc()).all()
    # Snapshot active publish jobs so the UI can resume the progress modal.
    with _JOB_LOCK:
        active_jobs = {
            pid: {
                "state": j["state"],
                "platforms": {k: dict(v) for k, v in j["platforms"].items()},
            }
            for pid, j in PUBLISH_JOBS.items()
            if j["state"] == "running" or (j.get("finished") and time.time() - j["finished"] < 30)
        }
    return templates.TemplateResponse(
        "queue.html", _ctx(request, db, posts=posts, active_jobs=active_jobs),
    )


@ui_router.post("/ui/posts/{pid}/save")
async def post_save(pid: int, request: Request, db: Session = Depends(get_db)):
    p = db.get(Post, pid)
    if not p:
        raise HTTPException(404)
    form = await request.form()
    p.title = form.get("title", p.title)
    p.summary = form.get("summary", p.summary)
    p.body = sanitize_html(form.get("body", p.body) or "")
    p.bullets = [b.strip() for b in (form.get("bullets", "") or "").split("\n") if b.strip()]
    p.hashtags = [h.lstrip("#").lower() for h in (form.get("hashtags", "") or "").split() if h.strip()]
    new_cover = (form.get("cover_image_url") or "").strip()
    if new_cover:
        p.cover_image_url = new_cover[:1000]
    elif "cover_image_url" in form:
        p.cover_image_url = None
    variants = dict(p.variants or {})
    for key in ["twitter", "linkedin", "facebook", "instagram", "whatsapp",
                "telegram", "discord", "mastodon", "reddit_title", "reddit_body"]:
        if f"variants.{key}" in form:
            variants[key] = form.get(f"variants.{key}", "")
    p.variants = variants
    db.commit()
    return RedirectResponse("/ui/queue", status_code=303)


@ui_router.post("/ui/posts/{pid}/reject")
def post_reject(pid: int, db: Session = Depends(get_db)):
    p = db.get(Post, pid)
    if p:
        p.status = "rejected"
        if p.article:
            p.article.status = "rejected"
        db.commit()
    return RedirectResponse("/ui/queue", status_code=303)


@ui_router.post("/ui/posts/{pid}/publish")
async def post_publish(pid: int, request: Request, db: Session = Depends(get_db)):
    p = db.get(Post, pid)
    if not p:
        raise HTTPException(404)
    form = await request.form()
    platforms = form.getlist("platforms") or ["rss"]
    # Drop unknown platforms early
    platforms = [pl for pl in platforms if pl in social.PUBLISHERS]
    if not platforms:
        platforms = ["rss"]

    # Anyone can publish to the local RSS feed; real social platforms require sign-in.
    needs_auth = any(plat != "rss" for plat in platforms)
    if needs_auth and _current_user(request, db) is None:
        if _wants_json(request):
            return JSONResponse({"error": "auth_required", "next": "/ui/signin?next=/ui/queue"}, status_code=401)
        return RedirectResponse("/ui/signin?next=/ui/queue", status_code=303)

    # If a job is already running for this post, just return its state.
    with _JOB_LOCK:
        existing = PUBLISH_JOBS.get(pid)
        if existing and existing.get("state") == "running":
            if _wants_json(request):
                return JSONResponse(existing)
            return RedirectResponse("/ui/queue", status_code=303)

        job = {
            "post_id": pid,
            "state": "running",
            "started": time.time(),
            "finished": None,
            "platforms": {plat: {"status": "pending", "error": None, "url": None} for plat in platforms},
        }
        PUBLISH_JOBS[pid] = job

    # Kick off worker thread — it must use its own DB session.
    threading.Thread(target=_publish_worker, args=(pid, list(platforms)), daemon=True).start()

    if _wants_json(request):
        return JSONResponse(job)
    return RedirectResponse(f"/ui/queue?publishing={pid}", status_code=303)


def _wants_json(request: Request) -> bool:
    """Return True if the client is doing an AJAX/fetch request."""
    accept = (request.headers.get("accept") or "").lower()
    xrw = (request.headers.get("x-requested-with") or "").lower()
    return "application/json" in accept or xrw == "fetch"


def _publish_worker(pid: int, platforms: list[str]) -> None:
    """Run each platform publisher sequentially, updating PUBLISH_JOBS as we go.
    Each platform that succeeds writes a Delivery row; first success marks the
    post as published. Continues through the list even if some platforms fail."""
    db = SessionLocal()
    try:
        p = db.get(Post, pid)
        if p is None:
            with _JOB_LOCK:
                PUBLISH_JOBS[pid]["state"] = "done"
                PUBLISH_JOBS[pid]["finished"] = time.time()
            return
        any_ok = False
        for plat in platforms:
            with _JOB_LOCK:
                PUBLISH_JOBS[pid]["platforms"][plat]["status"] = "running"
            try:
                result = social.publish_to(plat, p)
            except Exception as e:  # noqa - publisher should not crash worker
                logger.exception("publisher %s crashed for post %s", plat, pid)
                result = {"status": "error", "error": str(e)}
            d = Delivery(
                post_id=p.id, platform=plat,
                status=result["status"],
                external_id=result.get("external_id"),
                external_url=result.get("external_url"),
                error=result.get("error"),
                delivered_at=datetime.utcnow() if result["status"] == "ok" else None,
            )
            db.add(d)
            db.commit()
            with _JOB_LOCK:
                PUBLISH_JOBS[pid]["platforms"][plat] = {
                    "status": result["status"],
                    "error": result.get("error"),
                    "url": result.get("external_url"),
                }
            if result["status"] == "ok":
                any_ok = True
                if p.status != "published":
                    p.status = "published"
                    if p.article:
                        p.article.status = "published"
                    db.commit()
        with _JOB_LOCK:
            PUBLISH_JOBS[pid]["state"] = "done"
            PUBLISH_JOBS[pid]["finished"] = time.time()
            PUBLISH_JOBS[pid]["any_ok"] = any_ok
    finally:
        db.close()


@ui_router.get("/ui/posts/{pid}/publish/status")
def post_publish_status(pid: int):
    """JSON: progress of a publish job. Returns {state: 'idle'} when no job exists."""
    with _JOB_LOCK:
        job = PUBLISH_JOBS.get(pid)
        if not job:
            return JSONResponse({"post_id": pid, "state": "idle", "platforms": {}})
        # Return a shallow copy so the client sees a consistent snapshot.
        return JSONResponse({
            "post_id": pid,
            "state": job["state"],
            "started": job.get("started"),
            "finished": job.get("finished"),
            "any_ok": job.get("any_ok", False),
            "platforms": {k: dict(v) for k, v in job["platforms"].items()},
        })


# ---------------- posts history ----------------
@ui_router.get("/ui/posts", response_class=HTMLResponse)
def posts_page(request: Request, db: Session = Depends(get_db)):
    posts = (
        db.query(Post)
        .filter(Post.status == "published")
        .order_by(Post.created_at.desc())
        .limit(200)
        .all()
    )
    return templates.TemplateResponse(
        "posts.html", _ctx(request, db, posts=posts),
    )


# ---------------- blog (view / like / share / delete) ----------------
@ui_router.get("/ui/blog", response_class=HTMLResponse)
def blog_page(request: Request, db: Session = Depends(get_db)):
    posts = (
        db.query(Post)
        .filter(Post.status.in_(["drafted", "published"]))
        .order_by(Post.created_at.desc())
        .limit(200)
        .all()
    )
    return templates.TemplateResponse("blog.html", _ctx(request, db, posts=posts))


@ui_router.post("/ui/posts/{pid}/like")
def post_like(pid: int, request: Request, db: Session = Depends(get_db)):
    p = db.get(Post, pid)
    if p:
        p.likes = (p.likes or 0) + 1
        db.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "likes": p.likes if p else 0})
    referer = request.headers.get("referer") or "/ui/blog"
    return RedirectResponse(referer, status_code=303)


@ui_router.post("/ui/posts/{pid}/delete")
def post_delete(pid: int, request: Request, db: Session = Depends(get_db)):
    p = db.get(Post, pid)
    if p:
        db.delete(p)
        db.commit()
    referer = request.headers.get("referer") or "/ui/blog"
    return RedirectResponse(referer, status_code=303)


@ui_router.post("/ui/posts/{pid}/telegram")
def post_publish_telegram(pid: int, request: Request, db: Session = Depends(get_db)):
    """Publish a single post to all configured Telegram chats."""
    from . import social
    p = db.get(Post, pid)
    if not p:
        raise HTTPException(404)
    try:
        result = social.post_telegram(p, (p.variants or {}).get("telegram") or p.summary or p.title)
    except Exception as e:
        logger.exception("telegram publish failed for post %s: %s", pid, e)
        result = {"status": "error", "error": str(e)}
    d = Delivery(
        post_id=p.id, platform="telegram",
        status=result.get("status", "error"),
        external_id=result.get("external_id"),
        external_url=result.get("external_url"),
        error=result.get("error"),
        delivered_at=datetime.utcnow() if result.get("status") == "ok" else None,
    )
    db.add(d)
    if result.get("status") == "ok":
        p.status = "published"
        if p.article:
            p.article.status = "published"
    db.commit()
    referer = request.headers.get("referer") or "/ui/posts"
    return RedirectResponse(referer, status_code=303)


# ---------------- public post page (used by share intents) ----------------
@ui_router.get("/p/{pid}", response_class=HTMLResponse)
def public_post(pid: int, request: Request, db: Session = Depends(get_db)):
    p = db.get(Post, pid)
    if not p:
        raise HTTPException(404)
    p.views = (p.views or 0) + 1
    db.commit()
    share_url = str(request.url).split("?")[0]
    return templates.TemplateResponse(
        "public_post.html",
        {"request": request, "p": p, "share_url": share_url},
    )


# ---------------- image upload (Quill editor) ----------------
UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
_ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


@ui_router.post("/ui/upload-image")
async def upload_image(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower() or ".png"
    if ext not in _ALLOWED_IMAGE_EXT:
        return JSONResponse({"error": "unsupported file type"}, status_code=400)
    name = f"{secrets.token_hex(8)}{ext}"
    dest = UPLOAD_DIR / name
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        return JSONResponse({"error": "file too large (max 8 MB)"}, status_code=400)
    dest.write_bytes(data)
    return {"url": f"/uploads/{name}"}


# ---------------- settings ----------------
# Groups of credential fields per platform — driven by the same env var names that
# config.Settings exposes so apply_db_overrides() can pick them up.
PLATFORM_CRED_FIELDS = {
    "twitter":   ["TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET"],
    "linkedin":  ["LINKEDIN_ACCESS_TOKEN", "LINKEDIN_AUTHOR_URN"],
    "facebook":  ["FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_TOKEN"],
    "instagram": ["INSTAGRAM_USER_ID", "INSTAGRAM_ACCESS_TOKEN"],
    "whatsapp":  ["WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_RECIPIENTS"],
    "reddit":    ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD", "REDDIT_DEFAULT_SUBREDDIT"],
    "telegram":  ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_IDS"],
    "discord":   ["DISCORD_WEBHOOK_URL"],
    "mastodon":  ["MASTODON_BASE_URL", "MASTODON_ACCESS_TOKEN"],
    "ai":        ["OLLAMA_BASE_URL", "OLLAMA_MODEL"],
}
_SECRET_KEYS = {
    "TWITTER_API_SECRET", "TWITTER_ACCESS_SECRET", "LINKEDIN_ACCESS_TOKEN",
    "FACEBOOK_PAGE_TOKEN", "INSTAGRAM_ACCESS_TOKEN", "WHATSAPP_ACCESS_TOKEN",
    "REDDIT_CLIENT_SECRET", "REDDIT_PASSWORD", "TELEGRAM_BOT_TOKEN",
    "DISCORD_WEBHOOK_URL", "MASTODON_ACCESS_TOKEN",
}


def _settings_ctx(request: Request, db: Session, **extra) -> dict:
    """Build the rich Settings template context."""
    from . import tg_bot
    from .models import Setting, TelegramSubscriber
    s = settings  # already overlaid with DB values on startup / after save

    def field(key: str) -> dict:
        val = getattr(s, key, "") or ""
        is_secret = key in _SECRET_KEYS
        return {
            "name": key,
            "value": val if not is_secret else ("•" * 8 if val else ""),
            "is_secret": is_secret,
            "has_value": bool(val),
        }

    platform_cards = []
    for plat, keys in PLATFORM_CRED_FIELDS.items():
        platform_cards.append({
            "key": plat,
            "fields": [field(k) for k in keys],
            "configured": _configured_platforms().get(plat, False) if plat in _configured_platforms() else any(getattr(s, k, "") for k in keys),
        })

    subscribers = db.query(TelegramSubscriber).order_by(TelegramSubscriber.created_at.desc()).all()
    bot_username = tg_bot.get_bot_username() if s.TELEGRAM_BOT_TOKEN else None

    return _ctx(
        request, db,
        api_key=settings.API_KEY,
        platform_cards=platform_cards,
        subscribers=subscribers,
        bot_username=bot_username,
        **extra,
    )


@ui_router.get("/ui/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("settings.html", _settings_ctx(request, db))


@ui_router.post("/ui/settings/save/{platform}")
async def settings_save(
    platform: str, request: Request, db: Session = Depends(get_db),
):
    from .config import apply_db_overrides
    from .models import Setting
    if platform not in PLATFORM_CRED_FIELDS:
        raise HTTPException(404)
    if _current_user(request, db) is None:
        return RedirectResponse("/ui/signin?next=/ui/settings", status_code=303)
    form = await request.form()
    for key in PLATFORM_CRED_FIELDS[platform]:
        val = (form.get(key) or "").strip()
        # Empty submission on a secret field means "keep existing".
        if not val and key in _SECRET_KEYS and getattr(settings, key, ""):
            continue
        row = db.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=val)
            db.add(row)
        else:
            row.value = val
    db.commit()
    apply_db_overrides()
    return RedirectResponse("/ui/settings", status_code=303)


@ui_router.post("/ui/account/profile")
async def account_update_profile(request: Request, db: Session = Depends(get_db)):
    """Change display name and / or email."""
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/ui/signin?next=/ui/settings", status_code=303)
    form = await request.form()
    new_email = (form.get("email") or "").strip().lower()
    new_name = (form.get("name") or "").strip()[:120]
    error = None
    if new_email and new_email != user.email:
        if db.query(User).filter_by(email=new_email).first():
            error = "That email is already in use."
        else:
            user.email = new_email
    user.name = new_name
    db.commit()
    # Refresh session cookie since email may have changed.
    token = signer.dumps(user.email)
    resp = RedirectResponse("/ui/settings", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE,
                    httponly=True, samesite="lax")
    if error:
        # Stash in querystring (lightweight, no flash framework).
        resp = RedirectResponse(f"/ui/settings?err={quote_plus(error)}", status_code=303)
    return resp


@ui_router.post("/ui/account/password")
async def account_change_password(request: Request, db: Session = Depends(get_db)):
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/ui/signin?next=/ui/settings", status_code=303)
    form = await request.form()
    current = form.get("current") or ""
    new = form.get("new") or ""
    confirm = form.get("confirm") or ""
    if not verify_password(current, user.password_hash):
        return RedirectResponse(f"/ui/settings?err={quote_plus('Current password incorrect.')}",
                                status_code=303)
    if len(new) < 6:
        return RedirectResponse(f"/ui/settings?err={quote_plus('New password must be at least 6 characters.')}",
                                status_code=303)
    if new != confirm:
        return RedirectResponse(f"/ui/settings?err={quote_plus('Passwords do not match.')}",
                                status_code=303)
    user.password_hash = hash_password(new)
    db.commit()
    return RedirectResponse(f"/ui/settings?ok={quote_plus('Password updated.')}", status_code=303)


@ui_router.post("/ui/telegram/test")
def telegram_test(request: Request, db: Session = Depends(get_db)):
    from . import social
    result = social.telegram_send_test("Strip is connected. You will receive published posts here.")
    return templates.TemplateResponse(
        "settings.html",
        _settings_ctx(request, db, telegram_test=result),
    )


@ui_router.post("/ui/telegram/subscriber/{sid}/toggle")
def telegram_sub_toggle(sid: int, db: Session = Depends(get_db)):
    from .models import TelegramSubscriber
    sub = db.get(TelegramSubscriber, sid)
    if sub:
        sub.enabled = not sub.enabled
        db.commit()
    return RedirectResponse("/ui/settings", status_code=303)


@ui_router.post("/ui/telegram/subscriber/{sid}/delete")
def telegram_sub_delete(sid: int, db: Session = Depends(get_db)):
    from .models import TelegramSubscriber
    sub = db.get(TelegramSubscriber, sid)
    if sub:
        db.delete(sub)
        db.commit()
    return RedirectResponse("/ui/settings", status_code=303)


# ---------------- RSS feed ----------------
@ui_router.get("/rss")
def rss_feed(db: Session = Depends(get_db)):
    from datetime import timezone
    from feedgen.feed import FeedGenerator
    fg = FeedGenerator()
    fg.id("http://localhost:8000/rss")
    fg.title("Strip — Published")
    fg.link(href="http://localhost:8000/rss", rel="self")
    fg.description("Posts approved and published via Strip.")
    fg.language("en")
    posts = (
        db.query(Post).filter_by(status="published")
        .order_by(Post.updated_at.desc()).limit(50).all()
    )
    for p in posts:
        fe = fg.add_entry()
        fe.id(f"strip-post-{p.id}")
        fe.title(p.title)
        link = (p.links or [None])[0]
        if link:
            fe.link(href=link)
        fe.description(p.summary or "")
        fe.content(p.body or p.summary or "")
        dt = p.updated_at or datetime.utcnow()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        fe.pubDate(dt)
    return Response(content=fg.rss_str(pretty=True), media_type="application/rss+xml")


# ---------------- SEO: sitemap + robots.txt ----------------
@ui_router.get("/robots.txt")
def robots_txt(request: Request):
    base = str(request.base_url).rstrip("/")
    body = f"User-agent: *\nAllow: /\nDisallow: /ui/\nDisallow: /api/\nSitemap: {base}/sitemap.xml\n"
    return Response(body, media_type="text/plain")


@ui_router.get("/sitemap.xml")
def sitemap_xml(request: Request, db: Session = Depends(get_db)):
    """XML sitemap of every published post for search engines."""
    base = str(request.base_url).rstrip("/")
    posts = (
        db.query(Post).filter(Post.status.in_(["drafted", "published"]))
        .order_by(Post.updated_at.desc()).limit(5000).all()
    )
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    lines.append(f"<url><loc>{base}/ui/blog</loc><changefreq>daily</changefreq><priority>0.8</priority></url>")
    for p in posts:
        lastmod = (p.updated_at or p.created_at or datetime.utcnow()).strftime("%Y-%m-%d")
        lines.append(
            f"<url><loc>{base}/p/{p.id}</loc>"
            f"<lastmod>{lastmod}</lastmod>"
            f"<changefreq>weekly</changefreq><priority>0.7</priority></url>"
        )
    lines.append("</urlset>")
    return Response("\n".join(lines), media_type="application/xml")
