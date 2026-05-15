"""Local AI via Ollama with a strong heuristic fallback.

If Ollama is reachable, we use it to enrich the post copy. Otherwise we generate
post-ready content purely from the heuristic entity extractor — kind-aware
title/summary/bullets/variants that ALWAYS surface deadline + apply link.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from .config import get_settings
from .extractor import extract_entities

logger = logging.getLogger(__name__)
settings = get_settings()

PROMPT = """You are a content editor extracting POST-READY facts from a scraped page.
Produce JSON with these fields:
- kind: one of [scholarship, internship, job, grant, contest, event, course, funding, product, article]
- title:    catchy <= 90 chars, MUST name the program/opportunity
- summary:  2-3 sentence summary that names deadline / amount / where / who
- entities: object capturing the structured facts (use null when unknown):
    deadline:     ISO date (YYYY-MM-DD) of the application/submission deadline
    start_date:   ISO date when it starts
    end_date:     ISO date when it ends
    apply_url:    URL to apply or register
    location:     city/country or "Remote" / "Online"
    organization: the offering / hosting organization
    amount:       prize / salary / stipend with currency
    eligibility:  array of short strings
- body:     200-350 word markdown that LEADS with the key facts, then explains
- bullets:  3-6 useful fact bullets (deadline, value, apply link, location...)
- hashtags: 4-8 lowercase hashtags WITHOUT '#'
- links:    important URLs (max 5) — apply URL MUST be first if present
- variants: object — text for each channel, ALWAYS surfacing deadline & apply link.
    twitter, linkedin, facebook, instagram, telegram, discord, mastodon,
    reddit_title, reddit_body, whatsapp

Return ONLY valid JSON. Always include the source URL at the end of every social variant.

ARTICLE URL: {url}
ARTICLE TITLE: {title}
ARTICLE TEXT:
{text}
"""


def structure_article(*, url: str, title: str, text: str, html: str | None = None) -> dict:
    text = (text or "")[:8000]
    base_entities = extract_entities(url=url, title=title, text=text, html=html or "")
    try:
        out = _ollama_structure(url=url, title=title, text=text)
        used_ai = True
    except Exception as e:
        logger.warning("ollama unavailable (%s); using heuristic generator", e)
        out = _heuristic_structure(url=url, title=title, text=text, entities=base_entities)
        used_ai = False
    out["entities"] = _merge_entities(out.get("entities") or {}, base_entities)
    out = _enrich_with_entities(out, url=url, title=title, summary_fallback=text, used_ai=used_ai)
    return out


# ---------- Ollama ----------
def _ollama_structure(*, url: str, title: str, text: str) -> dict:
    prompt = PROMPT.format(url=url, title=title, text=text)
    with httpx.Client(timeout=120) as client:
        r = client.post(
            f"{settings.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": settings.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.3},
            },
        )
        r.raise_for_status()
        data = r.json()
    raw = data.get("response", "").strip()
    parsed = json.loads(raw)
    return _coerce(parsed, url=url, title=title, text=text)


def _coerce(d: dict, *, url: str, title: str, text: str) -> dict:
    raw_body = d.get("body") or text[:1500]
    # AI may return either Markdown or HTML; convert markdown-ish to HTML, then sanitize.
    if "<" not in raw_body or not re.search(r"</?[a-zA-Z]+", raw_body):
        raw_body = _md_to_html(raw_body)
    else:
        # Even if HTML, AI often leaves stray **bold** / *italic* markers inside.
        raw_body = _apply_inline_md(raw_body)
    body_html = sanitize_html(raw_body)
    return {
        "title": (d.get("title") or title)[:200],
        "summary": d.get("summary") or _first_sentences(text, 2),
        "body": body_html,
        "bullets": _as_list(d.get("bullets"))[:6],
        "hashtags": [h.lstrip("#").lower() for h in _as_list(d.get("hashtags"))[:8]],
        "links": _as_list(d.get("links"))[:5],
        "variants": d.get("variants") or {},
        "entities": d.get("entities") or {},
    }


# ---------- Heuristic generator (used when Ollama is down) ----------
_KIND_PREFIX = {
    "scholarship": "Scholarship",
    "internship":  "Internship",
    "job":         "Job",
    "grant":       "Grant",
    "contest":     "Contest",
    "event":       "Event",
    "course":      "Course",
    "funding":     "Funding",
    "product":     "Launch",
}
_KIND_HASHTAGS = {
    "scholarship": ["scholarship", "education", "opportunity", "scholarships"],
    "internship":  ["internship", "careers", "students", "opportunity"],
    "job":         ["hiring", "jobs", "careers", "opportunity"],
    "grant":       ["grants", "funding", "opportunity"],
    "contest":     ["contest", "competition", "prize"],
    "event":       ["event", "conference", "networking"],
    "course":      ["learning", "course", "education"],
    "funding":     ["funding", "startups", "venturecapital"],
    "product":     ["launch", "product", "news"],
    "article":     ["news"],
}


def _heuristic_structure(*, url: str, title: str, text: str, entities: dict) -> dict:
    """Build a post entirely from the extracted entities + page text."""
    kind = entities.get("kind") or "article"
    org = entities.get("organization")
    location = entities.get("location")
    amount = entities.get("amount")
    deadline = entities.get("deadline")
    apply_url = entities.get("apply_url")
    eligibility = entities.get("eligibility") or []

    # Title: prefix with kind label when meaningful
    base_title = (title or "").strip() or "Opportunity"
    prefix = _KIND_PREFIX.get(kind)
    if prefix and not re.search(rf"\b{re.escape(prefix)}\b", base_title, re.IGNORECASE):
        nice_title = f"{prefix}: {base_title}"
    else:
        nice_title = base_title
    nice_title = nice_title[:200]

    # Summary: fact-led
    facts = []
    if org:      facts.append(f"Offered by {org}")
    if location: facts.append(f"in {location}")
    if amount:   facts.append(f"value {amount}")
    if deadline: facts.append(f"apply by {deadline}")
    head = ", ".join(facts).capitalize() if facts else _first_sentences(text, 1)
    body_sentences = _first_sentences(text, 3)
    summary = (head + ". " + body_sentences).strip(" .") + "."
    summary = re.sub(r"\s+", " ", summary)[:600]

    # Bullets — start empty, _enrich_with_entities will prepend the fact bullets
    extra_bullets: list[str] = []
    for line in eligibility[:3]:
        extra_bullets.append(line)
    # add a couple of plain content sentences too
    for s in re.split(r"(?<=[.!?])\s+", text.strip())[:4]:
        if 20 < len(s) < 220 and s not in extra_bullets:
            extra_bullets.append(s.strip())
        if len(extra_bullets) >= 4:
            break

    # Body: lead with the facts, then add the text
    body_lines: list[str] = []
    if kind != "article":
        body_lines.append(f"**{prefix or kind.title()} opportunity**")
    fact_lines = []
    if org:                              fact_lines.append(f"- **Organization:** {org}")
    if entities.get("universities"):     fact_lines.append("- **Universities:** " + ", ".join(entities["universities"][:5]))
    if location:                         fact_lines.append(f"- **Location:** {location}")
    if amount:                           fact_lines.append(f"- **Value:** {amount}")
    if entities.get("start_date"):       fact_lines.append(f"- **Starts:** {entities['start_date']}")
    if deadline:                         fact_lines.append(f"- **Deadline:** {deadline}")
    if apply_url:                        fact_lines.append(f"- **Apply:** {apply_url}")
    if fact_lines:
        body_lines.append("\n".join(fact_lines))
    body_lines.append(text[:1400])
    if eligibility:
        body_lines.append("**Eligibility**\n" + "\n".join(f"- {e}" for e in eligibility[:6]))
    body_md = "\n\n".join(body_lines)
    body = _md_to_html(body_md)

    hashtags = list(_KIND_HASHTAGS.get(kind, ["news"]))[:6]

    return {
        "title": nice_title,
        "summary": summary,
        "body": body,
        "bullets": extra_bullets[:6],
        "hashtags": hashtags,
        "links": [apply_url] if apply_url else [url],
        "variants": {},   # built by _enrich_with_entities below
        "entities": dict(entities),
    }


# ---------- shared helpers ----------
def _merge_entities(ai_ent: dict, base: dict) -> dict:
    """AI-provided entity values win when truthy; otherwise fall back to heuristic."""
    merged = dict(base or {})
    for k, v in (ai_ent or {}).items():
        if v in (None, "", [], {}):
            continue
        merged[k] = v
    hl = []
    kind = merged.get("kind") or base.get("kind") or "article"
    if kind and kind != "article":
        hl.append(f"Type: {str(kind).title()}")
    for label, key in [
        ("From", "organization"), ("Location", "location"), ("Value", "amount"),
        ("Starts", "start_date"), ("Deadline", "deadline"), ("Apply", "apply_url"),
    ]:
        v = merged.get(key)
        if v:
            hl.append(f"{label}: {v}")
    if merged.get("universities"):
        hl.append("Universities: " + ", ".join(merged["universities"][:3]))
    merged["highlights"] = hl
    return merged


def _enrich_with_entities(out: dict, *, url: str, title: str, summary_fallback: str, used_ai: bool) -> dict:
    ent = out.get("entities") or {}
    apply_url = ent.get("apply_url")
    deadline = ent.get("deadline")
    start_date = ent.get("start_date")
    location = ent.get("location")
    amount = ent.get("amount")
    org = ent.get("organization")

    # links: apply url first, source last
    links = list(out.get("links") or [])
    if apply_url and apply_url not in links:
        links = [apply_url] + links
    if url not in links:
        links.append(url)
    out["links"] = links[:6]

    # bullets: prepend structured facts (deduped)
    fact_bullets: list[str] = []
    if deadline:     fact_bullets.append(f"\U0001f4c5 Deadline: {deadline}")
    if start_date:   fact_bullets.append(f"\U0001f680 Starts: {start_date}")
    if amount:       fact_bullets.append(f"\U0001f4b0 Value: {amount}")
    if location:     fact_bullets.append(f"\U0001f4cd Location: {location}")
    if org:          fact_bullets.append(f"\U0001f3e2 From: {org}")
    if ent.get("universities"):
        fact_bullets.append("\U0001f393 Universities: " + ", ".join(ent["universities"][:3]))
    if apply_url:    fact_bullets.append(f"\U0001f517 Apply: {apply_url}")
    existing = [b for b in (out.get("bullets") or []) if b and b not in fact_bullets]
    out["bullets"] = (fact_bullets + existing)[:9]

    # variants: build a strong default set, then ensure each ends with deadline + apply + source
    summary = out.get("summary") or _first_sentences(summary_fallback, 2) or title
    nice_title = out.get("title") or title
    variants = dict(out.get("variants") or {})
    if not variants:
        variants = _build_variants(title=nice_title, summary=summary, url=url, ent=ent)

    tail_parts: list[str] = []
    if deadline:  tail_parts.append(f"Deadline: {deadline}")
    if apply_url: tail_parts.append(f"Apply: {apply_url}")
    if url not in tail_parts and (not apply_url or apply_url != url):
        tail_parts.append(url)
    tail = "\n" + " | ".join(tail_parts) if tail_parts else ""

    for k, v in list(variants.items()):
        if not isinstance(v, str):
            continue
        if (apply_url and apply_url in v) or (deadline and deadline in v) or url in v:
            continue
        candidate = (v.rstrip() + tail).strip()
        if k == "twitter" and len(candidate) > 270:
            candidate = candidate[:267] + "..."
        if k == "mastodon" and len(candidate) > 480:
            candidate = candidate[:477] + "..."
        variants[k] = candidate
    out["variants"] = variants
    return out


def _build_variants(*, title: str, summary: str, url: str, ent: dict) -> dict:
    """Build kind-aware default variants with facts already inline."""
    kind = ent.get("kind") or "article"
    deadline = ent.get("deadline")
    apply_url = ent.get("apply_url")
    amount = ent.get("amount")
    location = ent.get("location")

    fact_line_parts = []
    if amount:   fact_line_parts.append(f"\U0001f4b0 {amount}")
    if location: fact_line_parts.append(f"\U0001f4cd {location}")
    if deadline: fact_line_parts.append(f"\U0001f4c5 Deadline {deadline}")
    facts = " · ".join(fact_line_parts)

    cta = apply_url or url
    short = (summary[:200] + "...") if len(summary) > 200 else summary

    tags = " ".join("#" + t for t in _KIND_HASHTAGS.get(kind, ["news"])[:3])

    twitter = "\n".join(filter(None, [title, facts, f"Apply: {cta}", tags]))
    linkedin = "\n\n".join(filter(None, [
        title,
        summary,
        facts,
        f"Apply here: {cta}" if cta else None,
    ]))
    facebook = "\n".join(filter(None, [title, short, facts, cta]))
    instagram = "\n".join(filter(None, [title, short, facts, cta, tags]))
    telegram = "\n".join(filter(None, [f"*{title}*", short, facts, f"[Apply]({cta})" if cta else None]))
    discord = "\n".join(filter(None, [f"**{title}**", short, facts, cta]))
    mastodon = "\n".join(filter(None, [title, short, facts, cta, tags]))
    whatsapp = "\n".join(filter(None, [title, short, facts, cta]))

    return {
        "twitter": twitter[:270],
        "linkedin": linkedin,
        "facebook": facebook,
        "instagram": instagram,
        "telegram": telegram,
        "discord": discord,
        "mastodon": mastodon[:480],
        "reddit_title": title[:280],
        "reddit_body": "\n\n".join(filter(None, [summary, facts, f"Apply: {cta}" if cta else None, f"Source: {url}"])),
        "whatsapp": whatsapp,
    }


def _as_list(x) -> list[str]:
    if x is None:
        return []
    if isinstance(x, list):
        return [str(i).strip() for i in x if str(i).strip()]
    if isinstance(x, str):
        return [s.strip() for s in re.split(r"[\n,;]", x) if s.strip()]
    return []


def _first_sentences(text: str, n: int) -> str:
    sents = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return " ".join(sents[:n])


# ------------- markdown -> safe HTML (no external deps) -------------
_URL_RX = re.compile(r"(https?://[^\s)<>\]]+)")


def _md_to_html(md: str) -> str:
    """Convert the small subset of Markdown the heuristic generator produces into
    clean HTML. The output is meant for both Quill (rich text editor) and the
    public post page, so it never contains script/style/raw HTML from input."""
    if not md:
        return ""
    import html as _html
    safe = _html.escape(md)

    out_blocks: list[str] = []
    in_list = False
    list_buf: list[str] = []

    def _flush_list():
        nonlocal in_list, list_buf
        if list_buf:
            items = "".join(f"<li>{li}</li>" for li in list_buf)
            out_blocks.append(f"<ul>{items}</ul>")
            list_buf = []
        in_list = False

    for raw_para in re.split(r"\n\s*\n", safe):
        para = raw_para.strip("\n")
        if not para.strip():
            continue
        lines = [ln for ln in para.split("\n") if ln.strip()]
        # All lines are list items?
        if all(re.match(r"^\s*[-*]\s+", ln) for ln in lines):
            for ln in lines:
                list_buf.append(re.sub(r"^\s*[-*]\s+", "", ln))
            _flush_list()
        else:
            _flush_list()
            # Heading-ish (single bold line)?
            if len(lines) == 1 and re.match(r"^\*\*[^*]+\*\*$", lines[0]):
                inner = lines[0].strip("*")
                out_blocks.append(f"<h3>{inner}</h3>")
            else:
                joined = " ".join(lines)
                out_blocks.append(f"<p>{joined}</p>")
    _flush_list()

    html_out = "\n".join(out_blocks)
    html_out = _apply_inline_md(html_out)
    return html_out


def _apply_inline_md(s: str) -> str:
    """Run inline-markdown conversions (bold/italic/autolink) and strip any orphan
    asterisk markers that AI output may leave behind."""
    if not s:
        return s
    # ***bold-italic*** first
    s = re.sub(r"\*\*\*([^*\n]+)\*\*\*", r"<strong><em>\1</em></strong>", s)
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", s)
    # Drop any leftover lone or paired asterisks that didn't form a pair.
    s = re.sub(r"\*+", "", s)
    # Drop leading markdown bullet hyphens that escaped paragraph-mode (e.g. "- foo" inside <p>).
    s = re.sub(r"(<p>)\s*[-\u2022]\s+", r"\1", s)
    # Autolink bare URLs (skip ones already inside an href="...")
    def _link(m: re.Match) -> str:
        url = m.group(1)
        return f'<a href="{url}" target="_blank" rel="noopener">{url}</a>'
    # Quick guard: only autolink URLs not immediately preceded by =" (already in attribute) or >.
    s = re.sub(r'(?<!["=>])(https?://[^\s<>\)\]"]+)', _link, s)
    return s


# ------------- HTML sanitiser for user-saved bodies (Quill output) -------------
_ALLOWED_TAGS = {
    "p", "br", "strong", "em", "u", "s", "ol", "ul", "li", "blockquote",
    "h1", "h2", "h3", "h4", "a", "img", "code", "pre", "span", "div",
}
_ALLOWED_ATTRS = {
    "a":   {"href", "target", "rel", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "span": {"class"}, "div": {"class"},
}
_TAG_RX = re.compile(r"<(/?)([a-zA-Z0-9]+)([^>]*)>")
_ATTR_RX = re.compile(r'([a-zA-Z\-]+)\s*=\s*"([^"]*)"')


def sanitize_html(html_in: str) -> str:
    """Drop any script/style/iframe/etc and unwanted attributes from user-supplied HTML."""
    if not html_in:
        return ""
    # Strip whole <script>/<style> blocks first.
    cleaned = re.sub(r"<(script|style|iframe|object|embed)[^>]*>.*?</\1>",
                     "", html_in, flags=re.IGNORECASE | re.DOTALL)

    def _tag(m: re.Match) -> str:
        closing, name, rest = m.group(1), m.group(2).lower(), m.group(3)
        if name not in _ALLOWED_TAGS:
            return ""
        if closing:
            return f"</{name}>"
        allowed = _ALLOWED_ATTRS.get(name, set())
        kept = []
        for am in _ATTR_RX.finditer(rest or ""):
            an, av = am.group(1).lower(), am.group(2)
            if an not in allowed:
                continue
            if an in ("href", "src") and av.strip().lower().startswith("javascript:"):
                continue
            kept.append(f'{an}="{av}"')
        attr_str = (" " + " ".join(kept)) if kept else ""
        if name == "a" and attr_str and 'target="_blank"' in attr_str and "rel=" not in attr_str:
            attr_str += ' rel="noopener"'
        return f"<{name}{attr_str}>"

    return _TAG_RX.sub(_tag, cleaned)

