"""Hybrid scraper with multi-level relevance crawl.

Strategy per source:
- mode=rss        : parse RSS feed only
- mode=http       : multi-level HTTP crawl (BFS) starting from the URL, keep relevant pages
- mode=playwright : render with headless Chromium (JS-heavy sites), then run same crawl
- mode=auto       : try RSS (discover if not provided) -> HTTP crawl -> Playwright fallback

The HTTP crawl follows internal links up to MAX_DEPTH and only keeps pages that look
RELEVANT (deadline / apply / scholarship / event / job signals OR substantive article body).
"""
from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
import trafilatura
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Crawl tuning
MAX_DEPTH = 2          # depth 0 = seed URL, 1 = its links, 2 = links from those
MAX_PAGES = 60         # hard cap on total pages fetched per source
MAX_KEEP = 25          # max relevant items returned
PER_PAGE_LINK_FANOUT = 25

# Signals that strongly suggest the page is "post-worthy"
_RELEVANCE_KEYWORDS = re.compile(
    r"\b(scholarship|bursary|fellowship|grant|funding|internship|apply\s+now|"
    r"applications?\s+(?:open|close)|deadline|register\s+now|hackathon|"
    r"competition|contest|conference|webinar|workshop|summit|hiring|vacancy|"
    r"call\s+for\s+(?:applications?|proposals?)|fully\s+funded|stipend|prize)\b",
    re.IGNORECASE,
)
_RELEVANCE_URL_RX = re.compile(
    r"(scholarship|bursar|fellowship|grant|fund|internship|apply|register|"
    r"opportunit|career|hiring|job|vacanc|event|conference|hackathon|contest|"
    r"competition|deadline|call-for|programme|program/)",
    re.IGNORECASE,
)
_SKIP_URL_RX = re.compile(
    r"/(login|signin|signup|register-account|cart|checkout|terms|privacy|"
    r"cookie|contact|about|tag/|tags/|category/|categories/|author/|page/\d+|"
    r"wp-admin|wp-login|feed/|comments/|search\?)",
    re.IGNORECASE,
)


@dataclass
class ScrapedItem:
    url: str
    title: str
    text: str = ""
    author: str | None = None
    published_at: datetime | None = None
    image_url: str | None = None
    html: str = ""
    extra: dict = field(default_factory=dict)


# ---------- RSS ----------
def scrape_rss(rss_url: str, limit: int = 20) -> list[ScrapedItem]:
    parsed = feedparser.parse(rss_url, agent=UA)
    items: list[ScrapedItem] = []
    for entry in parsed.entries[:limit]:
        link = entry.get("link")
        if not link:
            continue
        published = None
        if entry.get("published_parsed"):
            try:
                published = datetime(*entry.published_parsed[:6])
            except Exception:
                pass
        items.append(ScrapedItem(
            url=link,
            title=entry.get("title", "(untitled)"),
            text=_strip_html(entry.get("summary", "")),
            author=entry.get("author"),
            published_at=published,
        ))
    # For RSS items, fetch the full HTML/text of each link so the extractor
    # has real content to work with (RSS summaries are usually too short).
    items = _hydrate_items(items)
    return items


def _hydrate_items(items: list[ScrapedItem]) -> list[ScrapedItem]:
    if not items:
        return items
    with httpx.Client(headers=_DEFAULT_HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
        for it in items:
            try:
                r = client.get(it.url)
                if r.status_code != 200:
                    continue
                full = _extract_article(it.url, r.text)
                if full:
                    # keep the original title/published_at; upgrade text+html
                    if full.text and len(full.text) > len(it.text):
                        it.text = full.text
                    if not it.html:
                        it.html = full.html
            except Exception:
                continue
    return items


def _strip_html(s: str) -> str:
    return BeautifulSoup(s or "", "lxml").get_text(" ", strip=True)


# ---------- HTTP multi-level crawl ----------
def scrape_http(url: str, *, max_pages: int = MAX_PAGES, max_keep: int = MAX_KEEP) -> list[ScrapedItem]:
    """BFS crawl starting at `url`, depth-limited, same-host only.

    Returns articles whose body or URL signals real content (deadline/apply/etc.)
    """
    seen: set[str] = set()
    kept: list[ScrapedItem] = []
    fallback_candidates: list[ScrapedItem] = []
    seed_host = urlparse(url).netloc.lower()

    queue: deque[tuple[str, int]] = deque([(url, 0)])
    fetched = 0

    with httpx.Client(headers=_DEFAULT_HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
        while queue and fetched < max_pages and len(kept) < max_keep:
            current, depth = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            if _SKIP_URL_RX.search(current):
                continue
            try:
                r = client.get(current)
                fetched += 1
            except Exception as e:
                logger.debug("fetch fail %s: %s", current, e)
                continue
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", "").lower():
                continue
            html = r.text

            # Always try extraction; some listing pages still contain enough
            # structured content to become good drafts after AI processing.
            item = _extract_article(current, html)
            if item:
                if _is_relevant(item):
                    kept.append(item)
                else:
                    fallback_candidates.append(item)

            # Enqueue same-host children (relevant-looking URLs first)
            if depth < MAX_DEPTH:
                children = _discover_links(current, html, seed_host, PER_PAGE_LINK_FANOUT)
                children.sort(key=lambda u: -_url_relevance_score(u))
                for child in children:
                    if child not in seen:
                        queue.append((child, depth + 1))
    if kept:
        return kept

    # Fallback: if relevance rules are too strict for a site, return the best
    # extracted pages by body length instead of returning an empty list.
    if fallback_candidates:
        fallback_candidates.sort(key=lambda it: len(it.text or ""), reverse=True)
        return fallback_candidates[: min(max_keep, 8)]
    return []


def _looks_like_article(html: str) -> bool:
    soup = BeautifulSoup(html or "", "lxml")
    paras = soup.find_all("p")
    total = sum(len((p.get_text() or "").strip()) for p in paras)
    return total >= 400


def _url_relevance_score(u: str) -> int:
    s = 0
    if _RELEVANCE_URL_RX.search(u):
        s += 3
    p = urlparse(u).path.lower()
    if re.search(r"/[a-z0-9]+(?:-[a-z0-9]+){2,}", p):
        s += 1
    return s


def _discover_links(base_url: str, html: str, seed_host: str, limit: int) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0]
        if href in seen:
            continue
        p = urlparse(href)
        if p.scheme not in ("http", "https"):
            continue
        if p.netloc.lower() != seed_host:
            continue
        if len(p.path) < 4:
            continue
        if any(p.path.lower().endswith(ext) for ext in (
            ".jpg", ".jpeg", ".png", ".gif", ".svg", ".pdf", ".zip", ".mp4", ".mp3", ".webp",
        )):
            continue
        if _SKIP_URL_RX.search(href):
            continue
        seen.add(href)
        out.append(href)
        if len(out) >= limit:
            break
    return out


def _extract_article(url: str, html: str) -> ScrapedItem | None:
    text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    if len(text) < 120:
        # Fallback extractor for sites where trafilatura misses article text.
        soup_fb = BeautifulSoup(html, "lxml")
        paras = [p.get_text(" ", strip=True) for p in soup_fb.find_all("p")]
        text = "\n".join(p for p in paras if len(p) >= 30)
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.string.strip() if soup.title and soup.title.string else url)
    if len(text) < 120:
        return None
    image_url = _extract_cover_image(soup, url)
    return ScrapedItem(url=url, title=title[:500], text=text, html=html, image_url=image_url)


def _extract_cover_image(soup: BeautifulSoup, base_url: str) -> str | None:
    """Pull the best cover image: og:image > twitter:image > first big <img>."""
    for sel in [
        ("meta", {"property": "og:image"}),
        ("meta", {"property": "og:image:url"}),
        ("meta", {"name": "twitter:image"}),
        ("meta", {"name": "twitter:image:src"}),
        ("link", {"rel": "image_src"}),
    ]:
        tag = soup.find(*sel)
        if tag:
            v = tag.get("content") or tag.get("href")
            if v:
                return urljoin(base_url, v.strip())
    # Fallback: first <img> with a reasonable size hint
    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        if not src or src.startswith("data:"):
            continue
        if any(src.lower().endswith(ext) for ext in (".svg", ".gif")):
            continue
        return urljoin(base_url, src)
    return None


def _is_relevant(item: ScrapedItem) -> bool:
    """Keep pages that signal real opportunity content OR have substantive body."""
    blob = f"{item.title}\n{item.text[:3000]}"
    if _RELEVANCE_KEYWORDS.search(blob):
        return True
    if _RELEVANCE_URL_RX.search(item.url):
        return True
    if len(item.text) >= 700:
        return True
    return False


# ---------- Playwright ----------
def scrape_playwright(url: str, *, max_keep: int = MAX_KEEP) -> list[ScrapedItem]:
    # Playwright sync API does not work inside a running asyncio loop on Windows.
    # FastAPI handlers may call us from within asyncio, so we run Playwright in a
    # separate process to dodge the NotImplementedError + future-exception spam.
    import multiprocessing
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        logger.warning("playwright not installed")
        return []
    try:
        ctx = multiprocessing.get_context("spawn")
        q: "multiprocessing.Queue" = ctx.Queue()
        p = ctx.Process(target=_playwright_worker, args=(url, max_keep, q))
        p.start()
        p.join(timeout=90)
        if p.is_alive():
            p.terminate()
            return []
        if not q.empty():
            return q.get()
    except Exception as e:
        logger.warning("playwright failed: %s", e)
    return []


def _playwright_worker(url: str, max_keep: int, q) -> None:
    """Runs in a child process so its asyncio loop is independent."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        q.put([])
        return
    seed_host = urlparse(url).netloc.lower()
    seen: set[str] = set()
    kept: list[ScrapedItem] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            bctx = browser.new_context(user_agent=UA)
            page = bctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            seed_html = page.content()
            children = _discover_links(url, seed_html, seed_host, PER_PAGE_LINK_FANOUT)
            children.sort(key=lambda u: -_url_relevance_score(u))
            candidates = [url] + [c for c in children if c not in seen]
            for link in candidates[:MAX_PAGES]:
                if link in seen or len(kept) >= max_keep:
                    continue
                seen.add(link)
                try:
                    if link != url:
                        page.goto(link, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(800)
                    html = page.content()
                    item = _extract_article(link, html)
                    if item and _is_relevant(item):
                        kept.append(item)
                except Exception:
                    continue
            browser.close()
    except Exception:
        pass
    q.put(kept)


# ---------- auto-discovery ----------
def discover_rss(url: str) -> str | None:
    """Sniff a landing page for a linked RSS/Atom feed."""
    try:
        with httpx.Client(headers=_DEFAULT_HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
            r = client.get(url)
            if r.status_code != 200:
                return None
        soup = BeautifulSoup(r.text, "lxml")
        for link in soup.find_all("link", rel=lambda x: x and "alternate" in x):
            ltype = (link.get("type") or "").lower()
            if "rss" in ltype or "atom" in ltype or "xml" in ltype:
                href = link.get("href")
                if href:
                    return urljoin(url, href)
        for path in ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml"):
            try:
                with httpx.Client(headers=_DEFAULT_HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
                    rr = client.head(urljoin(url, path))
                    ct = rr.headers.get("content-type", "").lower()
                    if rr.status_code == 200 and ("xml" in ct or "rss" in ct or "atom" in ct):
                        return urljoin(url, path)
            except Exception:
                continue
    except Exception as e:
        logger.debug("discover_rss failed for %s: %s", url, e)
    return None


# ---------- dispatcher ----------
def scrape_source(*, url: str, rss_url: str | None, mode: str) -> list[ScrapedItem]:
    mode = (mode or "auto").lower()
    if mode == "rss" and rss_url:
        return scrape_rss(rss_url)
    if mode == "http":
        return scrape_http(url)
    if mode == "playwright":
        return scrape_playwright(url)
    # auto: try RSS first (discover if missing) -> HTTP crawl -> Playwright
    if not rss_url:
        rss_url = discover_rss(url)
    if rss_url:
        items = scrape_rss(rss_url)
        if items:
            return items
    items = scrape_http(url)
    if items:
        return items
    return scrape_playwright(url)
