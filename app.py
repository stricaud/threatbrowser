import glob
import hashlib
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

import feedparser
import html2text
import httpx
import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import fetcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
# httpx logs every request at INFO level — suppress to WARNING since our fallback
# chain intentionally lets httpx fail (403/timeout) before trying curl-cffi/Playwright.
logging.getLogger("httpx").setLevel(logging.WARNING)

_HERE = os.path.abspath(os.path.dirname(__file__))
CACHE_DIR = os.environ.get("TB_CACHE", os.path.join(_HERE, "cache"))
os.makedirs(CACHE_DIR, exist_ok=True)

FAVICON_DIR = os.path.join(CACHE_DIR, "favicons")
os.makedirs(FAVICON_DIR, exist_ok=True)

FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


# ── Fetch job ─────────────────────────────────────────────────────────────────

FETCH_TIMEOUT = 60  # raised to 60 to give Playwright fallback time to complete
_MAX_FETCH_WORKERS = 20


@dataclass
class FetchJob:
    running: bool = False
    total: int = 0
    done: int = 0
    errors: int = 0
    new_articles: int = 0
    current: str = ""
    started: str = ""
    finished: str = ""
    timed_out: list = field(default_factory=list)
    in_flight: list = field(default_factory=list)


@dataclass
class PrefetchJob:
    running: bool = False
    total: int = 0
    done: int = 0
    errors: int = 0
    succeeded: int = 0
    current: str = ""
    started: str = ""
    finished: str = ""


_job = FetchJob()
_job_lock = threading.Lock()

_prefetch_job = PrefetchJob()
_prefetch_lock = threading.Lock()
_PREFETCH_WORKERS = 4
_prefetch_log: list[dict] = []   # cleared at the start of each run
_MAX_PREFETCH_LOG = 1000


def _try_record_fetch_error(source_uuid: str, error: str | None) -> None:
    """Write fetch error to DB without raising — never disrupts the fetch loop."""
    try:
        db.update_source_fetch_error(source_uuid, error)
    except Exception as exc:
        log.warning("Could not record fetch error for %s: %s", source_uuid, exc)


def _run_fetch(source_uuids: list[str] | None = None):
    sources = db.get_sources(active_only=True)
    if source_uuids:
        sources = [s for s in sources if s["uuid"] in source_uuids]

    with _job_lock:
        _job.total = len(sources)
        _job.done = _job.errors = _job.new_articles = 0
        _job.timed_out = []
        _job.in_flight = []
        _job.current = ""
        _job.started = datetime.now(timezone.utc).isoformat()
        _job.finished = ""

    def _fetch_one(source):
        cfg = source.get("config") or {}
        known = None
        if source.get("scraper") == "html" and cfg.get("paginate"):
            known = db.get_source_article_urls(source["id"])
        articles = fetcher.fetch_source(source, known_urls=known)
        new = db.upsert_articles(articles)
        db.update_source_fetched(source["uuid"], datetime.now(timezone.utc).isoformat())
        return len(articles), new

    n_workers = min(len(sources), _MAX_FETCH_WORKERS) if sources else 1
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=n_workers)
    futures = {pool.submit(_fetch_one, src): src for src in sources}
    done_uuids: set[str] = set()

    with _job_lock:
        _job.in_flight = [src["uuid"] for src in sources]

    try:
        for future in concurrent.futures.as_completed(futures, timeout=FETCH_TIMEOUT):
            src = futures[future]
            done_uuids.add(src["uuid"])
            with _job_lock:
                _job.in_flight = [u for u in _job.in_flight if u != src["uuid"]]
            try:
                count, new = future.result()
                with _job_lock:
                    _job.new_articles += new
                log.info("Fetched %s: %d (%d new)", src["name"], count, new)
                _try_record_fetch_error(src["uuid"], None)
            except Exception as exc:
                log.error("Error %s: %s", src["name"], exc)
                _try_record_fetch_error(src["uuid"], str(exc))
                with _job_lock:
                    _job.errors += 1
            finally:
                with _job_lock:
                    _job.done += 1
    except concurrent.futures.TimeoutError:
        pass

    for future, src in futures.items():
        if src["uuid"] not in done_uuids:
            future.cancel()
            log.warning("Timeout fetching %s after %ds", src["name"], FETCH_TIMEOUT)
            _try_record_fetch_error(src["uuid"], f"Timed out after {FETCH_TIMEOUT}s")
            with _job_lock:
                _job.in_flight = [u for u in _job.in_flight if u != src["uuid"]]
                _job.timed_out.append(src["uuid"])
                _job.errors += 1
                _job.done += 1

    # Don't block waiting for stuck threads — they'll finish on their own via HTTP timeouts
    pool.shutdown(wait=False)

    with _job_lock:
        _job.running = False
        _job.current = ""
        _job.finished = datetime.now(timezone.utc).isoformat()


def _run_prefetch(source_uuids: list[str] | None = None):
    """Download and cache article content for all uncached articles."""
    conn = db.get_conn()
    if source_uuids:
        placeholders = ",".join("?" * len(source_uuids))
        rows = conn.execute(
            f"SELECT a.id, a.uuid, a.url, a.source_id, a.title "
            f"FROM articles a JOIN sources s ON a.source_id = s.id "
            f"WHERE a.cached_at IS NULL AND s.uuid IN ({placeholders})",
            source_uuids,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, uuid, url, source_id, title FROM articles WHERE cached_at IS NULL"
        ).fetchall()
    conn.close()
    uncached = [dict(r) for r in rows]

    # Pre-load source configs to avoid N per-article DB lookups
    sources_by_id = {s["id"]: s for s in db.get_sources()}

    with _prefetch_lock:
        _prefetch_job.total = len(uncached)
        _prefetch_job.done = _prefetch_job.errors = _prefetch_job.succeeded = 0
        _prefetch_job.current = ""
        _prefetch_job.started = datetime.now(timezone.utc).isoformat()
        _prefetch_job.finished = ""
        _prefetch_log.clear()
    db.clear_prefetch_errors()

    def _fetch_one(article):
        path = _cache_path(article["id"])
        if os.path.exists(path):
            # File already on disk but DB has no cached_at — sync it.
            db.set_article_cached(article["uuid"])
            db.set_article_download_status(article["uuid"], 200)
            return True
        source = sources_by_id.get(article["source_id"]) or {}
        cfg = source.get("config") or {}
        with _prefetch_lock:
            _prefetch_job.current = (article.get("title") or article["url"])[:80]
        try:
            md, extracted_date = _download_content(
                article["url"],
                cfg.get("date_selector", ""),
                cfg.get("date_format", ""),
                cfg.get("download_ua", ""),
                db._parse_ioc_patterns(cfg.get("block_detect", "")),
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(md)
            db.set_article_cached(article["uuid"])
            db.set_article_download_status(article["uuid"], 200)
            db.index_article_content(article["id"], md)
            if not extracted_date and cfg.get("date_url_format"):
                from fetcher import _date_from_url
                extracted_date = _date_from_url(article["url"], cfg["date_url_format"])
            if extracted_date:
                db.update_article_date(article["uuid"], extracted_date)
            return True
        except Exception as exc:
            reason = str(exc)
            # Extract HTTP status code from exception string when available (e.g. "HTTP 403")
            import re as _re
            _m = _re.search(r'\b([45]\d{2})\b', reason)
            http_status = int(_m.group(1)) if _m else 0
            log.debug("Prefetch %s: %s", article["url"][:60], reason)
            db.set_article_download_status(article["uuid"], http_status)
            entry = {
                "url": article["url"],
                "title": (article.get("title") or "")[:120],
                "error": reason[:300],
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            db.save_prefetch_error(entry)
            with _prefetch_lock:
                if len(_prefetch_log) < _MAX_PREFETCH_LOG:
                    _prefetch_log.append(entry)
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=_PREFETCH_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, a): a for a in uncached}
        for future in concurrent.futures.as_completed(futures):
            try:
                ok = future.result()
                with _prefetch_lock:
                    if ok:
                        _prefetch_job.succeeded += 1
                    else:
                        _prefetch_job.errors += 1
            except Exception:
                with _prefetch_lock:
                    _prefetch_job.errors += 1
            finally:
                with _prefetch_lock:
                    _prefetch_job.done += 1

    with _prefetch_lock:
        _prefetch_job.running = False
        _prefetch_job.current = ""
        _prefetch_job.finished = datetime.now(timezone.utc).isoformat()


# ── Content download ──────────────────────────────────────────────────────────

def _cache_path(article_int_id: int) -> str:
    """Cache files are named by integer PK (internal)."""
    return os.path.join(CACHE_DIR, f"{article_int_id}.md")


_FALLBACK_HEADERS = {
    "User-Agent": "Wget/1.21.4",
    "Accept": "*/*",
}

_HTTPX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def _fetch_with_httpx(url: str, user_agent: str = "") -> bytes:
    """HTTP/2 fetch — different TLS fingerprint from requests, bypasses some bot filters."""
    hdrs = dict(_HTTPX_HEADERS)
    if user_agent:
        hdrs["User-Agent"] = user_agent
    with httpx.Client(http2=True, headers=hdrs, timeout=25, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.content


def _fetch_with_playwright(url: str, user_agent: str = "") -> bytes:
    """Headless Chromium fetch — last resort for TLS-fingerprint or JS-challenge blocked sites."""
    with _sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = browser.new_context(user_agent=user_agent) if user_agent else browser.new_context()
            page = ctx.new_page()
            # Hide the webdriver flag that bot-detection services check for
            page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome={runtime:{}};"
            )
            resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
            if resp and resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            return page.content().encode("utf-8")
        finally:
            browser.close()


# Known CDN/WAF block-page fingerprints (checked globally on every download)
_GLOBAL_BLOCK_SIGNATURES: list[bytes] = [
    b"errors.edgesuite.net",            # Akamai
    b"challenge-platform.cloudflare.com",  # Cloudflare JS challenge
    b"__cf_chl_rt_tk",                  # Cloudflare challenge token
    b"Access to this page has been denied",  # Imperva / Distil
]


def _is_blocked_page(html_bytes: bytes, extra_patterns: list[str]) -> bool:
    """Return True if the response looks like a bot-protection error page."""
    preview = html_bytes[:8192]
    for sig in _GLOBAL_BLOCK_SIGNATURES:
        if sig in preview:
            return True
    preview_lo = preview.lower()
    for pat in extra_patterns:
        if pat.lower().encode() in preview_lo:
            return True
    return False

_DATE_FMTS = ("%Y-%m-%d", "%B %d, %Y", "%d %B %Y", "%b %d, %Y", "%d %b %Y",
              "%Y/%m/%d", "%d-%b-%Y", "%d-%b-%Y %H:%M:%S")


def _parse_date_text(text: str, date_format: str = "") -> str | None:
    """Parse a date string, normalising abbreviated months with trailing periods (e.g. 'Dec. 22, 2025')."""
    import re as _re
    # Strip trailing dot from month abbreviations: "Dec. 22" → "Dec 22"
    norm = _re.sub(r'\b([A-Za-z]{3,9})\.\s', r'\1 ', text.strip())
    if date_format:
        for t in (text.strip(), norm):
            try:
                return datetime.strptime(t, date_format).replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                pass
        return None
    for t in (text.strip(), norm):
        for fmt in _DATE_FMTS:
            try:
                return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                continue
    return None


def _extract_date(soup, date_selector: str, date_format: str) -> str | None:
    """Try to extract a publication date from the page using a CSS selector."""
    if not date_selector:
        return None
    el = soup.select_one(date_selector)
    if not el:
        return None
    # <time datetime="..."> is the cleanest source
    dt_attr = el.get("datetime", "").strip()
    if dt_attr:
        try:
            return datetime.fromisoformat(dt_attr.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    text = el.get_text(strip=True)
    if not text:
        return None
    return _parse_date_text(text, date_format)


def _download_content(url: str, date_selector: str = "", date_format: str = "",
                      user_agent: str = "", block_detect: list[str] | None = None) -> tuple[str, str | None]:
    """Fetch URL → markdown.  Falls back: requests → httpx/HTTP2 → playwright Chromium."""
    html_bytes: bytes | None = None
    last_exc: Exception | None = None
    block_pats: list[str] = block_detect or []

    headers = dict(FETCH_HEADERS)
    if user_agent:
        headers["User-Agent"] = user_agent

    def _blocked(b: bytes) -> bool:
        if _is_blocked_page(b, block_pats):
            log.debug("block page detected for %s — trying next strategy", url)
            return True
        return False

    _req_was_403 = False  # track whether strategy 1 got a hard 403

    # Strategy 1: plain requests (HTTP/1.1)
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 403:
            r = requests.get(url, headers=_FALLBACK_HEADERS, timeout=20)
        r.raise_for_status()
        if not _blocked(r.content):
            html_bytes = r.content
        else:
            last_exc = RuntimeError("block page (requests)")
    except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
        last_exc = exc
        log.debug("requests timed out for %s — trying httpx/http2", url)
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if code != 403:
            raise  # real error (404, 500 …) — don't retry
        _req_was_403 = True
        last_exc = exc
        log.debug("requests got 403 for %s — trying httpx/http2", url)

    # Strategy 2: httpx with HTTP/2 (different TLS fingerprint)
    if html_bytes is None:
        try:
            candidate = _fetch_with_httpx(url, user_agent)
            if not _blocked(candidate):
                html_bytes = candidate
                last_exc = None
            else:
                last_exc = RuntimeError("block page (httpx)")
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            log.debug("httpx timed out for %s — trying curl-cffi", url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 403:
                raise
            last_exc = exc
            log.debug("httpx got 403 for %s — trying curl-cffi", url)

    # Strategy 3: curl-cffi Firefox TLS impersonation — bypasses Akamai/Cloudflare TLS checks
    if html_bytes is None and fetcher._HAS_CURL_CFFI:
        try:
            from curl_cffi import requests as _cffi
            _cffi_r = _cffi.get(url, impersonate="firefox", timeout=25)
            _cffi_r.raise_for_status()
            if not _blocked(_cffi_r.content):
                html_bytes = _cffi_r.content
                last_exc = None
            else:
                last_exc = RuntimeError("block page (curl-cffi)")
                log.debug("curl-cffi block page for %s", url)
        except Exception as exc:
            last_exc = exc
            log.debug("curl-cffi failed for %s: %s — trying playwright", url, exc)

    # Strategy 4: headless Chromium — for JS challenges that curl-cffi cannot handle
    if html_bytes is None:
        if not _HAS_PLAYWRIGHT:
            raise last_exc or RuntimeError("All download strategies failed")
        log.debug("fetching %s via headless Chromium", url)
        candidate = _fetch_with_playwright(url, user_agent)
        if _blocked(candidate):
            raise last_exc or RuntimeError("All download strategies returned a block page")
        html_bytes = candidate

    # Normalize bare numeric character references (e.g. &#160 without closing ;).
    # Python 3.14's html.parser changed behaviour: it now passes everything up to EOF
    # as the char-ref name, causing int('160BY DOMAIN…') crashes in both BeautifulSoup
    # and html2text.  Adding the missing semicolons fixes both parsers.
    html_bytes = re.sub(rb'&#(\d+)(?![;0-9])', rb'&#\1;', html_bytes)
    html_bytes = re.sub(rb'&#([xX][0-9a-fA-F]+)(?![;0-9a-fA-F])', rb'&#\1;', html_bytes)

    soup = BeautifulSoup(html_bytes, "html.parser")

    # Extract date BEFORE stripping layout elements (date often lives in header/footer)
    extracted_date = _extract_date(soup, date_selector, date_format)

    # Remove noise
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()

    # Absolutize URLs so images load in the preview
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src", "")
        if src and not src.startswith("http"):
            img["src"] = urljoin(url, src)
        elif img.get("data-src"):
            img["src"] = urljoin(url, img["data-src"])
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith(("http", "mailto:", "#")):
            a["href"] = urljoin(url, href)

    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_links = False
    h.ignore_images = False
    h.unicode_snob = True
    h.skip_internal_links = False
    h.protect_links = False

    return h.handle(str(soup)), extracted_date


# ── Source discovery ──────────────────────────────────────────────────────────

def _fetch_with_fallback(url: str, headers: dict, timeout: int = 15):
    """GET url, falling back to curl-cffi Firefox TLS impersonation on 403/timeout."""
    _BLOCK = (403, 429, 503)
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code not in _BLOCK:
            r.raise_for_status()
            return r
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        pass
    except requests.exceptions.HTTPError as exc:
        if exc.response is None or exc.response.status_code not in _BLOCK:
            raise
    # Fallback: curl-cffi Firefox TLS impersonation
    if fetcher._HAS_CURL_CFFI:
        from curl_cffi import requests as _cffi
        r = _cffi.get(url, impersonate="firefox", timeout=timeout + 5)
        r.raise_for_status()
        return r
    raise requests.exceptions.HTTPError(f"Could not fetch {url} (blocked, curl-cffi not available)")


def _probe_feed_paths(base_url: str) -> str | None:
    """Try common RSS/Atom path suffixes relative to base_url. Returns first working feed URL."""
    from urllib.parse import urlparse as _urlparse, urljoin as _urljoin
    parsed = _urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    # Candidates: page-relative first, then origin-relative
    candidates = []
    for suffix in ("/rss", "/feed", "/rss.xml", "/feed.xml", "/atom.xml", "/atom"):
        candidates.append(path + suffix)        # e.g. /blog/rss
    for suffix in ("/rss", "/feed", "/rss.xml", "/feed.xml", "/atom.xml"):
        candidates.append(suffix)               # e.g. /rss
    # deduplicate preserving order
    seen: set[str] = set()
    unique = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    for candidate in unique:
        candidate_url = origin + candidate
        if candidate_url == base_url:
            continue
        try:
            r = _fetch_with_fallback(candidate_url, fetcher.RSS_HEADERS, timeout=8)
            feed = feedparser.parse(r.content)
            if [e for e in feed.entries if hasattr(e, "link")]:
                return candidate_url
        except Exception:
            continue
    return None


def _run_discovery(url: str) -> tuple[str, str, dict] | None:
    """Probe a URL and return (scraper, best_url, config) or None on failure.

    Used when promoting a pseudo-source so it gets a working scraper
    instead of the bare domain URL stored from the scenario reference.
    """
    try:
        r = _fetch_with_fallback(url, fetcher.RSS_HEADERS, timeout=15)
        feed = feedparser.parse(r.content)
        if [e for e in feed.entries if hasattr(e, "link")]:
            return "rss", url, {}
    except Exception:
        pass

    try:
        r = _fetch_with_fallback(url, FETCH_HEADERS, timeout=15)
        html_bytes = r.content
    except Exception:
        return None

    soup = BeautifulSoup(html_bytes, "html.parser")

    feed_link = (soup.find("link", {"type": "application/rss+xml"}) or
                 soup.find("link", {"type": "application/atom+xml"}))
    if feed_link and feed_link.get("href"):
        feed_url = urljoin(url, feed_link["href"])
        try:
            r2 = _fetch_with_fallback(feed_url, fetcher.RSS_HEADERS, timeout=15)
            feed2 = feedparser.parse(r2.content)
            if [e for e in feed2.entries if hasattr(e, "link")]:
                return "rss", feed_url, {}
        except Exception:
            pass

    config, articles, _ = _detect_html_config(url, soup)
    if articles:
        return "html", url, config

    # Last resort: probe common feed path suffixes (handles SPAs without <link> tags)
    found = _probe_feed_paths(url)
    if found:
        return "rss", found, {}

    return None


def _detect_html_config(page_url: str, soup) -> tuple[dict, list[dict], int]:
    """Heuristically detect link selector, pagination, and date selector from a parsed page."""
    from urllib.parse import urlparse as _urlparse
    parsed = _urlparse(page_url)
    domain = f"{parsed.scheme}://{parsed.netloc}"

    _BAD_SCHEMES = ("#", "javascript:", "mailto:", "tel:", "sms:", "callto:",
                    "fax:", "data:", "blob:", "vbscript:")

    def _is_post_href(href: str) -> bool:
        if not href or href.startswith(_BAD_SCHEMES):
            return False
        full = href if href.startswith("http") else urljoin(page_url, href)
        if not full.startswith(("http://", "https://")):
            return False
        if "/cdn-cgi/" in full:
            return False
        if not full.startswith(domain):
            return False
        path = _urlparse(full).path.rstrip("/")
        parts = [p for p in path.split("/") if p]
        if not parts:
            return False
        skip = {"category", "categories", "tag", "tags", "author", "page", "feed",
                "wp-content", "wp-admin", "search", "archive", "archives", "cdn-cgi",
                "rss", "atom", "about", "contact", "privacy", "sitemap"}
        return not any(p.lower() in skip for p in parts)

    LINK_CANDIDATES = [
        "article h2 a", "article h1 a",
        "h2.entry-title a", "h1.entry-title a",
        "h2.post-title a", ".post-title a",
        ".post-preview h2 a", ".post-preview h1 a",
        "h2 a",
    ]
    best_sel, best_articles = None, []
    for sel in LINK_CANDIDATES:
        try:
            arts = []
            for a in soup.select(sel):
                href = a.get("href", "")
                title = a.get_text(strip=True)
                if title and len(title) > 5 and _is_post_href(href):
                    full = href if href.startswith("http") else urljoin(page_url, href)
                    arts.append({"title": title, "url": full, "published_at": None})
            if len(arts) >= 2:
                best_sel, best_articles = sel, arts
                break
        except Exception:
            continue

    config: dict = {}
    if best_sel:
        config["link_selector"] = best_sel
        sample_hrefs = [a.get("href", "") for a in soup.select(best_sel) if a.get("href")]
        if sample_hrefs and not sample_hrefs[0].startswith("http"):
            config["link_prefix"] = domain

    # Pagination detection
    NEXT_SELS = [
        "a[rel='next']",
        "a.next.page-numbers",
        "li.pager__item--next a",
        "li.pager-next a",
        "a.pagination-next",
        "a.btn-outline-primary[href*='page']",
    ]
    next_sel_used = None
    for sel in NEXT_SELS:
        try:
            el = soup.select_one(sel)
            if el and el.get("href"):
                next_sel_used = sel
                break
        except Exception:
            continue
    if not next_sel_used:
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower().strip("→»> \t")
            href = a["href"]
            if text in ("next", "older", "older posts") and ("page" in href.lower() or href.startswith("?")):
                next_sel_used = f"a[href='{href}']"
                break
    if next_sel_used:
        config["paginate"] = True
        config["next_selector"] = next_sel_used
        config["max_pages"] = 20

    # Date element detection in listing cards
    DATE_SELS = [
        "time[datetime]", "time",
        "span.date", "span.published", "span.entry-date",
        ".entry-date", ".published", ".post-date", ".article-date",
        ".field--type-datetime span",
    ]
    for date_sel in DATE_SELS:
        el = soup.select_one(date_sel)
        if not el:
            continue
        if el.get("datetime"):
            config["card_date_selector"] = date_sel
            break
        text = el.get_text(strip=True)
        if text and 4 < len(text) < 32:
            config["card_date_selector"] = date_sel
            for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%B %d, %Y",
                        "%Y-%m-%d", "%d %B %Y", "%b %d, %Y", "%d/%m/%Y", "%m/%d/%Y"):
                try:
                    datetime.strptime(text.strip()[:len(fmt)], fmt)
                    config["card_date_format"] = fmt
                    break
                except ValueError:
                    pass
            break

    return config, best_articles[:10], len(best_articles)


# ── App ───────────────────────────────────────────────────────────────────────

def _startup_sync(cache_dir: str):
    """Backfill cached_at for articles whose .md file exists but DB has no cached_at."""
    db.reindex_all_cached(cache_dir)
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, uuid FROM articles WHERE cached_at IS NULL"
    ).fetchall()
    conn.close()
    now = datetime.now(timezone.utc).isoformat()
    synced = 0
    for row_id, uuid in rows:
        if os.path.exists(os.path.join(cache_dir, f"{row_id}.md")):
            c = db.get_conn()
            c.execute(
                "UPDATE articles SET cached_at=?, download_status=200 WHERE uuid=?",
                (now, uuid),
            )
            c.commit()
            c.close()
            synced += 1
    if synced:
        log.info("startup: synced cached_at for %d articles whose .md files existed", synced)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    threading.Thread(target=_startup_sync, args=(CACHE_DIR,), daemon=True).start()
    yield


app = FastAPI(title="ThreatBrowser", lifespan=lifespan)


# ── Source discovery ─────────────────────────────────────────────────────────

class DiscoverRequest(BaseModel):
    url: str


@app.post("/api/discover")
def discover_source_endpoint(body: DiscoverRequest):
    """Probe a URL and return the best fetching strategy with a sample of articles."""
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    notes: list[str] = []

    # 1. Try the URL directly as an RSS/Atom feed
    try:
        r = _fetch_with_fallback(url, fetcher.RSS_HEADERS, timeout=15)
        feed = feedparser.parse(r.content)
        entries = [e for e in feed.entries if hasattr(e, "link")]
        if entries:
            articles = [{"title": getattr(e, "title", None), "url": e.link,
                         "published_at": fetcher._entry_date(e)} for e in entries[:10]]
            dates = sum(1 for a in articles if a["published_at"])
            name = getattr(getattr(feed, "feed", None), "title", "") or ""
            total = len(entries)
            notes.append(f"RSS/Atom feed · {total} item{'s' if total != 1 else ''}")
            notes.append("Publication dates present" if dates == len(articles)
                         else f"Partial dates ({dates}/{len(articles)} items)" if dates
                         else "No publication dates in feed")
            return {"strategy": "rss", "url": url, "config": {}, "articles": articles,
                    "article_count": total, "name": name, "notes": notes,
                    "dates_from": "rss" if dates else "none"}
    except Exception:
        pass

    # 2. Fetch as HTML page
    try:
        r = _fetch_with_fallback(url, FETCH_HEADERS, timeout=15)
        html_bytes = r.content
    except Exception as exc:
        raise HTTPException(502, f"Could not reach {url}: {exc}")

    soup = BeautifulSoup(html_bytes, "html.parser")
    title_el = soup.find("title")
    name = title_el.get_text(strip=True) if title_el else ""

    # 2a. Look for a feed link in <head>
    feed_link = (soup.find("link", {"type": "application/rss+xml"}) or
                 soup.find("link", {"type": "application/atom+xml"}))
    if feed_link and feed_link.get("href"):
        feed_url = urljoin(url, feed_link["href"])
        try:
            r2 = _fetch_with_fallback(feed_url, fetcher.RSS_HEADERS, timeout=15)
            feed = feedparser.parse(r2.content)
            entries = [e for e in feed.entries if hasattr(e, "link")]
            if entries:
                articles = [{"title": getattr(e, "title", None), "url": e.link,
                             "published_at": fetcher._entry_date(e)} for e in entries[:10]]
                dates = sum(1 for a in articles if a["published_at"])
                feed_name = getattr(getattr(feed, "feed", None), "title", "") or name
                total = len(entries)
                notes.append(f"RSS feed found in page ({feed_url})")
                notes.append(f"{total} item{'s' if total != 1 else ''} in feed")
                notes.append("Publication dates present" if dates else "No publication dates in feed")
                return {"strategy": "rss", "url": feed_url, "config": {}, "articles": articles,
                        "article_count": total, "name": feed_name, "notes": notes,
                        "dates_from": "rss" if dates else "none"}
        except Exception:
            notes.append("RSS link found in page but could not fetch it")

    # 3. HTML scraping auto-detection
    config, articles, count = _detect_html_config(url, soup)

    if articles:
        notes.append(f"Detected {count} article link{'s' if count != 1 else ''} on page")
        if config.get("paginate"):
            notes.append("Pagination detected · will follow all pages")
        if config.get("card_date_selector"):
            notes.append("Dates detected on listing page")
        else:
            notes.append("No dates on listing page · configure date extraction after adding")
    else:
        # SPA / JS-rendered pages often have no static <link> tags and no detectable
        # article links — try common feed path suffixes before giving up.
        found = _probe_feed_paths(url)
        if found:
            try:
                r_feed = _fetch_with_fallback(found, fetcher.RSS_HEADERS, timeout=12)
                feed3 = feedparser.parse(r_feed.content)
                entries3 = [e for e in feed3.entries if hasattr(e, "link")]
                if entries3:
                    arts3 = [{"title": getattr(e, "title", None), "url": e.link,
                              "published_at": fetcher._entry_date(e)} for e in entries3[:10]]
                    dates3 = sum(1 for a in arts3 if a["published_at"])
                    total3 = len(entries3)
                    notes.append(f"RSS feed found at {found} (probed common paths)")
                    return {"strategy": "rss", "url": found, "config": {}, "articles": arts3,
                            "article_count": total3, "name": name, "notes": notes,
                            "dates_from": "rss" if dates3 else "none"}
            except Exception:
                pass
        notes.append("Could not auto-detect article links · use manual configuration")

    return {"strategy": "html" if articles else "unknown", "url": url, "config": config,
            "articles": articles, "article_count": count, "name": name, "notes": notes,
            "dates_from": "card" if config.get("card_date_selector") else "none"}


# ── Sources ───────────────────────────────────────────────────────────────────

class SourceCreate(BaseModel):
    name: str
    url: str
    scraper: str = "rss"
    config: dict = {}
    tags: list[str] = []


class SourcePatch(BaseModel):
    active: Optional[bool] = None
    name: Optional[str] = None
    url: Optional[str] = None
    scraper: Optional[str] = None
    config: Optional[dict] = None
    tags: Optional[list[str]] = None


@app.get("/api/sources")
def list_sources(q: str = Query(""), include_pseudo: bool = Query(False)):
    sources = db.get_sources()
    if not include_pseudo:
        sources = [s for s in sources if not s.get("is_pseudo")]
    if q:
        q_lo = q.lower()
        sources = [s for s in sources if q_lo in s["name"].lower() or q_lo in s["url"].lower()]
    return sources


@app.post("/api/sources", status_code=201)
def add_source(body: SourceCreate):
    import sqlite3 as _sqlite3
    try:
        return db.add_source(body.name, body.url, body.scraper, body.config, body.tags)
    except _sqlite3.IntegrityError:
        existing = next((s for s in db.get_sources() if s["url"] == body.url), None)
        if existing:
            raise HTTPException(409, f"URL already used by source \"{existing['name']}\"")
        raise HTTPException(409, "URL already exists in another source")
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/source-tags")
def list_source_tags():
    return db.get_all_source_tags()


@app.patch("/api/sources/{source_uuid}")
def patch_source(source_uuid: str, body: SourcePatch):
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    was_pseudo = False
    url_changed = False
    disc_result = None
    src = next((s for s in db.get_sources() if s["uuid"] == source_uuid), None)

    # Activating a pseudo-source promotes it to a real source
    if kwargs.get("active") is True:
        kwargs["is_pseudo"] = 0
        if src and src.get("is_pseudo"):
            was_pseudo = True
            try:
                discovered = _run_discovery(src["url"])
            except Exception as exc:
                log.warning("Auto-discovery failed for %s: %s", src.get("name"), exc)
                discovered = None
            if discovered:
                disc_scraper, disc_url, disc_config = discovered
                log.info("Auto-discovered %s: scraper=%s url=%s", src["name"], disc_scraper, disc_url)
                kwargs["scraper"] = disc_scraper
                kwargs["url"] = disc_url
                if disc_config and not src.get("config"):
                    kwargs["config"] = disc_config
                disc_result = {"scraper": disc_scraper, "url": disc_url}

    # URL changed manually — re-run discovery against the new URL
    elif "url" in kwargs and src and kwargs["url"] != src.get("url"):
        url_changed = True
        try:
            discovered = _run_discovery(kwargs["url"])
        except Exception as exc:
            log.warning("Discovery failed for new URL %s: %s", kwargs["url"], exc)
            discovered = None
        if discovered:
            disc_scraper, disc_url, disc_config = discovered
            log.info("Discovered new URL for %s: scraper=%s url=%s", src.get("name"), disc_scraper, disc_url)
            kwargs["scraper"] = disc_scraper
            kwargs["url"] = disc_url
            kwargs["config"] = disc_config or {}
            disc_result = {"scraper": disc_scraper, "url": disc_url}
        else:
            # Discovery found nothing — reset to html scraper so we don't try to parse the page as RSS.
            # For obvious feed-extension URLs keep rss; for everything else default to html.
            new_url = kwargs["url"]
            fallback = "rss" if any(new_url.lower().endswith(e) for e in (".xml", ".rss", ".atom")) else "html"
            kwargs["scraper"] = fallback
            kwargs["config"] = {}
            log.warning("No feed auto-detected at %s — defaulting to scraper=%s", new_url, fallback)
        # Clear previous fetch error when the URL changes (fresh start)
        kwargs["last_fetch_error"] = None

    if kwargs:
        import sqlite3 as _sqlite3
        try:
            db.update_source(source_uuid, **kwargs)
        except _sqlite3.IntegrityError:
            # Discovered feed URL already exists as another source — keep original URL
            kwargs.pop("url", None)
            if kwargs:
                db.update_source(source_uuid, **kwargs)
    if was_pseudo or url_changed:
        threading.Thread(target=_run_fetch, args=([source_uuid],), daemon=True).start()
    return {"ok": True, "discovered": disc_result}


@app.delete("/api/sources/{source_uuid}", status_code=204)
def remove_source(source_uuid: str, delete_content: bool = False, reject_domain: bool = False):
    if reject_domain:
        src = next((s for s in db.get_sources() if s["uuid"] == source_uuid), None)
        if src:
            from urllib.parse import urlparse as _urlparse
            host = _urlparse(src["url"]).hostname or ""
            if host:
                db.add_rejected_domain(host)
    article_int_ids = db.delete_source(source_uuid)
    if delete_content:
        for aid in article_int_ids:
            p = _cache_path(aid)
            if os.path.exists(p):
                os.remove(p)


@app.get("/api/rejected-domains")
def list_rejected_domains():
    return db.get_rejected_domains()


@app.delete("/api/rejected-domains/{domain}", status_code=204)
def unreject_domain(domain: str):
    db.remove_rejected_domain(domain)


# ── Articles ──────────────────────────────────────────────────────────────────

class BulkStatus(BaseModel):
    uuids: list[str]
    status: str


@app.get("/api/articles")
def list_articles(
    status: str = Query("all"),
    source_uuids: str = Query(""),
    q: str = Query(""),
    since: str = Query("all"),
    limit: int = Query(500, le=2000),
    offset: int = Query(0),
    dl_error: bool = Query(False),
    source_q: str = Query(""),
    title_q: str = Query(""),
    url_q: str = Query(""),
    noisy: bool = Query(False),
):
    uuid_list = [x.strip() for x in source_uuids.split(",") if x.strip()]
    articles, total = db.get_articles(
        status=status if status != "all" else None,
        source_uuids=uuid_list or None,
        q=q or None,
        since=since if since != "all" else None,
        limit=limit,
        offset=offset,
        noisy=noisy,
        dl_error=dl_error,
        source_q=source_q or None,
        title_q=title_q or None,
        url_q=url_q or None,
    )
    return {"articles": articles, "total": total}


@app.post("/api/articles/bulk-status")
def bulk_status(body: BulkStatus):
    if body.status not in ("new", "seen", "has_scenario"):
        raise HTTPException(400, "invalid status")
    db.set_articles_status(body.uuids, body.status)
    return {"ok": True}


# ── Article content ───────────────────────────────────────────────────────────

@app.get("/api/articles/{article_uuid}/content")
def get_content(article_uuid: str, force: bool = False):
    article = db.get_article(article_uuid)
    if not article:
        raise HTTPException(404, "not found")

    path = _cache_path(article["id"])

    source = db.get_source(article["source_id"]) or {}
    cfg = source.get("config") or {}

    def _effective_rule() -> dict | None:
        """URL-specific site_rule takes priority; fall back to source-level rule_start/rule_end."""
        r = db.rule_for_url(article["url"])
        if r:
            return r
        rs = cfg.get("rule_start", "")
        re_ = cfg.get("rule_end", "")
        if rs or re_:
            return {"id": None, "pattern": "", "rule_start": rs, "rule_end": re_, "source_level": True}
        return None

    if not force and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            md = f.read()
        return {
            "markdown": md,
            "url": article["url"],
            "cached_at": article.get("cached_at"),
            "from_cache": True,
            "rule": _effective_rule(),
        }

    date_selector  = cfg.get("date_selector", "")
    date_format    = cfg.get("date_format", "")
    date_url_format = cfg.get("date_url_format", "")
    download_ua    = cfg.get("download_ua", "")
    block_detect   = db._parse_ioc_patterns(cfg.get("block_detect", ""))

    try:
        md, extracted_date = _download_content(article["url"], date_selector, date_format,
                                               download_ua, block_detect)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        db.set_article_download_status(article_uuid, code)
        raise HTTPException(502, f"Download failed: {exc}")
    except Exception as exc:
        db.set_article_download_status(article_uuid, 0)
        raise HTTPException(502, f"Download failed: {exc}")

    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    db.set_article_cached(article_uuid)
    db.set_article_download_status(article_uuid, 200)
    db.index_article_content(article["id"], md)
    if not extracted_date and date_url_format:
        from fetcher import _date_from_url
        extracted_date = _date_from_url(article["url"], date_url_format)
    if extracted_date:
        db.update_article_date(article_uuid, extracted_date)

    return {
        "markdown": md,
        "url": article["url"],
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "from_cache": False,
        "rule": _effective_rule(),
    }


# ── Fetch job ─────────────────────────────────────────────────────────────────

class FetchRequest(BaseModel):
    source_uuids: Optional[list[str]] = None


@app.post("/api/fetch")
def start_fetch(body: FetchRequest = FetchRequest()):
    with _job_lock:
        if _job.running:
            raise HTTPException(409, "Fetch already running")
        _job.running = True

    threading.Thread(target=_run_fetch, args=(body.source_uuids,), daemon=True).start()
    return {"ok": True}


@app.get("/api/fetch/status")
def fetch_status():
    with _job_lock:
        return {
            "running": _job.running,
            "total": _job.total,
            "done": _job.done,
            "errors": _job.errors,
            "new_articles": _job.new_articles,
            "current": _job.current,
            "started": _job.started,
            "finished": _job.finished,
            "timed_out": list(_job.timed_out),
            "in_flight": list(_job.in_flight),
        }


# ── Prefetch (bulk offline content download) ──────────────────────────────────

class PrefetchRequest(BaseModel):
    source_uuids: Optional[list[str]] = None


@app.post("/api/prefetch")
def start_prefetch(body: PrefetchRequest = PrefetchRequest()):
    with _prefetch_lock:
        if _prefetch_job.running:
            raise HTTPException(409, "Prefetch already running")
        _prefetch_job.running = True
    threading.Thread(target=_run_prefetch, args=(body.source_uuids,), daemon=True).start()
    return {"ok": True}


@app.get("/api/prefetch/status")
def get_prefetch_status():
    with _prefetch_lock:
        return {
            "running": _prefetch_job.running,
            "total": _prefetch_job.total,
            "done": _prefetch_job.done,
            "errors": _prefetch_job.errors,
            "succeeded": _prefetch_job.succeeded,
            "current": _prefetch_job.current,
            "started": _prefetch_job.started,
            "finished": _prefetch_job.finished,
        }


@app.get("/api/prefetch/log")
def get_prefetch_log():
    entries = db.get_prefetch_errors()
    return {"entries": entries, "total": len(entries)}


# ── Backfill dates ────────────────────────────────────────────────────────────

def _fetch_date_only(url: str, date_selector: str, date_format: str, date_url_format: str) -> str | None:
    """Lightweight fetch to extract publication date only — no caching."""
    if date_url_format:
        from fetcher import _date_from_url
        d = _date_from_url(url, date_url_format)
        if d:
            return d
    if not date_selector:
        return None
    try:
        r = requests.get(url, headers=FETCH_HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        return _extract_date(soup, date_selector, date_format)
    except Exception as exc:
        log.debug("_fetch_date_only %s: %s", url[:60], exc)
        return None


_BACKFILL_WORKERS = 8


def _backfill_dates_task(source_uuid: str):
    source = next((s for s in db.get_sources() if s["uuid"] == source_uuid), None)
    if not source:
        return
    cfg = source.get("config") or {}
    date_sel = cfg.get("date_selector", "")
    date_fmt = cfg.get("date_format", "")
    date_url_fmt = cfg.get("date_url_format", "")
    if not date_sel and not date_url_fmt:
        log.info("Backfill dates: no date_selector configured for %s", source["name"])
        return

    arts, _ = db.get_articles(source_uuids=[source_uuid], limit=5000)
    targets = [a for a in arts if not a.get("published_at")]
    if not targets:
        log.info("Backfill dates: all articles already have dates for %s", source["name"])
        return

    log.info("Backfill dates: fetching %d articles for %s", len(targets), source["name"])
    updated = 0

    def _one(a):
        d = _fetch_date_only(a["url"], date_sel, date_fmt, date_url_fmt)
        if d:
            db.update_article_date(a["uuid"], d)
            return 1
        return 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=_BACKFILL_WORKERS) as pool:
        for result in concurrent.futures.as_completed(
            {pool.submit(_one, a): a for a in targets}
        ):
            try:
                updated += result.result()
            except Exception:
                pass

    log.info("Backfill dates done: %d/%d updated for %s", updated, len(targets), source["name"])


@app.post("/api/sources/{source_uuid}/backfill-dates")
def backfill_dates(source_uuid: str):
    source = next((s for s in db.get_sources() if s["uuid"] == source_uuid), None)
    if not source:
        raise HTTPException(404, "source not found")
    arts, _ = db.get_articles(source_uuids=[source_uuid], limit=5000)
    pending = sum(1 for a in arts if not a.get("published_at"))
    threading.Thread(target=_backfill_dates_task, args=(source_uuid,), daemon=True).start()
    return {"ok": True, "pending": pending}


# ── Sync scenarios ────────────────────────────────────────────────────────────

@app.post("/api/sync-scenarios")
def sync_scenarios():
    content_path = db.get_setting("content_path")
    return db.sync_scenarios(content_path)


# ── Site rules ────────────────────────────────────────────────────────────────

class RuleUpsert(BaseModel):
    pattern: str
    rule_start: str = ""
    rule_end: str = ""


@app.get("/api/rules")
def list_rules():
    return db.get_site_rules()


@app.get("/api/rules/match")
def match_rule(url: str = Query(...)):
    return db.rule_for_url(url)


@app.put("/api/rules")
def upsert_rule(body: RuleUpsert):
    return db.upsert_rule(body.pattern, body.rule_start, body.rule_end)


@app.delete("/api/rules/{rule_id}", status_code=204)
def remove_rule(rule_id: int):
    db.delete_rule(rule_id)


@app.post("/api/rules/import-termbrowser")
def import_termbrowser():
    """Re-import from ~/.config/termbrowser/config.toml (merge, no overwrite)."""
    path = os.path.expanduser("~/.config/termbrowser/config.toml")
    if not os.path.exists(path):
        raise HTTPException(404, "~/.config/termbrowser/config.toml not found")
    try:
        import tomllib
    except ImportError:
        raise HTTPException(500, "tomllib not available (Python 3.11+ required)")
    with open(path, "rb") as f:
        data = tomllib.load(f)
    count = 0
    for pattern, rule in data.get("sites", {}).items():
        db.upsert_rule(pattern, rule.get("start", ""), rule.get("end", ""))
        count += 1
    return {"imported": count}


# ── Settings ──────────────────────────────────────────────────────────────────

class SettingsPatch(BaseModel):
    content_path: Optional[str] = None
    ioc_exclude_global: Optional[str] = None
    auto_refresh_interval: Optional[int] = None


@app.get("/api/settings")
def get_settings():
    return db.get_all_settings()


@app.patch("/api/settings")
def patch_settings(body: SettingsPatch):
    for key, val in body.model_dump().items():
        if val is not None:
            db.set_setting(key, val)
    return db.get_all_settings()


# ── Warning lists ─────────────────────────────────────────────────────────────

@app.post("/api/warninglists/sync")
def sync_warninglists():
    try:
        return db.sync_warninglists()
    except Exception as exc:
        raise HTTPException(502, f"Sync failed: {exc}")


@app.get("/api/warninglists/stats")
def warninglist_stats():
    return db.get_warninglist_stats()


# ── IOCs ──────────────────────────────────────────────────────────────────────

def _ioc_exclude_patterns(article: dict) -> list[str]:
    """Combine global setting + per-source config into a flat exclusion list."""
    global_raw = db.get_setting("ioc_exclude_global", "")
    global_pats = db._parse_ioc_patterns(global_raw)

    sources = db.get_sources()
    src = next((s for s in sources if s["uuid"] == article.get("source_uuid")), None)
    src_pats = db._parse_ioc_patterns((src or {}).get("config", {}).get("ioc_exclude", []))

    return global_pats + src_pats


@app.get("/api/articles/{article_uuid}/iocs")
def get_iocs(article_uuid: str, apply_wl: bool = Query(True)):
    article = db.get_article(article_uuid)
    if not article:
        raise HTTPException(404, "not found")

    src = db.get_source(article["source_id"]) or {}
    src_cfg = src.get("config") or {}
    path = _cache_path(article["id"])
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            md = f.read()
    else:
        try:
            md, _ = _download_content(article["url"],
                                      user_agent=src_cfg.get("download_ua", ""),
                                      block_detect=db._parse_ioc_patterns(src_cfg.get("block_detect", "")))
        except Exception as exc:
            raise HTTPException(502, f"Download failed: {exc}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        db.set_article_cached(article_uuid)
        db.index_article_content(article["id"], md)

    rule = db.rule_for_url(article["url"])
    rule_start = rule["rule_start"] if rule else src_cfg.get("rule_start", "")
    rule_end   = rule["rule_end"]   if rule else src_cfg.get("rule_end",   "")
    iocs = db.extract_iocs(md, rule_start=rule_start, rule_end=rule_end)
    exclude = _ioc_exclude_patterns(article)
    iocs, filtered_count = db.filter_iocs(
        iocs, exclude_patterns=exclude, article_url=article["url"],
        apply_warninglists=apply_wl,
    )
    return {**iocs, "filtered_count": filtered_count, "wl_applied": apply_wl}


# ── Scenario viewer ───────────────────────────────────────────────────────────

@app.get("/api/articles/{article_uuid}/scenarios")
def get_scenarios(article_uuid: str):
    article = db.get_article(article_uuid)
    if not article:
        raise HTTPException(404, "not found")
    content_path = db.get_setting("content_path")
    if not content_path or not os.path.isdir(content_path):
        raise HTTPException(404, "content_path not configured or missing")
    url = article["url"]
    results = []
    for path in glob.glob(os.path.join(content_path, "**", "*.adel"), recursive=True):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            if url in text:
                results.append({
                    "path": os.path.relpath(path, content_path),
                    "content": text,
                })
        except OSError:
            pass
    return {"scenarios": results}


# ── MISP event export ─────────────────────────────────────────────────────────

_MISP_IOC_MAP = [
    ("ips",     "ip-dst",       "Network activity",  True),
    ("domains", "domain",       "Network activity",  True),
    ("urls",    "url",          "External analysis", False),
    ("sha256",  "sha256",       "Payload delivery",  True),
    ("sha1",    "sha1",         "Payload delivery",  True),
    ("md5",     "md5",          "Payload delivery",  True),
    ("cves",    "vulnerability","External analysis", False),
]


@app.get("/api/articles/{article_uuid}/misp-event")
def get_misp_event(article_uuid: str, apply_wl: bool = Query(True)):
    from pymisp import MISPEvent

    article = db.get_article(article_uuid)
    if not article:
        raise HTTPException(404, "not found")

    src = db.get_source(article["source_id"]) or {}
    src_cfg = src.get("config") or {}
    path = _cache_path(article["id"])
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            md = f.read()
    else:
        try:
            md, _ = _download_content(article["url"],
                                      user_agent=src_cfg.get("download_ua", ""),
                                      block_detect=db._parse_ioc_patterns(src_cfg.get("block_detect", "")))
        except Exception as exc:
            raise HTTPException(502, f"Download failed: {exc}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        db.set_article_cached(article_uuid)
        db.index_article_content(article["id"], md)

    rule = db.rule_for_url(article["url"])
    rule_start = rule["rule_start"] if rule else src_cfg.get("rule_start", "")
    rule_end   = rule["rule_end"]   if rule else src_cfg.get("rule_end",   "")
    iocs = db.extract_iocs(md, rule_start=rule_start, rule_end=rule_end)
    exclude = _ioc_exclude_patterns(article)
    iocs, _ = db.filter_iocs(
        iocs, exclude_patterns=exclude, article_url=article["url"],
        apply_warninglists=apply_wl,
    )

    event = MISPEvent()
    event.info = article.get("title") or article["url"]
    event.distribution = 0
    event.threat_level_id = 2
    event.analysis = 1
    if article.get("published_at"):
        try:
            event.date = article["published_at"][:10]
        except Exception:
            pass

    event.add_attribute(
        "link", article["url"],
        category="External analysis", to_ids=False,
        comment=f"Source: {article.get('source_name', '')}",
    )

    for key, ioc_type, category, to_ids in _MISP_IOC_MAP:
        for val in iocs.get(key, []):
            event.add_attribute(ioc_type, val, category=category, to_ids=to_ids)

    event.add_tag("tlp:white")

    slug = re.sub(r"[^\w-]", "-", (article.get("title") or article_uuid[:8])[:50]).strip("-")
    filename = f"misp-event-{slug}.json"

    return Response(
        content=event.to_json(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def stats():
    return db.get_stats()


# ── Favicon cache ────────────────────────────────────────────────────────────

_DOMAIN_RE = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$')

@app.get("/api/favicon")
def get_favicon(domain: str = Query(...)):
    if not _DOMAIN_RE.match(domain):
        raise HTTPException(400, "invalid domain")
    cache_path = os.path.join(FAVICON_DIR, f"{domain}.png")
    none_path  = os.path.join(FAVICON_DIR, f"{domain}.none")
    if os.path.exists(none_path):
        return Response(status_code=204)
    if not os.path.exists(cache_path):
        try:
            r = requests.get(
                f"https://www.google.com/s2/favicons?domain={domain}&sz=16",
                headers=FETCH_HEADERS, timeout=5,
            )
            r.raise_for_status()
            with open(cache_path, "wb") as f:
                f.write(r.content)
        except Exception:
            open(none_path, "w").close()  # cache the miss so we don't retry
            return Response(status_code=204)
    return FileResponse(cache_path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


# ── Static ────────────────────────────────────────────────────────────────────

_static = os.path.join(_HERE, "static")
app.mount("/static", StaticFiles(directory=_static), name="static")


@app.get("/")
def root():
    return FileResponse(os.path.join(_static, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=7474, reload=False)
