"""Long-polling Telegram bot worker.

Runs in a background thread. Handles:
- /start <token>  — link the Telegram chat to a Strip account or subscribe to broadcasts
- /start          — show welcome + subscribe
- /latest         — last 5 published posts
- /search <q>     — search published posts by title/summary
- /help           — list commands

The bot uses the same TELEGRAM_BOT_TOKEN as the publisher. New subscribers are
stored in the `telegram_subscribers` table; the publisher automatically delivers
new posts to every enabled subscriber.
"""
from __future__ import annotations

import html
import logging
import threading
import time

import httpx

from .config import get_settings
from .db import SessionLocal
from .models import Post, TelegramSubscriber

logger = logging.getLogger(__name__)
_thread: threading.Thread | None = None
_stop = threading.Event()


def start() -> None:
    """Start the long-poll worker in a background thread (idempotent)."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="tg-bot", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()


def _run() -> None:
    offset: int | None = None
    while not _stop.is_set():
        s = get_settings()
        token = (s.TELEGRAM_BOT_TOKEN or "").strip()
        if not token:
            # No token configured — sleep and retry; admin may set it in /ui/settings.
            time.sleep(15)
            continue
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = httpx.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params=params,
                timeout=35,
            )
            if r.status_code != 200:
                logger.debug("telegram getUpdates %s", r.status_code)
                time.sleep(5)
                continue
            data = r.json()
            if not data.get("ok"):
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    _handle_update(upd, token)
                except Exception as e:
                    logger.exception("tg update handler failed: %s", e)
        except httpx.ReadTimeout:
            continue
        except Exception as e:
            logger.warning("tg poll error: %s", e)
            time.sleep(5)


def _handle_update(upd: dict, token: str) -> None:
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return
    chat_id = str(chat_id)

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        link_token = parts[1].strip() if len(parts) > 1 else None
        _cmd_start(token, chat_id, chat, link_token)
    elif text.startswith("/latest"):
        _cmd_latest(token, chat_id)
    elif text.startswith("/search"):
        q = text[len("/search"):].strip()
        _cmd_search(token, chat_id, q)
    elif text.startswith("/help") or text.startswith("/menu"):
        _cmd_help(token, chat_id)
    elif text.startswith("/stop"):
        _cmd_stop(token, chat_id)
    else:
        # Free-text query — search.
        _cmd_search(token, chat_id, text)


def _cmd_start(token: str, chat_id: str, chat: dict, link_token: str | None) -> None:
    db = SessionLocal()
    try:
        sub = db.query(TelegramSubscriber).filter_by(chat_id=chat_id).first()
        if not sub:
            sub = TelegramSubscriber(
                chat_id=chat_id,
                username=chat.get("username"),
                first_name=chat.get("first_name") or chat.get("title"),
                link_token=link_token,
                enabled=True,
            )
            db.add(sub)
        else:
            sub.enabled = True
            if link_token:
                sub.link_token = link_token
        # If link_token matches a user, attach it.
        if link_token:
            from .models import User
            # Convention: token = "u<user_id>-<random>"
            if link_token.startswith("u"):
                try:
                    uid = int(link_token[1:].split("-", 1)[0])
                    if db.get(User, uid):
                        sub.user_id = uid
                except Exception:
                    pass
        db.commit()
        first = chat.get("first_name") or "there"
        _send(
            token, chat_id,
            f"\u2705 Hi {html.escape(first)}! You're now subscribed to Strip. "
            "You'll receive new opportunities here automatically.\n\n"
            "Commands:\n"
            "<b>/latest</b> \u2014 last 5 posts\n"
            "<b>/search</b> &lt;keyword&gt; \u2014 search posts\n"
            "<b>/stop</b> \u2014 pause notifications\n"
            "<b>/help</b> \u2014 show this menu",
        )
    finally:
        db.close()


def _cmd_stop(token: str, chat_id: str) -> None:
    db = SessionLocal()
    try:
        sub = db.query(TelegramSubscriber).filter_by(chat_id=chat_id).first()
        if sub:
            sub.enabled = False
            db.commit()
        _send(token, chat_id, "\U0001f515 Notifications paused. Send /start to resume.")
    finally:
        db.close()


def _cmd_help(token: str, chat_id: str) -> None:
    _send(
        token, chat_id,
        "<b>Strip bot</b>\n\n"
        "<b>/latest</b> \u2014 last 5 posts\n"
        "<b>/search</b> &lt;keyword&gt; \u2014 search posts\n"
        "<b>/stop</b> \u2014 pause notifications\n"
        "<b>/start</b> \u2014 resume notifications",
    )


def _cmd_latest(token: str, chat_id: str) -> None:
    db = SessionLocal()
    try:
        posts = (
            db.query(Post)
            .filter(Post.status == "published")
            .order_by(Post.created_at.desc())
            .limit(5)
            .all()
        )
        if not posts:
            _send(token, chat_id, "No published posts yet.")
            return
        for p in posts:
            _send_post_card(token, chat_id, p)
    finally:
        db.close()


def _cmd_search(token: str, chat_id: str, q: str) -> None:
    q = (q or "").strip()
    if not q:
        _send(token, chat_id, "Send <b>/search</b> &lt;keyword&gt; to find posts.")
        return
    db = SessionLocal()
    try:
        like = f"%{q}%"
        posts = (
            db.query(Post)
            .filter(Post.status == "published")
            .filter((Post.title.ilike(like)) | (Post.summary.ilike(like)))
            .order_by(Post.created_at.desc())
            .limit(5)
            .all()
        )
        if not posts:
            _send(token, chat_id, f"No matches for <i>{html.escape(q)}</i>.")
            return
        _send(token, chat_id, f"Found {len(posts)} for <i>{html.escape(q)}</i>:")
        for p in posts:
            _send_post_card(token, chat_id, p)
    finally:
        db.close()


def _send_post_card(token: str, chat_id: str, post: Post) -> None:
    """Send a post as a Telegram card (photo+caption when possible)."""
    title = html.escape(post.title or "")
    summary = html.escape((post.summary or "")[:600])
    ent = post.entities or {}
    apply_url = ent.get("apply_url") or (post.links[0] if post.links else None)
    lines = [f"<b>{title}</b>"]
    if summary:
        lines.append(summary)
    facts = []
    if ent.get("deadline"):  facts.append(f"\U0001f4c5 {ent['deadline']}")
    if ent.get("amount"):    facts.append(f"\U0001f4b0 {ent['amount']}")
    if ent.get("location"):  facts.append(f"\U0001f4cd {ent['location']}")
    if facts:
        lines.append(" \u00b7 ".join(facts))
    if apply_url:
        lines.append(f'\U0001f517 <a href="{html.escape(apply_url)}">Apply / Read more</a>')
    text = "\n\n".join(lines)
    _send(token, chat_id, text, photo=post.cover_image_url)


def _send(token: str, chat_id: str, text: str, photo: str | None = None) -> None:
    try:
        if photo:
            r = httpx.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                json={"chat_id": chat_id, "photo": photo,
                      "caption": text[:1000], "parse_mode": "HTML"},
                timeout=20,
            )
            if r.status_code < 300:
                return
            # fall through to plain text
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000],
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=20,
        )
    except Exception as e:
        logger.warning("tg send failed: %s", e)


def get_bot_username() -> str | None:
    """Fetch the bot's @username via /getMe so the Settings page can build a deep link."""
    s = get_settings()
    token = (s.TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        return None
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        if r.status_code == 200 and r.json().get("ok"):
            return r.json()["result"].get("username")
    except Exception:
        return None
    return None
