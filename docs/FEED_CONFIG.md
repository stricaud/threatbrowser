# Feed configuration JSON

A feed (source) is fully described by a small JSON object. You can create feeds
from a JSON file instead of filling in the UI form — useful for sharing feed
definitions, version-controlling them, or bulk-adding many at once.

See [FETCHING.md](FETCHING.md) for how each scraper strategy actually works;
this document is the reference for the **config schema** itself.

---

## Top-level shape

A feed object has these fields:

| Field     | Type           | Required | Default | Description |
|-----------|----------------|----------|---------|-------------|
| `name`    | string         | **yes**  | —       | Display name of the feed. |
| `url`     | string         | **yes**  | —       | Source URL: an RSS/Atom feed, a sitemap, or an HTML listing page. The URL is unique — re-importing the same URL **updates** the existing feed. |
| `scraper` | string         | no       | `"rss"` | One of `rss`, `html`, `sitemap`. |
| `config`  | object         | no       | `{}`    | Scraper-specific settings (see below). |
| `tags`    | array<string>  | no       | `[]`    | Free-form tags for filtering/grouping. |
| `active`  | boolean        | no       | `true`  | Whether the feed is fetched. |

The smallest valid feed:

```json
{ "name": "Talos Intelligence", "url": "https://blog.talosintelligence.com/rss/" }
```

## Accepted file formats

The importer accepts any of three shapes, so a file can hold one feed or many:

```json
// 1. a single feed object
{ "name": "DFIR Report", "url": "https://thedfirreport.com/feed/" }
```

```json
// 2. a bare array of feeds
[
  { "name": "DFIR Report", "url": "https://thedfirreport.com/feed/" },
  { "name": "Securelist", "url": "https://securelist.com/feed/", "tags": ["apt"] }
]
```

```json
// 3. an export envelope (what the Export button produces)
{ "version": 1, "sources": [ { "name": "…", "url": "…" } ] }
```

---

## The `config` object

All keys are optional. Only the keys relevant to the chosen `scraper` are used;
unknown keys are ignored, so it is safe to keep a superset.

### Common (all scrapers)

| Key                    | Type    | Description |
|------------------------|---------|-------------|
| `filter_url_contains`  | string  | Keep only article URLs containing this substring. |
| `filter_url_regex`     | string  | Keep only article URLs matching this regex. |
| `exclude_url`          | string  | Drop article URLs containing this substring. |
| `tls_impersonate`      | string  | TLS fingerprint to impersonate via curl-cffi, e.g. `"firefox"`, `"chrome"`. Bypasses Akamai/Cloudflare bot walls. |
| `user_agent`           | string  | Override the User-Agent used during **discovery**. |
| `download_ua`          | string  | Override the User-Agent used when **downloading** an article's full content. |
| `date_selector`        | string  | CSS selector for the publish date **on the article page**. Used both when downloading content and by *Backfill dates*. `select_one` is used, so pin the exact element (e.g. `:last-child`/`:nth-child`) when several share a class. A `<time datetime="…">` value is preferred automatically. |
| `date_format`          | string  | [strptime](https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes) pattern for the text matched by `date_selector`, e.g. `"%B %d, %Y"` for `October 2, 2023`. Omit to let common formats be auto-detected. |
| `date_url_format`      | string  | strftime-like pattern to extract a publish date from the article URL when the feed has none, e.g. `"/%Y/%m/%d/"`. |
| `block_detect`         | string  | Substring that marks a bot-block / "Access Denied" page so it is not cached as content. |
| `ioc_exclude`          | array<string> | Domains/URL substrings from this source that should never be treated as IOCs. |
| `rule_start` / `rule_end` | string | Markers bounding the article body, to strip boilerplate from cached content. |

### RSS / Atom (`scraper: "rss"`)

| Key                    | Type    | Description |
|------------------------|---------|-------------|
| `filter_tag_contains`  | string  | Keep only entries whose category/tag contains this substring. |

```json
{
  "name": "Outpost24 Threat Intel",
  "url": "https://outpost24.com/blog/feed/",
  "scraper": "rss",
  "config": { "filter_tag_contains": "Threat Intelligence" }
}
```

### HTML page (`scraper: "html"`)

| Key                    | Type    | Description |
|------------------------|---------|-------------|
| `link_selector`        | string  | CSS selector for the `<a>` links to articles. |
| `container_selector`   | string  | CSS selector scoping the search to article cards/rows. |
| `title_selector`       | string  | CSS selector for the article title within a container. |
| `title_from_url`       | bool    | Derive the title from the URL slug instead of the page. |
| `link_prefix`          | string  | Prefix prepended to relative links, e.g. `"https://example.com"`. |
| `card_date_selector`   | string  | CSS selector for a date shown on the listing card. |
| `card_date_format`     | string  | strptime format for `card_date_selector`. |
| `paginate`             | bool    | Follow pagination links. |
| `next_selector`        | string  | CSS selector for the "next page" link (when `paginate` is true). |
| `max_pages`            | int     | Maximum pages to crawl (0 = unlimited). |

```json
{
  "name": "Vendor Research Blog",
  "url": "https://example.com/blog/research/",
  "scraper": "html",
  "config": {
    "container_selector": "article.post",
    "link_selector": "h2 a",
    "link_prefix": "https://example.com",
    "card_date_selector": "time",
    "card_date_format": "%b %d, %Y",
    "paginate": true,
    "next_selector": "a.next",
    "max_pages": 5
  }
}
```

### Sitemap (`scraper: "sitemap"`)

| Key                | Type   | Description |
|--------------------|--------|-------------|
| `sitemap_pattern`  | string | Only follow child sitemaps / URLs containing this substring, e.g. `"post-sitemap"` to skip category/tag/author sitemaps. |

```json
{
  "name": "Unit 42",
  "url": "https://unit42.paloaltonetworks.com/sitemap.xml",
  "scraper": "sitemap",
  "config": { "sitemap_pattern": "post-sitemap" }
}
```

Sitemaps take publish dates from each entry's `<lastmod>`. When a sitemap omits
`<lastmod>` (many do), add `date_selector` + `date_format` so the date is read
from the article page instead — on content download and via *Backfill dates*:

```json
{
  "name": "Sekoia",
  "url": "https://www.sekoia.com/sitemap.xml",
  "scraper": "sitemap",
  "config": {
    "filter_url_regex": "sekoia\\.com/blog/[a-z0-9][a-z0-9-]+$",
    "date_selector": ".div-block-23 > div:last-child",
    "date_format": "%B %d, %Y"
  }
}
```

---

## Importing and exporting

**From the app UI**

- **Import:** *Add Source → "Import from JSON file"*, then pick a `.json` file.
- **Export:** open a feed's *Edit* dialog → *Export JSON*. The downloaded file is
  in the envelope format and can be edited and re-imported as-is.

**Via the API**

```sh
# Export all feeds (or ?uuids=a,b,c for a subset)
curl http://localhost:7474/api/sources/export > feeds.json

# Export a single feed
curl http://localhost:7474/api/sources/<uuid>/export

# Import (single object, array, or envelope all accepted)
curl -X POST http://localhost:7474/api/sources/import \
     -H 'Content-Type: application/json' \
     --data @feeds.json
```

The import response summarises the result:

```json
{ "ok": true, "created": ["DFIR Report"], "updated": [], "errors": [],
  "summary": "1 created, 0 updated, 0 failed" }
```

Re-importing a feed whose `url` already exists **updates** it in place rather than
creating a duplicate, so an exported file is also a safe way to back up and restore
your feed configuration.
