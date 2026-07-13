# Fetching strategies

> For the JSON schema used to create/import feeds (every config key, with examples),
> see [FEED_CONFIG.md](FEED_CONFIG.md).

ThreatBrowser fetches content in two distinct phases:

1. **Article discovery** — scanning a source URL to find article links and metadata (title, date)
2. **Article content download** — fetching the full text of a single article on demand

---

## Article discovery

Sources are fetched in parallel (`ThreadPoolExecutor`, up to 20 workers) on demand via `POST /api/fetch`.

### RSS / Atom (scraper: `rss`)

Uses `feedparser` on top of a plain `requests` GET.

**Why a separate RSS User-Agent?**  
RSS endpoints are consumed by bots and many publishers actively block browser-fingerprint User-Agents. `fetcher.py` uses a neutral `RSS_HEADERS` dict by default:

```
User-Agent: Mozilla/5.0 (compatible; Feedfetcher/1.0; +https://github.com/detecteam)
Accept: application/rss+xml, application/atom+xml, application/xml, text/xml, */*;q=0.1
```

Per-source override: set `user_agent` in the source config JSON to use a different UA for a specific feed. Example (CyberSecurityNews requires the feedparser UA):

```json
{ "user_agent": "python-feedparser/6.0.11 +https://github.com/kurtmckee/feedparser" }
```

**Config keys:**

| Key | Description |
|-----|-------------|
| `filter_url_contains` | Only keep entries whose link URL contains this string |
| `filter_tag_contains` | Only keep entries that have a tag matching this string |
| `user_agent` | Override the default RSS User-Agent |
| `date_url_format` | strftime pattern to extract a date from the article URL (see below) |

### HTML scraping (scraper: `html`)

When RSS is unavailable or capped (e.g. 10 entries), use the HTML scraper. It fetches the listing page with a browser-like User-Agent (`HEADERS` in `fetcher.py`) and extracts links via CSS selectors.

**Config keys:**

| Key | Default | Description |
|-----|---------|-------------|
| `container_selector` | — | Outer element per article card. If set, `link_selector` and `card_date_selector` are scoped inside it. |
| `link_selector` | `a[href]` | `<a>` element containing the article URL |
| `title_selector` | — | Element containing the article title (relative to container) |
| `card_date_selector` | — | Date element within the card (requires `container_selector`) |
| `card_date_format` | — | strftime format for parsing card date text. Omit to auto-detect. |
| `link_prefix` | — | Prepended to relative article URLs |
| `filter_url_contains` | — | Only keep URLs containing this string |
| `exclude_url` | — | Skip this exact URL |
| `filter_url_regex` | — | Only keep URLs matching this regex |
| `title_from_url` | `false` | Derive title from the URL slug instead of link text |
| `paginate` | `false` | Follow next-page links until exhausted |
| `next_selector` | `a.pagination-next, a[rel=next]` | CSS selector(s) for the next-page link (comma-separated, tried in order) |
| `max_pages` | `0` (unlimited) | Stop after this many pages |
| `date_url_format` | — | strftime pattern to extract a date from the article URL |

**Pagination flow:**

```
fetch page N
  → extract links
  → find next_selector element
  → resolve href (urljoin for relative URLs)
  → if href == current page URL → stop
  → else → fetch page N+1
```

**Date extraction from listing pages:**

`card_date_selector` extracts a date from each article card on the listing page — no content download needed. It checks:
1. `datetime` attribute on the element (ISO 8601 / `<time>` tag)
2. Text content, with auto-normalization for abbreviated months like `Dec. 22, 2025` → `Dec 22, 2025`
3. Auto-detection across common formats: `%Y-%m-%d`, `%B %d, %Y`, `%d %B %Y`, `%b %d, %Y`, `%d %b %Y`, `%Y/%m/%d`

### Date from URL (`date_url_format`)

Works for both RSS and HTML scrapers. Uses a strftime-like pattern matched against the article URL with a regex:

```json
{ "date_url_format": "/%Y/%m/%d/" }
```

This is zero-cost — no extra HTTP request — but requires the date to appear in the URL path.

---

## Article content download

Triggered on demand when a user opens an article. The result is cached as Markdown in `cache/<int_id>.md` and never re-fetched unless `?force=true`.

### Three-tier fallback strategy

```
requests (HTTP/1.1, Chrome UA)
    ↓ 403 or timeout
httpx (HTTP/2, different TLS fingerprint)
    ↓ 403 or timeout
playwright (headless Chromium — always works, slow)
```

**Why three tiers?**

- **`requests`**: Fast, zero overhead. Fails against bot-detection middleware (Akamai, Cloudflare) that inspects the TLS fingerprint (JA3/JA4 hash). The Chrome UA paired with Python's TLS stack produces a mismatch.
- **`httpx` with HTTP/2**: Uses a different TLS client implementation (`h2`), producing a distinct fingerprint that bypasses many Akamai rules. Same UA, different handshake. Also handles sites that reject HTTP/1.1.
- **`playwright` Chromium**: A real browser. TLS fingerprint matches the Chrome UA exactly. Runs JavaScript, handles `<noscript>` walls, Cloudflare Turnstile challenges that don't require user interaction. Slowest option (~5–10 s per page). Requires `playwright install chromium`.

**Note on 403 retry in requests:**  
A plain `requests` 403 is first retried with a minimal Wget UA (`Wget/1.21.4`) before escalating, in case the site blocks decorative headers rather than TLS.

### Date extraction during content download

If the source has `date_selector` configured, the date is extracted from the article page HTML *before* stripping layout elements (nav, header, footer), since publication dates often appear there.

| Config key | Description |
|------------|-------------|
| `date_selector` | CSS selector for the date element inside the article |
| `date_format` | strftime format for parsing the date text. Omit to auto-detect. |
| `date_url_format` | strftime pattern in the URL (no HTTP request needed) |

Date parsing normalises abbreviated months with trailing periods (`Dec.` → `Dec`) before attempting all known formats.

### Backfill dates

For articles already stored without a date, `POST /api/sources/{uuid}/backfill-dates` fetches all null-date articles in parallel (8 workers) using a lightweight path that only reads the date element — no Markdown conversion, no cache write. Requires `date_selector` or `date_url_format` in the source config.

---

## Common problems and workarounds

### Bot detection

| Symptom | Cause | Fix |
|---------|-------|-----|
| 403 on RSS feed | Site blocks browser-fingerprint UA on feed endpoint | Use `user_agent` config to set a bot-friendly UA |
| 403 on article page (requests) | Akamai/Cloudflare TLS fingerprint check | httpx HTTP/2 fallback handles most cases |
| 403 on article page (httpx) | Cloudflare JS challenge or stricter fingerprinting | playwright fallback |
| Blank page / `<noscript>` content only | JS-rendered page | playwright renders JS before returning HTML |

### RSS feed issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| Only 10–20 entries, no history | Feed publisher caps entry count | Switch to `html` scraper with `paginate: true` |
| Feed URL returns 404 | Domain changed or path restructured | Update source URL |
| Feed URL times out | DNS/CDN migration (e.g. `news.` subdomain retired) | Update to current canonical URL |

### HTML scraper issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| Pagination loops back to page 1 | `next_selector` matches a "back" or "first" link | Use a more specific selector or pair with `filter_url_regex` to skip already-visited URLs |
| No dates on listing page | Dates only appear on article pages | Use `date_selector` + trigger backfill, or `date_url_format` if date is in the URL |
| Titles too short / generic | Link text is an icon or image | Set `title_from_url: true` to derive title from URL slug |

### Playwright not available

If `playwright` is not installed, the two-tier strategy (`requests` → `httpx`) is used and a `RuntimeError` is raised only when both fail. Install with:

```
pip install playwright
playwright install chromium
```

### "Could not find a suitable TLS CA certificate bundle"

Seen as a fetch error on every source at once, pointing at a path like
`/var/folders/.../T/_MEIejcVr4/certifi/cacert.pem`.

The packaged server is a PyInstaller `--onefile` binary, which unpacks its data files into
`$TMPDIR/_MEIxxxx`. macOS periodically deletes files under `/var/folders/.../T/` that have not
been accessed for a few days, so a server left running for a week loses the CA bundle certifi
still points at, and every HTTPS request fails.

Three things prevent this now:

- `certs.install()` (see [certs.py](../certs.py)) copies the bundle out of the temp dir into
  `<data dir>/certs/cacert.pem` at startup and repoints certifi, requests, httpx and curl_cffi
  at that copy. It also keeps the bytes in memory, so the file can be recreated even after the
  temp dir is gone.
- The Tauri launcher sets `TMPDIR` to `<data dir>/tmp`, so `_MEIxxxx` itself is no longer
  unpacked anywhere macOS purges.
- A fetch that still hits the error triggers `certs.repair()` and retries once. If it persists,
  **Settings → TLS certificates → Repair** rewrites the bundle and clears the errors it caused.

---

## Config example: paginated HTML source with card dates

```json
{
  "container_selector": "article.post",
  "link_selector": "h2 a",
  "card_date_selector": "span.post-date",
  "link_prefix": "https://example.com",
  "filter_url_contains": "/blog/",
  "paginate": true,
  "next_selector": "a.next-page",
  "max_pages": 10
}
```

## Config example: RSS with date from URL

```json
{
  "date_url_format": "/%Y/%m/%d/",
  "filter_url_contains": "/threat-research/"
}
```

## Config example: article date from page content

```json
{
  "date_selector": "time.published",
  "date_format": "%B %d, %Y"
}
```
