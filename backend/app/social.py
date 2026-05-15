"""Social platform publishers.

Each publisher takes the structured `Post` and a content string (already chosen per platform)
and returns a dict: {status, external_id?, external_url?, error?}.

Missing credentials -> {"status":"skipped","error":"missing credentials"}.
"""
from __future__ import annotations

import logging
import re
from typing import Callable

import httpx

from .config import get_settings
from .models import Post

logger = logging.getLogger(__name__)
settings = get_settings()


def _skip(reason: str = "missing credentials") -> dict:
    return {"status": "skipped", "error": reason}


def _ok(external_id: str | None = None, external_url: str | None = None) -> dict:
    return {"status": "ok", "external_id": external_id, "external_url": external_url}


def _err(msg: str) -> dict:
    return {"status": "error", "error": msg[:1000]}


# ---------- Twitter / X ----------
def post_twitter(post: Post, content: str) -> dict:
    if not all([settings.TWITTER_API_KEY, settings.TWITTER_API_SECRET,
                settings.TWITTER_ACCESS_TOKEN, settings.TWITTER_ACCESS_SECRET]):
        return _skip()
    try:
        import tweepy
        client = tweepy.Client(
            consumer_key=settings.TWITTER_API_KEY,
            consumer_secret=settings.TWITTER_API_SECRET,
            access_token=settings.TWITTER_ACCESS_TOKEN,
            access_token_secret=settings.TWITTER_ACCESS_SECRET,
        )
        resp = client.create_tweet(text=content[:280])
        tid = resp.data.get("id") if resp.data else None
        return _ok(str(tid) if tid else None, f"https://x.com/i/web/status/{tid}" if tid else None)
    except Exception as e:
        return _err(str(e))


# ---------- LinkedIn ----------
def post_linkedin(post: Post, content: str) -> dict:
    if not settings.LINKEDIN_ACCESS_TOKEN or not settings.LINKEDIN_AUTHOR_URN:
        return _skip()
    try:
        body = {
            "author": settings.LINKEDIN_AUTHOR_URN,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": content[:3000]},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }
        headers = {
            "Authorization": f"Bearer {settings.LINKEDIN_ACCESS_TOKEN}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        r = httpx.post("https://api.linkedin.com/v2/ugcPosts", headers=headers, json=body, timeout=30)
        if r.status_code >= 300:
            return _err(f"{r.status_code} {r.text}")
        return _ok(r.headers.get("x-restli-id"))
    except Exception as e:
        return _err(str(e))


# ---------- Facebook Page ----------
def post_facebook(post: Post, content: str) -> dict:
    if not settings.FACEBOOK_PAGE_ID or not settings.FACEBOOK_PAGE_TOKEN:
        return _skip()
    try:
        url = f"https://graph.facebook.com/v20.0/{settings.FACEBOOK_PAGE_ID}/feed"
        data = {"message": content, "access_token": settings.FACEBOOK_PAGE_TOKEN}
        if post.image_url:
            url = f"https://graph.facebook.com/v20.0/{settings.FACEBOOK_PAGE_ID}/photos"
            data["url"] = post.image_url
            data["caption"] = data.pop("message")
        r = httpx.post(url, data=data, timeout=30)
        if r.status_code >= 300:
            return _err(f"{r.status_code} {r.text}")
        return _ok(r.json().get("id"))
    except Exception as e:
        return _err(str(e))


# ---------- Instagram (Graph API; requires image) ----------
def post_instagram(post: Post, content: str) -> dict:
    if not settings.INSTAGRAM_USER_ID or not settings.INSTAGRAM_ACCESS_TOKEN:
        return _skip()
    if not post.image_url:
        return _skip("instagram requires an image_url on the post")
    try:
        base = f"https://graph.facebook.com/v20.0/{settings.INSTAGRAM_USER_ID}"
        token = settings.INSTAGRAM_ACCESS_TOKEN
        r1 = httpx.post(f"{base}/media", data={
            "image_url": post.image_url,
            "caption": content,
            "access_token": token,
        }, timeout=30)
        if r1.status_code >= 300:
            return _err(f"create {r1.status_code} {r1.text}")
        creation_id = r1.json().get("id")
        r2 = httpx.post(f"{base}/media_publish", data={
            "creation_id": creation_id,
            "access_token": token,
        }, timeout=30)
        if r2.status_code >= 300:
            return _err(f"publish {r2.status_code} {r2.text}")
        return _ok(r2.json().get("id"))
    except Exception as e:
        return _err(str(e))


# ---------- WhatsApp Cloud API ----------
def post_whatsapp(post: Post, content: str) -> dict:
    if not settings.WHATSAPP_PHONE_NUMBER_ID or not settings.WHATSAPP_ACCESS_TOKEN:
        return _skip()
    recipients = [r.strip() for r in settings.WHATSAPP_RECIPIENTS.split(",") if r.strip()]
    if not recipients:
        return _skip("no WHATSAPP_RECIPIENTS configured")
    try:
        url = f"https://graph.facebook.com/v20.0/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
        headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
        sent: list[str] = []
        last_err: str | None = None
        for to in recipients:
            r = httpx.post(url, headers=headers, json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": content[:4000], "preview_url": True},
            }, timeout=30)
            if r.status_code >= 300:
                last_err = f"{to}: {r.status_code} {r.text}"
                continue
            sent.append(to)
        if not sent:
            return _err(last_err or "all recipients failed")
        return _ok(",".join(sent))
    except Exception as e:
        return _err(str(e))


# ---------- Reddit ----------
def post_reddit(post: Post, content: str) -> dict:
    if not all([settings.REDDIT_CLIENT_ID, settings.REDDIT_CLIENT_SECRET,
                settings.REDDIT_USERNAME, settings.REDDIT_PASSWORD]):
        return _skip()
    sub = settings.REDDIT_DEFAULT_SUBREDDIT
    if not sub:
        return _skip("REDDIT_DEFAULT_SUBREDDIT not set")
    try:
        import praw
        reddit = praw.Reddit(
            client_id=settings.REDDIT_CLIENT_ID,
            client_secret=settings.REDDIT_CLIENT_SECRET,
            username=settings.REDDIT_USERNAME,
            password=settings.REDDIT_PASSWORD,
            user_agent=settings.REDDIT_USER_AGENT,
        )
        title = (post.variants or {}).get("reddit_title") or post.title
        sub_obj = reddit.subreddit(sub)
        submission = sub_obj.submit(title=title[:300], selftext=content)
        return _ok(submission.id, f"https://reddit.com{submission.permalink}")
    except Exception as e:
        return _err(str(e))


# ---------- Telegram ----------
_TG_ALLOWED = {"b", "strong", "i", "em", "u", "s", "code", "pre", "a"}
_TG_TAG_RX = re.compile(r"<(/?)([a-zA-Z0-9]+)([^>]*)>")


def _telegram_safe_html(html_in: str) -> str:
    """Telegram's `parse_mode=HTML` only supports a tiny tag set. Convert what
    we can (p, br, ul/li become newlines / bullets) and drop everything else."""
    if not html_in:
        return ""
    s = html_in
    s = re.sub(r"<\s*br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p\s*>", "\n\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<p[^>]*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<li[^>]*>", "\n  - ", s, flags=re.IGNORECASE)
    s = re.sub(r"</li\s*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"</?(ul|ol)[^>]*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<h[1-6][^>]*>", "<b>", s, flags=re.IGNORECASE)
    s = re.sub(r"</h[1-6]\s*>", "</b>\n", s, flags=re.IGNORECASE)

    def _t(m: re.Match) -> str:
        closing, name = m.group(1), m.group(2).lower()
        if name == "strong":
            name = "b"
        elif name == "em":
            name = "i"
        if name not in _TG_ALLOWED:
            return ""
        if closing:
            return f"</{name}>"
        if name == "a":
            href_m = re.search(r'href\s*=\s*"([^"]+)"', m.group(3) or "")
            href = href_m.group(1) if href_m else "#"
            if href.lower().startswith("javascript:"):
                return ""
            return f'<a href="{href}">'
        return f"<{name}>"

    s = _TG_TAG_RX.sub(_t, s)
    # collapse excessive newlines
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


def post_telegram(post: Post, content: str) -> dict:
    if not settings.TELEGRAM_BOT_TOKEN:
        return _skip()
    chats = [c.strip() for c in settings.TELEGRAM_CHAT_IDS.split(",") if c.strip()]
    if not chats:
        return _skip("no TELEGRAM_CHAT_IDS")

    # Build a clean rich message: title (bold) + summary + key facts + apply CTA.
    ent = post.entities or {}
    lines: list[str] = [f"<b>{(post.title or '').strip()}</b>"]
    if post.summary:
        lines.append(post.summary.strip())
    fact_lines = []
    if ent.get("organization"):  fact_lines.append(f"🏢 <b>From:</b> {ent['organization']}")
    if ent.get("universities"):  fact_lines.append("🎓 <b>Universities:</b> " + ", ".join(ent["universities"][:3]))
    if ent.get("location"):      fact_lines.append(f"📍 <b>Location:</b> {ent['location']}")
    if ent.get("amount"):        fact_lines.append(f"💰 <b>Value:</b> {ent['amount']}")
    if ent.get("start_date"):    fact_lines.append(f"🚀 <b>Starts:</b> {ent['start_date']}")
    if ent.get("deadline"):      fact_lines.append(f"📅 <b>Deadline:</b> {ent['deadline']}")
    if fact_lines:
        lines.append("\n".join(fact_lines))
    if ent.get("apply_url"):
        lines.append(f'🔗 <a href="{ent["apply_url"]}">Apply / Read more</a>')
    elif post.links:
        lines.append(f'🔗 <a href="{post.links[0]}">Read more</a>')
    if post.hashtags:
        lines.append(" ".join("#" + h for h in post.hashtags[:5]))

    message = "\n\n".join(lines)
    # If caller passed a custom variant, prefer it (already HTML-ish or plain) but still clean.
    if content and content.strip() and content.strip() != post.summary:
        message = _telegram_safe_html(content) or message
    message = _telegram_safe_html(message)
    if len(message) > 3800:
        message = message[:3800] + "..."

    photo = post.cover_image_url
    try:
        sent: list[str] = []
        last_err = None
        for chat in chats:
            if photo:
                url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendPhoto"
                payload = {"chat_id": chat, "photo": photo,
                           "caption": message[:1000], "parse_mode": "HTML"}
            else:
                url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
                payload = {"chat_id": chat, "text": message,
                           "parse_mode": "HTML", "disable_web_page_preview": False}
            r = httpx.post(url, json=payload, timeout=30)
            if r.status_code >= 300:
                last_err = f"{chat}: {r.status_code} {r.text[:200]}"
                continue
            sent.append(chat)
        if not sent:
            return _err(last_err or "failed")
        return _ok(",".join(sent))
    except Exception as e:
        return _err(str(e))


def telegram_send_test(text: str = "Strip is connected.") -> dict:
    """Used by Settings page 'Send test' button."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}
    chats = [c.strip() for c in settings.TELEGRAM_CHAT_IDS.split(",") if c.strip()]
    if not chats:
        return {"ok": False, "error": "TELEGRAM_CHAT_IDS not set"}
    results = []
    for chat in chats:
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat, "text": f"🤖 {text}", "parse_mode": "HTML"},
                timeout=15,
            )
            results.append({"chat": chat, "status": r.status_code, "body": r.text[:200]})
        except Exception as e:
            results.append({"chat": chat, "status": "exception", "body": str(e)})
    ok = any(r["status"] == 200 for r in results)
    return {"ok": ok, "results": results}


# ---------- Discord ----------
def post_discord(post: Post, content: str) -> dict:
    if not settings.DISCORD_WEBHOOK_URL:
        return _skip()
    try:
        r = httpx.post(settings.DISCORD_WEBHOOK_URL,
                       json={"content": content[:1900]}, timeout=30)
        if r.status_code >= 300:
            return _err(f"{r.status_code} {r.text}")
        return _ok()
    except Exception as e:
        return _err(str(e))


# ---------- Local RSS feed (free, always available) ----------
def post_rss(post: Post, content: str) -> dict:
    """Always succeeds. The /rss endpoint serves all 'rss-published' posts as an Atom feed
    that you can plug into IFTTT/Zapier/Make/Buffer/etc. for free fan-out."""
    return _ok(external_id=f"post-{post.id}", external_url=f"/rss")


# ---------- Mastodon ----------
def post_mastodon(post: Post, content: str) -> dict:
    if not settings.MASTODON_ACCESS_TOKEN:
        return _skip()
    try:
        from mastodon import Mastodon
        m = Mastodon(access_token=settings.MASTODON_ACCESS_TOKEN,
                     api_base_url=settings.MASTODON_BASE_URL)
        status = m.status_post(content[:500])
        return _ok(str(status["id"]), status.get("url"))
    except Exception as e:
        return _err(str(e))


# ---------- registry ----------
PUBLISHERS: dict[str, tuple[Callable[[Post, str], dict], str]] = {
    # platform_key -> (function, variants_key)
    "twitter":   (post_twitter, "twitter"),
    "linkedin":  (post_linkedin, "linkedin"),
    "facebook":  (post_facebook, "facebook"),
    "instagram": (post_instagram, "instagram"),
    "whatsapp":  (post_whatsapp, "whatsapp"),
    "reddit":    (post_reddit, "reddit_body"),
    "telegram":  (post_telegram, "telegram"),
    "discord":   (post_discord, "discord"),
    "mastodon":  (post_mastodon, "mastodon"),
    "rss":       (post_rss, "summary"),
}

PLATFORM_KEYS = list(PUBLISHERS.keys())


def publish_to(platform: str, post: Post) -> dict:
    if platform not in PUBLISHERS:
        return _err(f"unknown platform: {platform}")
    fn, variant_key = PUBLISHERS[platform]
    variants = post.variants or {}
    content = variants.get(variant_key) or post.summary or post.title
    try:
        return fn(post, content)
    except Exception as e:
        logger.exception("publisher %s crashed", platform)
        return _err(str(e))
