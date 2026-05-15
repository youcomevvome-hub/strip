"""Content-type detection + structured entity extraction.

Runs *without* any LLM, using regex/heuristics on the scraped HTML + plain text.
Designed so even a deterministic deployment (no Ollama, no API keys) still pulls
the *useful* facts out of a page — deadlines, apply links, prices, locations, etc.

Result shape:
{
  "kind": "scholarship" | "job" | "event" | "course" | "grant" | "contest" |
          "product" | "funding" | "internship" | "article",
  "deadline": ISO-8601 date string | None,
  "start_date": ISO-8601 date string | None,
  "end_date": ISO-8601 date string | None,
  "apply_url": str | None,
  "location": str | None,
  "organization": str | None,
  "amount": str | None,           # "$5,000" / "€1,200/mo"
  "eligibility": [str, ...],
  "tags": [str, ...],
  "highlights": [str, ...],       # short bullet points of the most important facts
}
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# ------------- content-type detection -------------
# Order matters: more specific kinds first. Common ambiguous words like
# "stipend" appear in both scholarships and internships, so we check
# internship/job before falling back to the broader scholarship pattern.
_KIND_KEYWORDS = [
    ("internship",  r"\b(internship|intern\b|trainee\s+program|software\s+engineering\s+intern)\b"),
    ("job",         r"\b(job|career|hiring|vacancy|we'?re\s+hiring|full[-\s]?time\s+role|part[-\s]?time\s+role|remote\s+role)\b"),
    ("scholarship", r"\b(scholarship|bursary|fellowship|tuition\s+waiver|fully\s+funded\s+scholarship|fully[-\s]funded)\b"),
    ("grant",       r"\b(grant|funding\s+opportunity|call\s+for\s+proposals|rfp\b|seed\s+fund)\b"),
    ("contest",     r"\b(contest|hackathon|competition|challenge|awards?\b|prize)\b"),
    ("event",       r"\b(conference|webinar|workshop|meet[-\s]?up|summit|symposium|seminar)\b"),
    ("course",      r"\b(course|bootcamp|certification|mooc|enrol+ment|training\s+program)\b"),
    ("funding",     r"\b(series\s+[a-d]|raised\s+\$?\d|valuation|venture\s+capital|funding\s+round)\b"),
    ("product",     r"\b(launches?|launched|releases?|released|now\s+available|introducing|version\s+\d)\b"),
]

def detect_kind(title: str, text: str) -> str:
    blob = f"{title}\n{text[:3000]}".lower()
    for kind, pat in _KIND_KEYWORDS:
        if re.search(pat, blob, re.IGNORECASE):
            return kind
    return "article"


# ------------- date parsing -------------
_MONTHS = "(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"

_DATE_PATTERNS = [
    # ISO 2026-08-15
    (re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"), "iso"),
    # 15 August 2026 / 15 Aug 2026
    (re.compile(rf"\b(\d{{1,2}})\s+{_MONTHS}\s+(20\d{{2}})\b", re.IGNORECASE), "dmy"),
    # August 15, 2026 / Aug 15 2026
    (re.compile(rf"\b{_MONTHS}\s+(\d{{1,2}}),?\s+(20\d{{2}})\b", re.IGNORECASE), "mdy"),
    # 15/08/2026 or 08/15/2026 (assume month-first if first <= 12 and second > 12; else day-first)
    (re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})\b"), "numeric"),
]

_MONTH_NUM = {m: i for i, m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], start=1)}


def _parse_iso(s: str) -> str | None:
    """Return the first parseable date found in s, normalized to YYYY-MM-DD."""
    if not s:
        return None
    for rx, kind in _DATE_PATTERNS:
        m = rx.search(s)
        if not m:
            continue
        try:
            if kind == "iso":
                y, mo, d = m.group(1), m.group(2), m.group(3)
                return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            if kind == "dmy":
                d, mo_s, y = m.group(1), m.group(0).split()[1][:3].lower(), m.group(2)
                mo = _MONTH_NUM.get(mo_s)
                if mo:
                    return f"{int(y):04d}-{mo:02d}-{int(d):02d}"
            if kind == "mdy":
                parts = re.split(r"\s+", m.group(0).strip())
                mo_s = parts[0][:3].lower()
                d = parts[1].rstrip(",")
                y = parts[2]
                mo = _MONTH_NUM.get(mo_s)
                if mo:
                    return f"{int(y):04d}-{mo:02d}-{int(d):02d}"
            if kind == "numeric":
                a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if a > 12:
                    d, mo = a, b
                elif b > 12:
                    mo, d = a, b
                else:
                    # ambiguous: prefer day-first (international)
                    d, mo = a, b
                if 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}-{mo:02d}-{d:02d}"
        except Exception:
            continue
    return None


def _find_dates_near(text: str, *labels: str) -> str | None:
    """Find a date occurring near one of the given labels (within ~120 chars after)."""
    for label in labels:
        for m in re.finditer(rf"{label}[\s:\-—]{{0,4}}([^\n\r]{{0,120}})", text, re.IGNORECASE):
            snippet = m.group(1)
            iso = _parse_iso(snippet)
            if iso:
                return iso
    return None


# ------------- apply / register link -------------
_APPLY_TEXT_RX = re.compile(
    r"\b(apply\s+now|apply\s+here|apply\s+online|application\s+form|register\s+now|"
    r"submit\s+application|application\s+link|register\s+here|sign[-\s]?up)\b",
    re.IGNORECASE,
)

# Hosts that are share/social buttons rather than real apply links.
_SOCIAL_SHARE_HOSTS = {
    "twitter.com", "x.com", "facebook.com", "www.facebook.com",
    "linkedin.com", "www.linkedin.com", "reddit.com", "www.reddit.com",
    "t.me", "telegram.me", "wa.me", "api.whatsapp.com", "pinterest.com",
    "www.pinterest.com", "plus.google.com",
}
_SOCIAL_SHARE_PATH_RX = re.compile(
    r"/(intent/tweet|sharer|share|share-offsite|submit)\b", re.IGNORECASE,
)


def _is_social_share_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    host = (p.netloc or "").lower()
    if host in _SOCIAL_SHARE_HOSTS and _SOCIAL_SHARE_PATH_RX.search(p.path or ""):
        return True
    if host in {"twitter.com", "x.com"} and "/intent/" in (p.path or ""):
        return True
    return False


def find_apply_link(html: str, page_url: str) -> str | None:
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return None
    page_host = urlparse(page_url).netloc

    candidates: list[tuple[int, str]] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        abs_url = urljoin(page_url, href)
        if _is_social_share_url(abs_url):
            continue
        text = " ".join(a.stripped_strings)[:200]
        score = 0
        if _APPLY_TEXT_RX.search(text):
            score += 5
        if re.search(r"\b(apply|application|register|signup|sign-up)\b", abs_url, re.IGNORECASE):
            score += 2
        if urlparse(abs_url).netloc != page_host:
            score += 1  # external "apply" links often point to a form host
        # button-y anchors
        cls = " ".join(a.get("class") or [])
        if re.search(r"\b(btn|button|cta|apply)\b", cls, re.IGNORECASE):
            score += 1
        if score >= 3:
            candidates.append((score, abs_url))
    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]
    return None


# ------------- amount / money -------------
_MONEY_RX = re.compile(
    r"(?:[€£$₹¥₦₱₽₩]\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|m|million|thousand|bn|billion))?"
    r"|\b(?:R|ZAR|USD|EUR|GBP|INR|JPY|CAD|AUD|NGN|KES|GHS|UGX|TZS|ZMW)\s?\d[\d,]*(?:\.\d+)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|INR|JPY|CAD|AUD|NGN|ZAR|KES|GHS))",
    re.IGNORECASE,
)

def find_amount(text: str) -> str | None:
    for m in _MONEY_RX.finditer(text or ""):
        val = m.group(0).strip()
        # cheap filter: skip obvious phone numbers / years
        if re.fullmatch(r"\d{4}", val):
            continue
        if re.fullmatch(r"R\d{2,4}", val):  # avoid matching things like "R250" alone? keep it; better than nothing
            return val
        return val
    # labelled value: "Value: 5000" or "Stipend: 1200/mo"
    m = re.search(r"\b(?:value|amount|stipend|prize|salary|funding)\s*[:\-]\s*([^\n\r]{1,60})",
                  text or "", re.IGNORECASE)
    if m:
        v = m.group(1).strip().rstrip(".")
        if v:
            return v
    return None


# ------------- location -------------
_REMOTE_RX = re.compile(r"\b(remote|work\s+from\s+home|wfh|fully\s+remote|hybrid|on[-\s]?site)\b", re.IGNORECASE)
_CITY_HINT_RX = re.compile(
    r"\b(?:in|at|based\s+in|located\s+in|takes\s+place\s+in|hosted\s+in|location[s]?[:\-])\s+"
    r"([A-Z][A-Za-z]+(?:[\s,][A-Z][A-Za-z]+){0,3})",
)
# Avoid noisy capture phrases. These are common false positives the
# "in <Word>" pattern picks up when the surrounding text is not a real location.
_LOCATION_BLOCKLIST = {s.lower() for s in {
    "the", "this", "these", "that", "those", "a", "an", "some",
    "the world", "the top", "the top of", "the organization", "the field",
    "the program", "the past", "the future", "the year", "the country",
    "university", "nutrition", "email", "english", "science", "engineering",
    "business", "finance", "technology", "history", "medicine",
    "this post", "this article", "this opportunity", "this scholarship",
}}
# Common country/city tail words that confirm a real location
_LOCATION_TAIL = re.compile(
    r"\b(City|Town|Province|State|Country|UK|USA|US|UAE|EU|Africa|Asia|Europe|"
    r"South\s+Africa|United\s+Kingdom|United\s+States|Germany|France|Kenya|Nigeria|"
    r"Ghana|Uganda|Tanzania|Switzerland|Netherlands|Sweden|Norway|Canada|Australia|"
    r"India|Japan|China|Brazil|Egypt|Morocco|Rwanda|Ethiopia|Senegal)\b"
)

def find_location(text: str) -> str | None:
    if not text:
        return None
    remote = _REMOTE_RX.search(text)
    candidates: list[str] = []
    for m in _CITY_HINT_RX.finditer(text[:4000]):
        cand = m.group(1).strip().rstrip(",.").strip()
        if not cand or cand.lower() in _LOCATION_BLOCKLIST:
            continue
        # drop obvious non-place captures (e.g. "the African Tech")
        first = cand.split()[0]
        if first.lower() in {"the", "an", "a"}:
            continue
        candidates.append(cand)
    # Only accept a candidate when it looks like a real place: either it has
    # a country/admin tail (UK, Africa, ...) or it contains multiple capitalized
    # tokens (e.g. "Cape Town"). Bare single nouns like "Nutrition" are dropped.
    best = next((c for c in candidates if _LOCATION_TAIL.search(c)), None)
    if not best:
        best = next((c for c in candidates if len(c.split()) >= 2), None)
    parts = []
    if best:
        parts.append(best)
    if remote:
        parts.append(remote.group(1).title())
    seen: list[str] = []
    for p in parts:
        if p.lower() not in {s.lower() for s in seen}:
            seen.append(p)
    return ", ".join(seen) or None


# ------------- organization (very rough) -------------
def find_organization(html: str, text: str) -> str | None:
    if html:
        try:
            soup = BeautifulSoup(html, "lxml")
            og = soup.find("meta", property="og:site_name")
            if og and og.get("content"):
                return og["content"].strip()
        except Exception:
            pass
    m = re.search(r"\b(?:by|from|hosted\s+by|offered\s+by|organi[sz]ed\s+by)\s+([A-Z][A-Za-z0-9&.\- ]{2,60})", text or "")
    if m:
        return m.group(1).strip().rstrip(",.")
    return None


# ------------- universities / institutions -------------
_UNI_RX = re.compile(
    r"\b("
    r"University\s+of\s+[A-Z][A-Za-z\-]+(?:\s+[A-Z][A-Za-z\-]+){0,3}"
    r"|[A-Z][A-Za-z\-]+(?:\s+[A-Z][A-Za-z\-]+){0,3}\s+University"
    r"|[A-Z][A-Za-z\-]+(?:\s+[A-Z][A-Za-z\-]+){0,2}\s+(?:College|Institute|Polytechnic|Academy|School\s+of\s+[A-Z][a-z]+)"
    r")\b"
)


def find_universities(text: str) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for m in _UNI_RX.finditer(text):
        name = m.group(1).strip().rstrip(".,;")
        # filter obvious false positives (start with common stop words)
        if name.split()[0].lower() in {"the", "any", "all", "your", "our"}:
            continue
        if name.lower() in {s.lower() for s in seen}:
            continue
        seen.append(name)
        if len(seen) >= 6:
            break
    return seen


# ------------- eligibility -------------
_ELIG_LABELS_RX = re.compile(r"^\s*(eligibility|who\s+can\s+apply|requirements?)\s*[:\-]?", re.IGNORECASE)

def find_eligibility(text: str) -> list[str]:
    if not text:
        return []
    lines = text.splitlines()
    out: list[str] = []
    capture = 0
    for ln in lines:
        if _ELIG_LABELS_RX.search(ln):
            capture = 8
            continue
        if capture > 0:
            s = ln.strip(" •-–—\t")
            if s and len(s) < 240:
                out.append(s)
            capture -= 1
            if not s:
                capture = 0
        if len(out) >= 6:
            break
    return out


# ------------- main entry -------------
def extract_entities(*, url: str, title: str, text: str, html: str | None = None) -> dict:
    """Pure-Python heuristic extractor — runs alongside (or instead of) the LLM."""
    text = text or ""
    title = title or ""

    kind = detect_kind(title, text)

    deadline = (
        _find_dates_near(text, "deadline", "apply\\s+by", "applications?\\s+close", "closing\\s+date", "last\\s+date", "due\\s+date")
        or _find_dates_near(text, "expires?", "ends?", "submission\\s+deadline")
    )
    start_date = (
        _find_dates_near(text, "starts?\\s+on", "start\\s+date", "begins?\\s+on", "commence", "kick[-\\s]?off", "program\\s+start")
    )
    end_date = _find_dates_near(text, "ends?\\s+on", "end\\s+date", "concludes?\\s+on")

    apply_url = find_apply_link(html or "", url)
    amount = find_amount(text)
    location = find_location(text)
    org = find_organization(html or "", text)
    eligibility = find_eligibility(text)
    universities = find_universities(text)

    highlights: list[str] = []
    if kind != "article":
        highlights.append(f"Type: {kind.title()}")
    if org:           highlights.append(f"From: {org}")
    if universities:  highlights.append("Universities: " + ", ".join(universities[:3]))
    if location:      highlights.append(f"Location: {location}")
    if amount:        highlights.append(f"Value: {amount}")
    if start_date:    highlights.append(f"Starts: {start_date}")
    if deadline:      highlights.append(f"Deadline: {deadline}")
    if apply_url:     highlights.append(f"Apply: {apply_url}")

    return {
        "kind": kind,
        "deadline": deadline,
        "start_date": start_date,
        "end_date": end_date,
        "apply_url": apply_url,
        "location": location,
        "organization": org,
        "universities": universities,
        "amount": amount,
        "eligibility": eligibility,
        "highlights": highlights,
        "extracted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
