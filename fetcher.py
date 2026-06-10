import re
import time
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse as _urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as _cffi_requests
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

log = logging.getLogger(__name__)

HEADERS = {
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

# RSS feeds are consumed by bots; many block browser-fingerprint UAs.
RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Feedfetcher/1.0; +https://github.com/detecteam)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*;q=0.1",
    "Accept-Encoding": "gzip, deflate",  # no br — Brotli causes decompression failures on some feeds
}


_URL_FORMAT_MAP = {
    '%Y': r'(\d{4})', '%m': r'(\d{2})', '%d': r'(\d{2})',
    '%H': r'(\d{2})', '%M': r'(\d{2})', '%S': r'(\d{2})',
}

def _date_from_url(url: str, url_format: str) -> str | None:
    """Extract a date from the URL using a strftime-like format string."""
    pattern = url_format
    for code, rx in _URL_FORMAT_MAP.items():
        pattern = pattern.replace(code, rx)
    m = re.search(pattern, url)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), url_format).replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _entry_date(entry) -> str | None:
    for attr in ("published", "updated"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return parsedate_to_datetime(val).astimezone(timezone.utc).isoformat()
            except Exception:
                return val
    return None


def _title_from_url(url: str) -> str:
    """Extract a readable title from the last URL path segment."""
    path = url.rstrip("/").rsplit("/", 1)[-1]
    return path.replace("-", " ").replace("_", " ").title()


def _url_ok(url: str, cfg: dict) -> bool:
    if cfg.get("filter_url_contains") and cfg["filter_url_contains"] not in url:
        return False
    if cfg.get("exclude_url") and url == cfg["exclude_url"]:
        return False
    if cfg.get("filter_url_regex") and not re.search(cfg["filter_url_regex"], url):
        return False
    return True


# ── RSS scraper ───────────────────────────────────────────────────────────────

def fetch_rss(url: str, config: dict) -> list[dict]:
    """Generic RSS/Atom via feedparser with optional URL and tag filtering."""
    filter_url = config.get("filter_url_contains")
    filter_tag = config.get("filter_tag_contains")

    ua = config.get("user_agent")
    tls_imp = config.get("tls_impersonate", "")  # e.g. "firefox", "chrome"

    def _build_results(feed):
        results = []
        for e in feed.entries:
            if not hasattr(e, "link"):
                continue
            if filter_url and filter_url not in e.link:
                continue
            if filter_tag:
                tags = getattr(e, "tags", [])
                if not any(filter_tag in getattr(t, "term", "") for t in tags):
                    continue
            results.append({
                "title": getattr(e, "title", None),
                "url": e.link,
                "published_at": _entry_date(e),
            })
        return results

    # Fast path: tls_impersonate configured — skip request strategies entirely.
    if tls_imp:
        if not _HAS_CURL_CFFI:
            raise RuntimeError(
                f"tls_impersonate={tls_imp!r} is set but curl-cffi is not installed. "
                "Run: pip install curl-cffi"
            )
        log.info("RSS fetch %s: curl-cffi %s (configured)", url, tls_imp)
        r = _cffi_requests.get(url, impersonate=tls_imp, timeout=20)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        if not feed.version and not feed.entries:
            bozo = getattr(feed, "bozo_exception", None)
            msg = f"Not a valid RSS/Atom feed: {bozo}" if bozo else "Not a valid RSS/Atom feed"
            log.warning("RSS parse %s: %s", url, msg)
            raise ValueError(msg)
        return _build_results(feed)

    # Normal path: try request-based strategies then curl-cffi then Playwright.
    first_headers = dict(RSS_HEADERS)
    if ua:
        first_headers["User-Agent"] = ua

    _GOOGLEBOT = {
        **RSS_HEADERS,
        "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    }
    # Strip Brotli from browser headers when used for RSS — servers may send Brotli
    # which can fail to decompress reliably; gzip is sufficient for small XML feeds.
    _CHROME_RSS = {**HEADERS, "Accept-Encoding": "gzip, deflate"}
    # Timeouts are deliberately short — a site that's going to block us does so
    # immediately; spending 10 s per attempt just delays the curl-cffi fallback.
    _STRATEGIES = [
        (first_headers, 8),
        (_CHROME_RSS,    6),
        (_GOOGLEBOT,     8),
    ]
    _RETRY_STATUSES = (403, 429, 503)
    last_exc: Exception | None = None
    feed: feedparser.FeedParserDict | None = None
    r = None  # last successful HTTP response (may be None if all strategies threw)

    for i, (hdrs, timeout) in enumerate(_STRATEGIES):
        try:
            r = requests.get(url, headers=hdrs, timeout=timeout)
            r.raise_for_status()
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as exc:
            log.info("RSS fetch %s strategy %d timed out: %s", url, i, exc)
            last_exc = exc
            continue
        except requests.exceptions.HTTPError as exc:
            sc = exc.response.status_code if exc.response is not None else 0
            if sc in _RETRY_STATUSES:
                log.info("RSS fetch %s strategy %d got %d, trying next UA", url, i, sc)
                last_exc = exc
                continue
            raise

        parsed = feedparser.parse(r.content)
        if parsed.version or parsed.entries:
            feed = parsed
            break

        ct = r.headers.get("content-type", "").lower()
        bozo_exc = getattr(parsed, "bozo_exception", None)
        log.info("RSS fetch %s strategy %d got non-feed response (%s), trying next",
                 url, i, ct or str(bozo_exc))
        last_exc = ValueError(f"Not a valid RSS/Atom feed: {bozo_exc}" if bozo_exc
                              else "Not a valid RSS/Atom feed (HTML response)")

    # HTML feed-link extraction — when the URL returned an HTML page instead of a
    # feed, parse it for <link rel="alternate" type="application/rss+xml"> and retry.
    # Handles sites where /blog/feed/ serves the blog homepage but <link> points to
    # the real feed (e.g. /feed/).
    if feed is None and r is not None and r.content:
        ct = getattr(r, "headers", {}).get("content-type", "").lower()
        if "html" in ct:
            try:
                soup = BeautifulSoup(r.content, "html.parser")
                link_el = (
                    soup.find("link", {"type": "application/rss+xml"}) or
                    soup.find("link", {"type": "application/atom+xml"})
                )
                alternate_url = link_el.get("href") if link_el else None
                if alternate_url:
                    if not alternate_url.startswith("http"):
                        from urllib.parse import urljoin as _urljoin
                        alternate_url = _urljoin(url, alternate_url)
                    if alternate_url != url:
                        log.info("RSS fetch %s: HTML page has feed link %s, retrying", url, alternate_url)
                        r2 = requests.get(alternate_url, headers=first_headers, timeout=12)
                        r2.raise_for_status()
                        parsed2 = feedparser.parse(r2.content)
                        if parsed2.version or parsed2.entries:
                            feed = parsed2
                            log.info("RSS fetch %s: feed found via HTML link tag at %s", url, alternate_url)
            except Exception as exc:
                log.debug("RSS fetch %s HTML link extraction failed: %s", url, exc)

    # curl-cffi fallback — Firefox TLS fingerprint bypasses Akamai/Cloudflare checks
    if feed is None:
        if _HAS_CURL_CFFI:
            log.info("RSS fetch %s: trying curl-cffi Firefox TLS impersonation", url)
            try:
                r = _cffi_requests.get(url, impersonate="firefox", timeout=20)
                r.raise_for_status()
                parsed = feedparser.parse(r.content)
                if parsed.version or parsed.entries:
                    feed = parsed
                else:
                    bozo = getattr(parsed, "bozo_exception", None)
                    last_exc = ValueError(f"Not a valid RSS/Atom feed: {bozo}" if bozo
                                          else f"curl-cffi got non-feed response")
            except Exception as exc:
                log.info("RSS fetch %s curl-cffi failed: %s", url, exc)
                last_exc = exc
        else:
            log.warning(
                "RSS fetch %s: curl-cffi not installed — install with: pip install curl-cffi",
                url,
            )

    # Playwright fallback — last resort for JS challenges
    if feed is None:
        if _HAS_PLAYWRIGHT:
            log.info("RSS fetch %s: trying Playwright fallback", url)
            try:
                with _sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True, args=["--disable-http2"])
                    try:
                        page = browser.new_page()
                        resp = page.goto(url, timeout=20000, wait_until="commit")
                        raw = resp.body() if resp else b""
                    finally:
                        browser.close()
                if raw:
                    parsed = feedparser.parse(raw)
                    if parsed.version or parsed.entries:
                        feed = parsed
                    else:
                        bozo = getattr(parsed, "bozo_exception", None)
                        last_exc = ValueError(f"Not a valid RSS/Atom feed: {bozo}" if bozo
                                             else "Playwright got non-feed response")
            except Exception as exc:
                log.info("RSS fetch %s Playwright failed: %s", url, exc)
                last_exc = exc
        else:
            log.warning(
                "RSS fetch %s: all strategies failed and Playwright is not installed. "
                "Install with: pip install playwright && playwright install chromium",
                url,
            )

    if feed is None:
        msg = str(last_exc) if last_exc else "All fetch strategies failed"
        log.warning("RSS parse %s: %s", url, msg)
        raise (last_exc if isinstance(last_exc, Exception) else ValueError(msg))

    return _build_results(feed)


# ── HTML scraper ──────────────────────────────────────────────────────────────

def fetch_html(url: str, config: dict, known_urls: set[str] | None = None) -> list[dict]:
    """
    Generic HTML scraper driven by CSS selectors in config:
      container_selector  – outer element per article (optional)
      link_selector       – <a> element (relative to container, or absolute)
      title_selector      – title element (relative to container; optional)
      card_date_selector  – date element within the card (optional; requires container_selector)
      card_date_format    – strftime format for card date text (optional)
      link_prefix         – prepended to relative URLs
      filter_url_contains – only keep URLs containing this string
      exclude_url         – skip this exact URL
      filter_url_regex    – only keep URLs matching this regex
      title_from_url      – derive title from URL slug when link text is unhelpful
      paginate            – follow next-page links until exhausted (default: False)
      next_selector       – CSS selector(s) for the next-page link, comma-separated
                            (default: "a.pagination-next, a[rel=next]")
      max_pages           – stop after this many pages, 0 = unlimited (default: 0)
    """
    link_sel      = config.get("link_selector", "a[href]")
    title_sel     = config.get("title_selector")
    cont_sel      = config.get("container_selector")
    card_date_sel = config.get("card_date_selector")
    card_date_fmt = config.get("card_date_format", "")
    prefix        = config.get("link_prefix", "")
    from_url      = config.get("title_from_url", False)
    paginate      = config.get("paginate", False)
    next_sel      = config.get("next_selector", "a.pagination-next, a[rel=next]")
    max_pages     = int(config.get("max_pages", 0))

    seen: set[str] = set()
    results: list[dict] = []
    _known_hits = 0  # count of already-DB-known URLs seen during pagination

    # ── Domain allow-list ─────────────────────────────────────────────────────
    # Articles must be on the same registered domain as the source URL or
    # link_prefix.  This rejects ads, trackers, and cross-site links.
    _src_host  = _urlparse(url).netloc.lower()
    _pfx_host  = (_urlparse(prefix).netloc.lower()
                  if prefix and prefix.startswith("http") else "")
    _ok_hosts  = {h for h in (_src_host, _pfx_host) if h}

    def _reg_domain(host: str) -> str:
        """Return last-two-label registered domain (e.g. 'group-ib.com')."""
        parts = host.rstrip(".").split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    # Subdomains that never host blog articles
    _NOISE_SUBS = frozenset({
        "sso", "login", "auth", "signin", "accounts", "oauth", "id",
        "cdn", "static", "assets", "images", "img", "media",
        "mail", "email", "smtp", "api", "ads", "track", "analytics",
    })

    # First path-segment values that indicate site structure, not an article.
    # Prevents navigation menus, product pages, author profiles etc. from
    # being mistaken for blog posts.
    _SKIP_SEGS = frozenset({
        "author", "authors", "category", "categories", "tag", "tags",
        "wp-content", "wp-admin", "wp-json", "wp-includes",
        "search", "archive", "archives",
        "about", "about-us", "contact", "contact-us", "contacts",
        "privacy", "terms", "legal", "cookies", "cookie-policy",
        "sitemap", "rss", "atom", "feed", "feeds",
        "newsletter", "subscribe", "unsubscribe",
        "login", "logout", "register", "signin", "signup", "oauth",
        "products", "product", "solutions", "solution",
        "services", "service", "pricing", "plans", "features",
        "resources", "resource", "webinars", "webinar",
        "podcasts", "podcast", "events", "event",
        "landing", "faq", "support", "help", "docs", "documentation",
        "careers", "jobs", "press", "media", "media-center",
        "newsroom", "news-room", "press-room",
        "partners", "partner", "partner-program", "technology-partners",
        "shopping", "cart", "checkout",
        "cdn", "static", "assets", "images", "fonts",
        "team", "management", "leadership", "board",
        "sustainability", "esg", "investors", "investor-relations",
        "integrations", "integration", "marketplace",
        "academic", "academic-alliance", "education",
    })

    def _host_ok(art_host: str) -> bool:
        art_host = art_host.lower()
        # Exact match first (handles ccTLD+1 like jpcert.or.jp correctly)
        if art_host in _ok_hosts:
            return True
        # Same registered domain covers blog.example.com vs www.example.com
        art_rd = _reg_domain(art_host)
        return any(art_rd == _reg_domain(ok) for ok in _ok_hosts)
    # ─────────────────────────────────────────────────────────────────────────

    def _parse_card_date(container) -> str | None:
        if not card_date_sel or container is None:
            return None
        el = container.select_one(card_date_sel)
        if not el:
            return None
        dt_attr = el.get("datetime", "").strip()
        if dt_attr:
            try:
                return datetime.fromisoformat(dt_attr.replace("Z", "+00:00")).isoformat()
            except ValueError:
                pass
        text = el.get_text(strip=True)
        if not text:
            return None
        # Normalise "Dec. 22, 2025" → "Dec 22, 2025"
        norm = re.sub(r'\b([A-Za-z]{3,9})\.\s', r'\1 ', text)
        for t in (text, norm):
            if card_date_fmt:
                try:
                    return datetime.strptime(t, card_date_fmt).replace(tzinfo=timezone.utc).isoformat()
                except ValueError:
                    continue
            for fmt in ("%Y-%m-%d", "%B %d, %Y", "%d %B %Y", "%b %d, %Y", "%d %b %Y",
                    "%Y/%m/%d", "%d-%b-%Y", "%d-%b-%Y %H:%M:%S"):
                try:
                    return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc).isoformat()
                except ValueError:
                    continue
        return None

    _BAD_SCHEMES = ("javascript:", "mailto:", "tel:", "sms:", "callto:", "fax:",
                    "data:", "blob:", "vbscript:", "#")

    def process(container, link_el):
        nonlocal _known_hits
        href = link_el.get("href", "")
        if not href or href.startswith(_BAD_SCHEMES):
            return
        full = href if href.startswith("http") else f"{prefix}{href}"
        if not full.startswith(("http://", "https://")):
            return
        if "/cdn-cgi/" in full:
            return
        # Domain + path filtering
        try:
            _p = _urlparse(full)
            art_host = _p.netloc.lower()
            # Absolute links must be on the same registered domain
            if href.startswith("http") and not _host_ok(art_host):
                return
            # Skip noise subdomains (SSO portals, CDN, tracking)
            if art_host.split(".")[0] in _NOISE_SUBS:
                return
            # Skip structural site paths (navigation, products, authors, etc.)
            path_segs = [s for s in _p.path.split("/") if s]
            if path_segs and path_segs[0].lower() in _SKIP_SEGS:
                return
            # Skip root URL (listing page linked to itself)
            if not path_segs:
                return
        except Exception:
            return
        if full in seen or not _url_ok(full, config):
            return
        seen.add(full)
        if known_urls and full in known_urls:
            _known_hits += 1
        if title_sel and container is not None:
            tel = container.select_one(title_sel)
            title = tel.get_text(strip=True) if tel else None
        else:
            title = link_el.get_text(strip=True) or None
        if from_url or not title or len(title) < 8:
            title = _title_from_url(full)
        results.append({"title": title, "url": full, "published_at": _parse_card_date(container)})

    page_url = url
    page_num = 0

    while True:
        page_num += 1
        try:
            r = requests.get(page_url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.content, "html.parser")
        except requests.exceptions.RequestException as exc:
            if page_num == 1:
                raise  # first-page failure = source is broken; propagate to caller
            log.warning("HTML fetch %s: %s", page_url, exc)
            break
        except Exception as exc:
            log.warning("HTML fetch %s: %s", page_url, exc)
            break

        if cont_sel:
            for container in soup.select(cont_sel):
                link_el = container.select_one(link_sel) if link_sel else container
                if link_el:
                    process(container, link_el)
        else:
            for link_el in soup.select(link_sel):
                process(None, link_el)

        if not paginate:
            break
        if max_pages > 0 and page_num >= max_pages:
            break
        # Stop when we've hit enough already-known URLs — we've reached content
        # the DB already has, so further pages will only have older known articles.
        if known_urls and page_num >= 2 and _known_hits >= 3:
            log.info("HTML paginate %s: stopping at page %d (hit %d known URLs)", url, page_num, _known_hits)
            break

        # Find the next-page link (try each comma-separated selector in order)
        next_href = None
        for sel in (s.strip() for s in next_sel.split(",")):
            el = soup.select_one(sel)
            if el and el.get("href"):
                next_href = el["href"]
                break
        if not next_href:
            break

        next_full = next_href if next_href.startswith("http") else urljoin(page_url, next_href)
        if next_full == page_url:
            break
        log.info("HTML paginate %s → page %d", url, page_num + 1)
        page_url = next_full

    return results


# ── Sitemap scraper ───────────────────────────────────────────────────────────

def fetch_sitemap(url: str, config: dict) -> list[dict]:
    """
    Fetch all article URLs from an XML sitemap (or sitemap index).
    Supports the same filter_url_contains / filter_url_regex options as the HTML scraper.
    If the sitemap is a <sitemapindex>, each child sitemap is fetched recursively
    (one level deep to avoid infinite loops).

    Extra config options:
      sitemap_pattern  – regex; only follow child sitemaps whose URL matches
                         (e.g. "post-sitemap" to skip category/tag/author sitemaps)
    """
    import xml.etree.ElementTree as ET

    _NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
    sitemap_pat = config.get("sitemap_pattern", "")

    def _tag(name: str) -> str:
        return f"{{{_NS}}}{name}"

    def _parse(xml_bytes: bytes, base_url: str, recurse: bool = True) -> list[dict]:
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            log.warning("Sitemap parse error %s: %s", base_url, exc)
            return []

        # Sitemap index — recurse into child sitemaps (one level)
        if root.tag == _tag("sitemapindex") and recurse:
            results: list[dict] = []
            for sm in root.findall(_tag("sitemap")):
                loc_el = sm.find(_tag("loc"))
                if loc_el is None or not loc_el.text:
                    continue
                child_url = loc_el.text.strip()
                if sitemap_pat and not re.search(sitemap_pat, child_url):
                    continue
                try:
                    r2 = requests.get(child_url, headers=RSS_HEADERS, timeout=20)
                    r2.raise_for_status()
                    results.extend(_parse(r2.content, child_url, recurse=False))
                except Exception as exc:
                    log.warning("Sitemap child %s: %s", child_url, exc)
            return results

        # Regular sitemap — collect <url> entries
        entries: list[dict] = []
        for url_el in root.findall(_tag("url")):
            loc_el = url_el.find(_tag("loc"))
            if loc_el is None or not loc_el.text:
                continue
            article_url = loc_el.text.strip()
            if not _url_ok(article_url, config):
                continue

            # lastmod → published_at (optional; many sitemaps omit it)
            published_at: str | None = None
            lastmod_el = url_el.find(_tag("lastmod"))
            if lastmod_el is not None and lastmod_el.text:
                try:
                    raw = lastmod_el.text.strip()
                    if len(raw) == 10:
                        published_at = (
                            datetime.strptime(raw, "%Y-%m-%d")
                            .replace(tzinfo=timezone.utc)
                            .isoformat()
                        )
                    else:
                        published_at = datetime.fromisoformat(
                            raw.replace("Z", "+00:00")
                        ).isoformat()
                except Exception:
                    pass

            entries.append({
                "title": _title_from_url(article_url),
                "url": article_url,
                "published_at": published_at,
            })
        return entries

    # HTTP/connection errors propagate to the caller so they can be recorded.
    r = requests.get(url, headers=RSS_HEADERS, timeout=20)
    r.raise_for_status()
    try:
        return _parse(r.content, url)
    except Exception as exc:
        log.warning("Sitemap parse %s: %s", url, exc)
        raise


# ── Dispatch ──────────────────────────────────────────────────────────────────

def fetch_source(source: dict, known_urls: set[str] | None = None) -> list[dict]:
    cfg = source.get("config") or {}
    if isinstance(cfg, str):
        import json
        cfg = json.loads(cfg)

    scraper = source.get("scraper", "rss")
    if scraper == "rss":
        articles = fetch_rss(source["url"], cfg)
    elif scraper == "html":
        articles = fetch_html(source["url"], cfg, known_urls=known_urls)
    elif scraper == "sitemap":
        articles = fetch_sitemap(source["url"], cfg)
    else:
        log.warning("Unknown scraper %r for %s, trying rss", scraper, source["url"])
        articles = fetch_rss(source["url"], cfg)

    url_fmt = cfg.get("date_url_format", "")
    for a in articles:
        a["source_id"] = source["id"]
        if not a.get("published_at") and url_fmt:
            a["published_at"] = _date_from_url(a["url"], url_fmt)
    return articles
