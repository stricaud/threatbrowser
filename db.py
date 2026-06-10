import io
import json
import re
import sqlite3
import glob
import os
import uuid as _uuid
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlparse

_HERE = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.environ.get("TB_DB", os.path.join(_HERE, "threatbrowser.db"))
CONTENT_PATH = os.environ.get("TB_CONTENT", os.path.join(_HERE, "../content"))
CACHE_DIR = os.environ.get("TB_CACHE", os.path.join(_HERE, "cache"))

# (name, url, scraper, config_dict)
INITIAL_SOURCES = [
    ("Talos Intelligence",      "https://blog.talosintelligence.com/rss/",                        "rss", {}),
    ("Unit 42",                 "https://unit42.paloaltonetworks.com/sitemap.xml",                  "sitemap", {"sitemap_pattern": "post-sitemap"}),
    ("CISA Advisories",         "https://www.cisa.gov/cybersecurity-advisories/all.xml",           "rss", {}),
    ("Threat Intel (FeedBurner)","https://feeds.feedburner.com/threatintelligence/pvexyqv7v0v",   "rss", {}),
    ("DFIR Report",             "https://thedfirreport.com/feed/",                                 "rss", {}),
    ("Qualys Threat Research",  "https://blog.qualys.com/vulnerabilities-threat-research/feed",    "rss", {}),
    ("Cybersecurity News",      "https://cybersecuritynews.com/feed/",                             "rss", {}),
    ("Outpost24",               "https://outpost24.com/blog/category/research-and-threat-intel/feed/", "rss", {}),
    ("Securelist APT",          "https://securelist.com/threat-category/apt-targeted-attacks/feed/","rss", {}),
    ("Security Affairs APT",    "https://securityaffairs.com/category/apt/feed",                   "rss", {}),
    ("The Hacker News",         "https://feeds.feedburner.com/TheHackersNews",                     "rss", {}),
    ("Elastic Security Labs",   "https://www.elastic.co/security-labs/rss/feed.xml",               "rss", {}),
    ("WeLiveSecurity",          "https://www.welivesecurity.com/en/rss/feed/",                     "rss", {}),
    ("CERT-UA",                 "https://cert.gov.ua/api/articles/rss",                            "rss", {}),
    ("NIST Cybersecurity",      "https://www.nist.gov/blogs/cybersecurity-insights/rss.xml",       "rss", {}),
    ("ThreatPost",              "https://threatpost.com/feed/",                                    "rss", {}),
    ("dmpdump",                 "https://dmpdump.github.io/",                                      "html", {
        "link_selector":      "article h2 a, article h1 a",
        "link_prefix":        "https://dmpdump.github.io",
        "filter_url_contains":"https://dmpdump.github.io/posts/",
        "paginate":           True,
        "next_selector":      "a.btn-outline-primary[href*='page'], a[rel='next']",
        "max_pages":          10,
    }),
    ("threat.boutique",         "https://threat.boutique/feed/rss",                                "rss", {}),
    ("Bitdefender Labs",        "https://www.bitdefender.com/en-us/blog/labs/sitemap.xml",         "sitemap", {"date_selector": "p[style*='757575']"}),
    ("Checkpoint Research",     "https://research.checkpoint.com/feed/",                           "rss", {}),
    ("Cloudflare Security",     "https://blog.cloudflare.com/tag/security/rss",                    "rss", {}),
    ("Microsoft Threat Intel",  "https://www.microsoft.com/en-us/security/blog/topic/threat-intelligence/feed", "rss", {}),
    ("Akamai Security",         "https://feeds.feedburner.com/akamai/blog",                        "rss", {"filter_url_contains": "akamai.com/blog/security", "block_detect": "Access Denied"}),
    ("Sekoia",                  "https://blog.sekoia.io/",                                         "html", {
        "link_selector":      "article h2 a, article h3 a",
        "filter_url_regex":   "blog\\.sekoia\\.io/[a-zA-Z0-9][a-zA-Z0-9-]+/$",
        "paginate":           True,
        "next_selector":      "a.next, a[rel='next'], a.next.page-numbers",
        "max_pages":          20,
    }),
    ("Hexacorn",                "http://www.hexacorn.com/blog/feed/",                              "rss", {}),
    ("SpecterOps",              "https://specterops.io/feed/",                                     "rss", {}),
    ("Red Canary",              "https://www.redcanary.co/feed/",                                  "rss", {}),
    ("Sophos",                  "https://news.sophos.com/en-us/category/threat-research/feed/",    "rss", {}),
    ("Virus Bulletin",          "https://www.virusbulletin.com/rss",                               "rss", {}),
    ("BleepingComputer",        "https://www.bleepingcomputer.com/feed/",                          "rss", {}),
    ("SentinelOne Labs",        "https://www.sentinelone.com/labs/feed/",                          "rss", {}),
    ("TrendMicro",              "http://feeds.trendmicro.com/TrendMicroSimplySecurity",            "rss", {}),
    ("Volexity",                "https://www.volexity.com/feed/",                                  "rss", {}),
    ("HarfangLab",              "https://harfanglab.io/insidethelab/feed/",                        "rss", {}),
    ("AhnLab ASEC APT",         "https://asec.ahnlab.com/en/category/apt-en/feed/",               "rss", {}),
    ("AhnLab ASEC Phishing",    "https://asec.ahnlab.com/en/category/phishing-scam-en/feed",      "rss", {}),
    ("AhnLab ASEC Trend",       "https://asec.ahnlab.com/en/category/trend-en/feed",              "rss", {}),
    ("AhnLab ASEC CERT",        "https://asec.ahnlab.com/en/category/cert-en/feed",               "rss", {}),
    ("McAfee Labs",             "https://www.mcafee.com/blogs/other-blogs/mcafee-labs/feed/",     "rss", {}),
    ("Symantec/Broadcom",       "https://sed-cms.broadcom.com/rss/v1/blogs/rss.xml",              "rss", {"filter_url_contains": "/threat-intelligence/"}),
    ("ThreatConnect",           "https://threatconnect.com/blog/feed/",                            "rss", {}),
    ("CERT.se",                 "https://www.cert.se/feed.rss",                                   "rss", {}),
    ("CERT.si",                 "https://www.cert.si/en/category/news/feed/",                     "rss", {}),
    ("CERT.pl",                 "https://cert.pl/en/rss.xml",                                     "rss", {}),
    ("NCSC.nl",                 "https://feeds.english.ncsc.nl/news.rss",                         "rss", {}),
    ("CERT.lv",                 "https://cert.lv/en/feed/rss/all",                                "rss", {}),
    ("JPCERT CC",               "https://blogs.jpcert.or.jp/en/atom.xml",                         "rss", {}),
    ("CIRT Bangladesh",         "https://www.cirt.gov.bd/news",                                    "html", {
        "link_selector":      "a[href^='/news/']",
        "link_prefix":        "https://www.cirt.gov.bd",
        "paginate":           True,
        "next_selector":      "li.pager__item--next a, li.pager-next a, a[rel='next']",
        "max_pages":          20,
        "card_date_selector": "span.date",
        "card_date_format":   "%d-%b-%Y %H:%M:%S",
    }),
    ("Microsoft Security Blog", "https://www.microsoft.com/en-us/security/blog/feed/",            "rss", {}),
    ("Infostealers",            "https://www.infostealers.com/info-stealers-reports/feed/",        "rss", {}),
    ("Google Project Zero",     "https://googleprojectzero.blogspot.com/feeds/posts/default?alt=rss", "rss", {}),
    ("SecurityWeek",            "https://www.securityweek.com/feed/",                             "rss", {}),
    ("Genians",                 "https://www.genians.co.kr/blog/threat_intelligence/rss.xml",     "rss", {}),
    ("Intrinsec",               "https://www.intrinsec.com/feed/",                                "rss", {}),
    ("Huntress",                "https://www.huntress.com/blog/rss.xml",                          "rss", {}),
    ("SANS ISC",                "https://isc.sans.edu/rssfeed.xml",                               "rss", {}),
    ("IC3",                     "https://www.ic3.gov/CSA/rss",                                    "rss", {}),
    ("ANY.RUN Malware",         "https://any.run/cybersecurity-blog/category/malware-analysis/feed/","rss", {}),
    ("Malwarebytes",            "https://www.malwarebytes.com/blog/feed/index.xml",               "rss", {}),
    ("Binary Defense",          "https://binarydefense.com/sitemaps-1-section-resources-1-sitemap.xml", "sitemap", {"filter_url_contains": "/resources/blog/"}),
    ("Field Effect",            "https://fieldeffect.com/blog/rss.xml",                           "rss", {}),
    ("Avast",                   "https://blog.avast.com/rss.xml",                                 "rss", {}),
    ("Google TAG",              "https://blog.google/threat-analysis-group/rss/",                 "rss", {}),
    ("Group-IB",                "https://blog.group-ib.com/rss.xml",                              "rss", {}),
    ("Dr.Web",                  "https://news.drweb.com/rss/get/?c=9",                            "rss", {}),
    ("MalwareTech",             "https://www.malwaretech.com/feed",                               "rss", {}),
    ("Trustwave SpiderLabs",    "https://www.trustwave.com/en-us/resources/blogs/spiderlabs-blog/rss.xml", "rss", {}),
    ("Krebs on Security",       "https://krebsonsecurity.com/feed/",                              "rss", {}),
    ("Israel Gov",              "https://www.gov.il/he/api/PublicationApi/rss/4bcc13f5-fed6-4b8c-b8ee-7bf4a6bc81c8", "rss", {}),
    # HTML-scraped blogs (CSS-selector based)
    ("Proofpoint Threat Insight","https://www.proofpoint.com/us/blog/threat-insight", "html", {
        "container_selector": "div.blog-mosaic__item",
        "link_selector":      "a.blog-mosaic__link",
        "title_selector":     "div.blog-mosaic__title",
        "link_prefix":        "https://www.proofpoint.com",
    }),
    ("ReversingLabs",           "https://www.reversinglabs.com/blog/tag/threat-research", "html", {
        "link_selector": "div.blog__listing-item article.blog__item h3 a",
        "link_prefix":   "https://www.reversinglabs.com",
    }),
    ("Splunk Security",         "https://www.splunk.com/en_us/blog/author/secmrkt-research.html", "html", {
        "link_selector":         "div.item a",
        "filter_url_contains":   "splunk.com/en_us/blog/",
    }),
    ("JPCERT Blog",             "https://www.jpcert.or.jp/english/update.html", "html", {
        "link_selector":    "table.table_list td a, ul.list a",
        "link_prefix":      "https://www.jpcert.or.jp",
        "filter_url_regex": "/en/\\d{4}/",
    }),
    ("Forcepoint X-Labs",       "https://www.forcepoint.com/blog/x-labs", "html", {
        "link_selector":   "a[href^=\"/blog/x-labs/\"]",
        "link_prefix":     "https://www.forcepoint.com",
        "exclude_url":     "https://www.forcepoint.com/blog/x-labs/x-labs",
        "title_from_url":  True,
    }),
    ("CrowdStrike Counter-Adversary", "https://www.crowdstrike.com/en-us/blog/category/counter-adversary-operations/", "html", {
        "container_selector": "dl.vertical_dropdown_list dd",
        "link_selector":      "a",
        "title_selector":     "div.title",
        "link_prefix":        "https://www.crowdstrike.com",
    }),
    ("EclecticIQ",              "https://www.eclecticiq.com/blog?type=intelligence-research", "html", {
        "container_selector": "div.relative.flex.flex-col.items-start",
        "link_selector":      "a",
        "title_selector":     "h3",
        "link_prefix":        "https://www.eclecticiq.com",
    }),
]

# Migrations for old-style scraper names
_SCRAPER_MIGRATIONS = {
    "rss_qualys":       ("rss", {}),
    "rss_akamai":       ("rss", {"filter_url_contains": "akamai.com/blog/security"}),
    "rss_symantec":     ("rss", {"filter_url_contains": "/threat-intelligence/"}),
    "rss_threatconnect":("rss", {}),
    "rss_mcafee":       ("rss", {}),
    "blog_proofpoint":  ("html", {"container_selector": "div.blog-mosaic__item", "link_selector": "a.blog-mosaic__link", "title_selector": "div.blog-mosaic__title", "link_prefix": "https://www.proofpoint.com"}),
    "blog_reversinglabs":("html", {"link_selector": "div.blog__listing-item article.blog__item h3 a", "link_prefix": "https://www.reversinglabs.com"}),
    "blog_splunk":      ("html", {"link_selector": "div.item a", "filter_url_contains": "splunk.com/en_us/blog/"}),
    "blog_jpcert":      ("html", {"link_selector": "table.table_list td a, ul.list a", "link_prefix": "https://www.jpcert.or.jp", "filter_url_regex": "/en/\\d{4}/"}),
    "blog_forcepoint":  ("html", {"link_selector": "a[href^=\"/blog/x-labs/\"]", "link_prefix": "https://www.forcepoint.com", "exclude_url": "https://www.forcepoint.com/blog/x-labs/x-labs", "title_from_url": True}),
    "blog_crowdstrike": ("html", {"container_selector": "dl.vertical_dropdown_list dd", "link_selector": "a", "title_selector": "div.title", "link_prefix": "https://www.crowdstrike.com"}),
    "blog_zscaler":     ("html", {"link_selector": "a[href*=\"/blogs/\"]", "link_prefix": "https://threatlabz.zscaler.com"}),
    "blog_eclecticiq":  ("html", {"container_selector": "div.relative.flex.flex-col.items-start", "link_selector": "a", "title_selector": "h3", "link_prefix": "https://www.eclecticiq.com"}),
}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            id           INTEGER PRIMARY KEY,
            uuid         TEXT NOT NULL DEFAULT '' UNIQUE,
            name         TEXT NOT NULL,
            url          TEXT NOT NULL UNIQUE,
            scraper      TEXT NOT NULL DEFAULT 'rss',
            config       TEXT NOT NULL DEFAULT '{}',
            tags         TEXT NOT NULL DEFAULT '[]',
            active       INTEGER NOT NULL DEFAULT 1,
            last_fetched TEXT
        );

        CREATE TABLE IF NOT EXISTS articles (
            id           INTEGER PRIMARY KEY,
            uuid         TEXT NOT NULL DEFAULT '' UNIQUE,
            source_id    INTEGER REFERENCES sources(id) ON DELETE CASCADE,
            title        TEXT,
            url          TEXT NOT NULL UNIQUE,
            published_at TEXT,
            first_seen   TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'new',
            cached_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS site_rules (
            id        INTEGER PRIMARY KEY,
            pattern   TEXT NOT NULL UNIQUE,
            rule_start TEXT NOT NULL DEFAULT '',
            rule_end   TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS warninglists (
            id         INTEGER PRIMARY KEY,
            entry      TEXT NOT NULL UNIQUE,
            entry_type TEXT NOT NULL DEFAULT 'hostname'
        );

        CREATE TABLE IF NOT EXISTS prefetch_errors (
            id    INTEGER PRIMARY KEY,
            url   TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL,
            ts    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rejected_domains (
            id     INTEGER PRIMARY KEY,
            domain TEXT NOT NULL UNIQUE
        );

        CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
        CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_id);
        CREATE INDEX IF NOT EXISTS idx_articles_date   ON articles(published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_wl_entry        ON warninglists(entry);
    """)
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS article_fts
        USING fts5(article_id UNINDEXED, content, tokenize='trigram');
    """)
    conn.commit()

    _migrate(conn)

    # Default settings
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('content_path', ?)", (CONTENT_PATH,))
    conn.commit()

    cur = conn.execute("SELECT COUNT(*) FROM sources")
    if cur.fetchone()[0] == 0:
        conn.executemany(
            "INSERT OR IGNORE INTO sources (uuid, name, url, scraper, config) VALUES (?, ?, ?, ?, ?)",
            [(str(_uuid.uuid4()), n, u, s, json.dumps(c)) for n, u, s, c in INITIAL_SOURCES],
        )
        conn.commit()

    # Import existing termbrowser config (only if no rules exist yet)
    cur2 = conn.execute("SELECT COUNT(*) FROM site_rules")
    if cur2.fetchone()[0] == 0:
        _import_termbrowser_config(conn)

    conn.close()


def index_article_content(article_int_id: int, text: str):
    """Index article content in FTS. Uses integer PK internally."""
    conn = get_conn()
    conn.execute("DELETE FROM article_fts WHERE article_id=?", (article_int_id,))
    conn.execute("INSERT INTO article_fts(article_id, content) VALUES (?, ?)", (article_int_id, text))
    conn.commit()
    conn.close()


def reindex_all_cached(cache_dir: str):
    """Index any cached .md files not yet in article_fts. Called once on startup."""
    conn = get_conn()
    indexed = {r[0] for r in conn.execute("SELECT article_id FROM article_fts").fetchall()}
    conn.close()
    for path in glob.glob(os.path.join(cache_dir, "*.md")):
        try:
            article_int_id = int(os.path.basename(path)[:-3])
        except ValueError:
            continue
        if article_int_id in indexed:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            index_article_content(article_int_id, text)
        except Exception:
            pass


def set_article_download_status(article_uuid: str, status: int):
    conn = get_conn()
    conn.execute("UPDATE articles SET download_status=? WHERE uuid=?", (status, article_uuid))
    conn.commit()
    conn.close()


def clear_prefetch_errors():
    conn = get_conn()
    conn.execute("DELETE FROM prefetch_errors")
    conn.commit()
    conn.close()


def save_prefetch_error(entry: dict):
    conn = get_conn()
    conn.execute(
        "INSERT INTO prefetch_errors (url, title, error, ts) VALUES (?, ?, ?, ?)",
        (entry["url"], entry.get("title", ""), entry["error"], entry["ts"]),
    )
    conn.commit()
    conn.close()


def get_prefetch_errors(limit: int = 1000) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT url, title, error, ts FROM prefetch_errors ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [{"url": r[0], "title": r[1], "error": r[2], "ts": r[3]} for r in rows]


def add_rejected_domain(domain: str):
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO rejected_domains (domain) VALUES (?)", (domain.lower(),))
    conn.commit()
    conn.close()


def remove_rejected_domain(domain: str):
    conn = get_conn()
    conn.execute("DELETE FROM rejected_domains WHERE domain=?", (domain.lower(),))
    conn.commit()
    conn.close()


def get_rejected_domains() -> list[str]:
    conn = get_conn()
    rows = conn.execute("SELECT domain FROM rejected_domains ORDER BY domain").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _migrate(conn):
    """Idempotent schema migrations."""
    src_cols = {r[1] for r in conn.execute("PRAGMA table_info(sources)").fetchall()}
    art_cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()}

    if "config" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN config TEXT NOT NULL DEFAULT '{}'")
    if "tags" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
    if "cached_at" not in art_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN cached_at TEXT")
    if "download_status" not in art_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN download_status INTEGER")
        conn.execute("UPDATE articles SET download_status=200 WHERE cached_at IS NOT NULL")

    # Add uuid columns and backfill existing rows
    if "uuid" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN uuid TEXT")
        rows = conn.execute("SELECT id FROM sources WHERE uuid IS NULL OR uuid=''").fetchall()
        for (row_id,) in rows:
            conn.execute("UPDATE sources SET uuid=? WHERE id=?", (str(_uuid.uuid4()), row_id))
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_uuid ON sources(uuid)")
        except Exception:
            pass

    if "uuid" not in art_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN uuid TEXT")
        rows = conn.execute("SELECT id FROM articles WHERE uuid IS NULL OR uuid=''").fetchall()
        for (row_id,) in rows:
            conn.execute("UPDATE articles SET uuid=? WHERE id=?", (str(_uuid.uuid4()), row_id))
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_uuid ON articles(uuid)")
        except Exception:
            pass

    if "last_fetch_error" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN last_fetch_error TEXT")

    if "is_pseudo" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN is_pseudo INTEGER NOT NULL DEFAULT 0")
        # Mark known internal pseudo-sources
        conn.execute("UPDATE sources SET is_pseudo=1 WHERE url LIKE 'internal://%'")
        for _, _, base_url in _PSEUDO_SOURCE_PATTERNS:
            conn.execute("UPDATE sources SET is_pseudo=1 WHERE url=?", (base_url,))
        # Any active=0 source whose articles are ALL has_scenario is a stub source
        conn.execute("""
            UPDATE sources SET is_pseudo=1
            WHERE active=0
            AND id IN (SELECT DISTINCT source_id FROM articles WHERE status='has_scenario')
            AND id NOT IN (SELECT DISTINCT source_id FROM articles WHERE status!='has_scenario')
        """)

    # Remove garbage articles from scraper noise
    conn.execute("DELETE FROM articles WHERE url LIKE 'tel:%'")
    conn.execute("DELETE FROM articles WHERE url LIKE 'sms:%'")
    conn.execute("DELETE FROM articles WHERE url LIKE 'callto:%'")
    conn.execute("DELETE FROM articles WHERE url LIKE '%/cdn-cgi/%'")
    conn.execute(
        "DELETE FROM articles WHERE url NOT LIKE 'http://%' AND url NOT LIKE 'https://%'"
    )
    # Remove cross-domain junk: articles from HTML sources whose URL domain
    # doesn't share a registered domain with the source URL.
    # (RSS/sitemap sources are trusted — they use feed-provided links.)
    _html_sources = conn.execute(
        "SELECT id, url FROM sources WHERE scraper='html' AND url LIKE 'http%'"
    ).fetchall()
    for src_id, src_url in _html_sources:
        try:
            src_parts = src_url.split("/")[2].lower().split(".")  # hostname parts
            src_rd = ".".join(src_parts[-2:]) if len(src_parts) >= 2 else src_parts[0]
        except Exception:
            continue
        # Fetch article URLs for this source, delete those on different domains
        art_rows = conn.execute(
            "SELECT id, url FROM articles WHERE source_id=?", (src_id,)
        ).fetchall()
        to_delete = []
        for art_id, art_url in art_rows:
            try:
                if not art_url.startswith(("http://", "https://")):
                    to_delete.append(art_id)
                    continue
                art_parts = art_url.split("/")[2].lower().split(".")
                art_rd = ".".join(art_parts[-2:]) if len(art_parts) >= 2 else art_parts[0]
                if art_rd != src_rd:
                    to_delete.append(art_id)
            except Exception:
                continue
        if to_delete:
            conn.executemany("DELETE FROM articles WHERE id=?", [(i,) for i in to_delete])

    # Remove pseudo-sources whose registered domain is already covered by a real source.
    # E.g. any.run / app.any.run refs are redundant when ANY.RUN Malware source exists.
    def _reg_domain_from_url(u: str) -> str:
        try:
            parts = u.split("/")[2].lower().rstrip(".").split(".")
            return ".".join(parts[-2:]) if len(parts) >= 2 else parts[0]
        except Exception:
            return ""

    real_rds = {
        _reg_domain_from_url(r[0])
        for r in conn.execute(
            "SELECT url FROM sources WHERE is_pseudo=0 AND url LIKE 'http%'"
        ).fetchall()
    }
    redundant_pseudo = [
        r[0] for r in conn.execute(
            "SELECT id, url FROM sources WHERE is_pseudo=1"
        ).fetchall()
        if _reg_domain_from_url(r[1]) in real_rds
    ]
    if redundant_pseudo:
        ph = ",".join("?" * len(redundant_pseudo))
        conn.execute(f"DELETE FROM articles WHERE source_id IN ({ph})", redundant_pseudo)
        conn.execute(f"DELETE FROM sources  WHERE id         IN ({ph})", redundant_pseudo)

    conn.commit()

    # Migrate old named scrapers
    for old, (new_scraper, cfg) in _SCRAPER_MIGRATIONS.items():
        conn.execute(
            "UPDATE sources SET scraper=?, config=? WHERE scraper=?",
            (new_scraper, json.dumps(cfg), old),
        )
    conn.commit()

    def _migrate_url(old_url: str, new_url: str, new_scraper: str, new_cfg: dict):
        """Rename a source URL, merging articles if the new URL already exists."""
        old = conn.execute("SELECT id FROM sources WHERE url=?", (old_url,)).fetchone()
        if not old:
            return  # already migrated or never existed
        new = conn.execute("SELECT id FROM sources WHERE url=?", (new_url,)).fetchone()
        if new:
            # new URL already present — re-home articles from old to new, drop old
            conn.execute("UPDATE articles SET source_id=? WHERE source_id=?", (new[0], old[0]))
            conn.execute("DELETE FROM sources WHERE id=?", (old[0],))
        else:
            conn.execute(
                "UPDATE sources SET url=?, scraper=?, config=? WHERE id=?",
                (new_url, new_scraper, json.dumps(new_cfg), old[0]),
            )
        # Always sync scraper + config on the surviving row so re-runs are idempotent
        surviving = conn.execute("SELECT id FROM sources WHERE url=?", (new_url,)).fetchone()
        if surviving:
            conn.execute(
                "UPDATE sources SET scraper=?, config=? WHERE id=?",
                (new_scraper, json.dumps(new_cfg), surviving[0]),
            )

    # dmpdump: RSS feed capped at 5 items; switch to HTML scraper for full coverage
    _migrate_url(
        "https://dmpdump.github.io/feed.xml",
        "https://dmpdump.github.io/",
        "html",
        {
            "link_selector":       "article h2 a, article h1 a",
            "link_prefix":         "https://dmpdump.github.io",
            "filter_url_contains": "https://dmpdump.github.io/posts/",
            "paginate":            True,
            "next_selector":       "a.btn-outline-primary[href*='page'], a[rel='next']",
            "max_pages":           10,
        },
    )

    # Sekoia: RSS feed capped at 10 items; switch to HTML scraper for full coverage
    _migrate_url(
        "https://blog.sekoia.io/feed/",
        "https://blog.sekoia.io/",
        "html",
        {
            "link_selector":    "article h2 a, article h3 a",
            "filter_url_regex": "blog\\.sekoia\\.io/[a-zA-Z0-9][a-zA-Z0-9-]+/$",
            "paginate":         True,
            "next_selector":    "a.next, a[rel='next'], a.next.page-numbers",
            "max_pages":        20,
        },
    )

    # CIRT Bangladesh: RSS feed dead (404); switch to HTML scraper
    _migrate_url(
        "https://www.cirt.gov.bd/feed/",
        "https://www.cirt.gov.bd/news",
        "html",
        {
            "link_selector":      "a[href^='/news/']",
            "link_prefix":        "https://www.cirt.gov.bd",
            "paginate":           True,
            "next_selector":      "li.pager__item--next a, li.pager-next a, a[rel='next']",
            "max_pages":          20,
            "card_date_selector": "span.date",
            "card_date_format":   "%d-%b-%Y %H:%M:%S",
        },
    )

    # Bitdefender Labs: RSS capped at 16 items; sitemap has all 517 posts
    _migrate_url(
        "https://www.bitdefender.com/nuxt/api/en-us/rss/labs/",
        "https://www.bitdefender.com/en-us/blog/labs/sitemap.xml",
        "sitemap",
        {"date_selector": "p[style*='757575']"},
    )
    # Backfill date_selector for databases where the above already ran (config was {})
    conn.execute(
        "UPDATE sources SET config=? WHERE url=? AND config NOT LIKE '%date_selector%'",
        (json.dumps({"date_selector": "p[style*='757575']"}),
         "https://www.bitdefender.com/en-us/blog/labs/sitemap.xml"),
    )

    # Unit 42: RSS capped at 17 items; WordPress sitemap has all posts with lastmod dates
    _migrate_url(
        "https://unit42.paloaltonetworks.com/feed/",
        "https://unit42.paloaltonetworks.com/sitemap.xml",
        "sitemap",
        {"sitemap_pattern": "post-sitemap"},
    )

    # Akamai: add block_detect so its Access Denied pages don't get cached as content
    conn.execute("""
        UPDATE sources
        SET config = json_set(COALESCE(config,'{}'), '$.block_detect', 'Access Denied')
        WHERE url = 'https://feeds.feedburner.com/akamai/blog'
          AND (config NOT LIKE '%block_detect%')
    """)

    # ThreatConnect: "Threat Intelligence Operations" tag no longer exists in feed
    conn.execute(
        "UPDATE sources SET config=? WHERE url=? AND config LIKE '%Threat Intelligence Operations%'",
        ("{}", "https://threatconnect.com/blog/feed/"),
    )

    # Volexity: /blog/feed/ returns HTML; actual feed is at /feed/
    _migrate_url("https://www.volexity.com/blog/feed/", "https://www.volexity.com/feed/", "rss", {})

    # SpecterOps: blog moved from Medium (posts.specterops.io) to specterops.io/feed/
    _migrate_url(
        "https://posts.specterops.io/feed",
        "https://specterops.io/feed/",
        "rss",
        {},
    )

    # McAfee Labs: re-enable if previously disabled (curl-cffi Firefox TLS impersonation
    # bypasses Akamai's fingerprint check that blocked requests/Playwright).
    conn.execute(
        "UPDATE sources SET active=1 WHERE url=? AND active=0",
        ("https://www.mcafee.com/blogs/other-blogs/mcafee-labs/feed/",),
    )

    conn.commit()


# ── Sources ───────────────────────────────────────────────────────────────────

def get_sources(active_only=False):
    conn = get_conn()
    q = "SELECT * FROM sources WHERE active=1" if active_only else "SELECT * FROM sources"
    rows = conn.execute(q + " ORDER BY name").fetchall()
    counts = {
        r[0]: r[1]
        for r in conn.execute("SELECT source_id, COUNT(*) FROM articles GROUP BY source_id").fetchall()
    }
    id_map = {
        r[0]: r[1]
        for r in conn.execute("SELECT uuid, id FROM sources").fetchall()
    }
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["config"] = json.loads(d.get("config") or "{}")
        d["tags"]   = json.loads(d.get("tags")   or "[]")
        d["active"] = bool(d.get("active", 1))   # SQLite stores as 0/1; JSON clients expect true/false
        d["article_count"] = counts.get(id_map.get(d["uuid"], -1), 0)
        result.append(d)
    return result


def add_source(name: str, url: str, scraper: str = "rss", config: dict = None, tags: list[str] = None) -> dict:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO sources (uuid, name, url, scraper, config, tags) VALUES (?, ?, ?, ?, ?, ?) RETURNING *",
            (str(_uuid.uuid4()), name, url, scraper, json.dumps(config or {}), json.dumps(tags or [])),
        )
        row = dict(cur.fetchone())
        conn.commit()
    finally:
        conn.close()
    row["config"] = json.loads(row.get("config") or "{}")
    row["tags"]   = json.loads(row.get("tags")   or "[]")
    row["active"] = bool(row.get("active", 1))
    return row


def get_all_source_tags() -> list[str]:
    sources = get_sources()
    tags: set[str] = set()
    for s in sources:
        tags.update(s.get("tags") or [])
    return sorted(tags)


def update_source(source_uuid: str, **kwargs):
    conn = get_conn()
    try:
        if "config" in kwargs and isinstance(kwargs["config"], dict):
            kwargs["config"] = json.dumps(kwargs["config"])
        if "tags" in kwargs and isinstance(kwargs["tags"], list):
            kwargs["tags"] = json.dumps(kwargs["tags"])
        sets = ", ".join(f"{k}=?" for k in kwargs)
        conn.execute(f"UPDATE sources SET {sets} WHERE uuid=?", (*kwargs.values(), source_uuid))
        conn.commit()
    finally:
        conn.close()


def get_source_article_urls(source_id: int) -> set[str]:
    conn = get_conn()
    rows = conn.execute("SELECT url FROM articles WHERE source_id=?", (source_id,)).fetchall()
    conn.close()
    return {r[0] for r in rows}


def delete_source(source_uuid: str) -> list[int]:
    """Delete source and its articles. Returns article integer ids for cache file purging."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT a.id FROM articles a JOIN sources s ON a.source_id=s.id WHERE s.uuid=?",
            (source_uuid,),
        ).fetchall()
        article_int_ids = [r[0] for r in rows]
        conn.execute(
            "DELETE FROM articles WHERE source_id=(SELECT id FROM sources WHERE uuid=?)", (source_uuid,)
        )
        conn.execute("DELETE FROM sources WHERE uuid=?", (source_uuid,))
        conn.commit()
    finally:
        conn.close()
    return article_int_ids


def get_source(source_id: int) -> dict | None:
    """Look up source by integer PK (internal use only)."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["config"] = json.loads(d.get("config") or "{}")
    return d


def update_source_fetched(source_uuid: str, ts: str):
    conn = get_conn()
    conn.execute("UPDATE sources SET last_fetched=? WHERE uuid=?", (ts, source_uuid))
    conn.commit()
    conn.close()


def update_source_fetch_error(source_uuid: str, error: str | None):
    """Store or clear the last fetch error for a source."""
    conn = get_conn()
    conn.execute("UPDATE sources SET last_fetch_error=? WHERE uuid=?", (error, source_uuid))
    conn.commit()
    conn.close()


def update_article_date(article_uuid: str, published_at: str):
    conn = get_conn()
    conn.execute("UPDATE articles SET published_at=? WHERE uuid=?", (published_at, article_uuid))
    conn.commit()
    conn.close()


# ── Articles ──────────────────────────────────────────────────────────────────

SINCE_MAP = {
    "24h": "24 hours",
    "7d":  "7 days",
    "30d": "30 days",
    "6m":  "180 days",
    "1y":  "365 days",
}


def get_articles(status=None, source_uuids=None, q=None, since=None, limit=500, offset=0,
                 dl_error=False, source_q=None, title_q=None, url_q=None, noisy=False):
    conn = get_conn()
    # Always exclude articles from pseudo-sources (scenario-reference stubs, MITRE, etc.)
    clauses = ["(s.is_pseudo IS NULL OR s.is_pseudo = 0)"]
    params = []

    if noisy:
        # Noisy tab: show ONLY articles from sources tagged "noisy"
        clauses.append('s.tags LIKE \'%"noisy"%\'')
    else:
        # Normal view: hide articles from sources tagged "noisy"
        clauses.append('(s.tags IS NULL OR s.tags NOT LIKE \'%"noisy"%\')')

    if dl_error:
        clauses.append("a.download_status IS NOT NULL AND a.download_status != 200")
    elif status == "no_scenario":
        clauses.append("a.status != 'has_scenario'")
    elif status and status != "all":
        clauses.append("a.status = ?")
        params.append(status)

    if source_uuids:
        placeholders = ",".join("?" * len(source_uuids))
        clauses.append(f"s.uuid IN ({placeholders})")
        params.extend(source_uuids)

    # Field-specific filters (from parsed query tokens)
    if source_q:
        clauses.append("s.name LIKE ?")
        params.append(f"%{source_q}%")
    if title_q:
        clauses.append("a.title LIKE ?")
        params.append(f"%{title_q}%")
    if url_q:
        clauses.append("a.url LIKE ?")
        params.append(f"%{url_q}%")

    # Global free-text search: title + URL + content FTS + source name
    if q:
        fts_conn = get_conn()
        try:
            fts_int_ids = [r[0] for r in fts_conn.execute(
                "SELECT article_id FROM article_fts WHERE content MATCH ?", (q,)
            ).fetchall()]
        except sqlite3.OperationalError:
            fts_int_ids = []
        fts_conn.close()
        like = f"%{q}%"
        if fts_int_ids:
            placeholders = ",".join("?" * len(fts_int_ids))
            clauses.append(
                f"(a.title LIKE ? OR a.url LIKE ? OR s.name LIKE ? OR a.id IN ({placeholders}))"
            )
            params.extend([like, like, like, *fts_int_ids])
        else:
            clauses.append("(a.title LIKE ? OR a.url LIKE ? OR s.name LIKE ?)")
            params.extend([like, like, like])

    if since and since in SINCE_MAP:
        clauses.append(
            f"COALESCE(a.published_at, a.first_seen) > datetime('now', '-{SINCE_MAP[since]}')"
        )

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    count_params = list(params)

    params.extend([limit, offset])
    rows = conn.execute(f"""
        SELECT a.uuid, a.title, a.url, a.published_at, a.first_seen, a.status, a.cached_at,
               a.download_status, s.name as source_name, s.uuid as source_uuid
        FROM articles a JOIN sources s ON a.source_id = s.id
        {where}
        ORDER BY COALESCE(a.published_at, a.first_seen) DESC
        LIMIT ? OFFSET ?
    """, params).fetchall()

    total = conn.execute(
        f"SELECT COUNT(*) FROM articles a JOIN sources s ON a.source_id = s.id {where}",
        count_params,
    ).fetchone()[0]

    conn.close()
    return [dict(r) for r in rows], total


def get_article(article_uuid: str) -> dict | None:
    """Look up article by UUID. Returns dict including integer 'id' for internal cache path use."""
    conn = get_conn()
    row = conn.execute(
        "SELECT a.*, s.name as source_name, s.uuid as source_uuid "
        "FROM articles a JOIN sources s ON a.source_id=s.id WHERE a.uuid=?",
        (article_uuid,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _is_pseudo_source_url(url: str) -> bool:
    if url.startswith("internal://"):
        return True
    return any(url == base_url for _, _, base_url in _PSEUDO_SOURCE_PATTERNS)


def upsert_articles(articles: list[dict]) -> int:
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    new_count = 0
    try:
        for a in articles:
            url = a.get("url", "")
            if not url.startswith(("http://", "https://")) or "/cdn-cgi/" in url:
                continue
            try:
                conn.execute(
                    "INSERT INTO articles (uuid, source_id, title, url, published_at, first_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (str(_uuid.uuid4()), a["source_id"], a.get("title"), a["url"], a.get("published_at"), now),
                )
                new_count += 1
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT a.id, s.url FROM articles a JOIN sources s ON a.source_id=s.id WHERE a.url=?",
                    (a["url"],),
                ).fetchone()
                if row and _is_pseudo_source_url(row[1]):
                    conn.execute(
                        "UPDATE articles SET source_id=?, title=COALESCE(?, title),"
                        " published_at=COALESCE(published_at, ?) WHERE id=?",
                        (a["source_id"], a.get("title"), a.get("published_at"), row[0]),
                    )
        conn.commit()
    finally:
        conn.close()
    return new_count


def set_articles_status(uuids: list[str], status: str):
    conn = get_conn()
    conn.executemany("UPDATE articles SET status=? WHERE uuid=?", [(status, u) for u in uuids])
    conn.commit()
    conn.close()


def set_article_cached(article_uuid: str):
    conn = get_conn()
    conn.execute(
        "UPDATE articles SET cached_at=? WHERE uuid=?",
        (datetime.now(timezone.utc).isoformat(), article_uuid),
    )
    conn.commit()
    conn.close()


# ── Scenario sync ─────────────────────────────────────────────────────────────

# (url_prefix, source_name, source_base_url) — longest prefix wins
_PSEUDO_SOURCE_PATTERNS = [
    ("https://attack.mitre.org/detectionstrategies/", "Mitre detectionstrategies", "https://attack.mitre.org/detectionstrategies/"),
    ("https://attack.mitre.org/techniques/",          "Mitre ATT&CK Techniques",   "https://attack.mitre.org/techniques/"),
    ("https://attack.mitre.org/",                     "Mitre ATT&CK",              "https://attack.mitre.org/"),
]
_FALLBACK_SOURCE_NAME = "Scenario References"
_FALLBACK_SOURCE_URL  = "internal://scenario-references"


def _ensure_ref_source(conn, name: str, url: str) -> int:
    """Get or create a pseudo-source with the given name and URL. Returns integer PK."""
    row = conn.execute("SELECT id FROM sources WHERE url=?", (url,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO sources (uuid, name, url, scraper, config, active, is_pseudo)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(_uuid.uuid4()), name, url, "rss", "{}", 0, 1),
    )
    conn.commit()
    return cur.lastrowid


def _source_for_ref_url(url: str) -> tuple[str, str]:
    """Return (source_name, source_base_url) for a reference URL."""
    for prefix, name, base_url in _PSEUDO_SOURCE_PATTERNS:
        if url.startswith(prefix):
            return name, base_url
    try:
        host = urlparse(url).hostname or ""
        if host:
            return host, f"https://{host}/"
    except Exception:
        pass
    return _FALLBACK_SOURCE_NAME, _FALLBACK_SOURCE_URL


def _title_from_url(url: str) -> str:
    path = url.rstrip("/").rsplit("/", 1)[-1]
    # Strip common file extensions
    for ext in (".html", ".htm", ".php", ".asp", ".aspx"):
        if path.lower().endswith(ext):
            path = path[:-len(ext)]
            break
    return path.replace("-", " ").replace("_", " ").title() or url


def sync_scenarios(content_path: str = None) -> dict:
    if content_path is None:
        content_path = get_setting("content_path", CONTENT_PATH)

    scenario_urls: set[str] = set()
    for adel_file in glob.glob(os.path.join(content_path, "**", "*.adel"), recursive=True):
        try:
            with open(adel_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('reference "') and line.endswith('"'):
                        url = line[11:-1]
                        if url.startswith("http"):
                            scenario_urls.add(url)
        except OSError:
            pass

    if not scenario_urls:
        return {"found": 0, "updated": 0, "unmatched": []}

    conn = get_conn()
    updated = 0
    unmatched: list[str] = []

    rejected = {r[0] for r in conn.execute("SELECT domain FROM rejected_domains").fetchall()}

    for url in scenario_urls:
        # Mark existing real articles that are referenced in a scenario
        cur = conn.execute(
            "UPDATE articles SET status='has_scenario'"
            " WHERE url=? AND status!='has_scenario'"
            " AND source_id IN (SELECT id FROM sources WHERE is_pseudo=0)",
            (url,),
        )
        if cur.rowcount:
            updated += cur.rowcount
        else:
            exists = conn.execute(
                "SELECT 1 FROM articles a"
                " JOIN sources s ON a.source_id=s.id"
                " WHERE a.url=? AND (s.is_pseudo IS NULL OR s.is_pseudo=0)",
                (url,),
            ).fetchone()
            if exists:
                # Already marked has_scenario in a previous sync
                pass
            else:
                # URL not in feed — collect for caller but do NOT create a stub
                host = urlparse(url).hostname or ""
                if host.lower() not in rejected:
                    unmatched.append(url)

    conn.commit()
    conn.close()
    return {"found": len(scenario_urls), "updated": updated, "unmatched": unmatched}


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key: str, value: str):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def get_all_settings() -> dict:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


# ── Site rules ────────────────────────────────────────────────────────────────

def get_site_rules() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM site_rules ORDER BY LENGTH(pattern) DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_rule(pattern: str, rule_start: str = "", rule_end: str = "") -> dict:
    conn = get_conn()
    conn.execute(
        "INSERT INTO site_rules (pattern, rule_start, rule_end) VALUES (?, ?, ?) "
        "ON CONFLICT(pattern) DO UPDATE SET rule_start=excluded.rule_start, rule_end=excluded.rule_end",
        (pattern, rule_start or "", rule_end or ""),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM site_rules WHERE pattern=?", (pattern,)).fetchone()
    conn.close()
    return dict(row)


def delete_rule(rule_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM site_rules WHERE id=?", (rule_id,))
    conn.commit()
    conn.close()


def rule_for_url(url: str) -> dict | None:
    """Return the longest-matching pattern rule for a URL."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM site_rules").fetchall()
    conn.close()
    best: dict | None = None
    best_len = 0
    for r in rows:
        d = dict(r)
        if d["pattern"] in url and len(d["pattern"]) > best_len:
            best = d
            best_len = len(d["pattern"])
    return best


def _import_termbrowser_config(conn) -> int:
    """Auto-import ~/.config/termbrowser/config.toml into site_rules."""
    path = os.path.expanduser("~/.config/termbrowser/config.toml")
    if not os.path.exists(path):
        return 0
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return 0
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        count = 0
        for pattern, rule in data.get("sites", {}).items():
            conn.execute(
                "INSERT OR IGNORE INTO site_rules (pattern, rule_start, rule_end) VALUES (?, ?, ?)",
                (pattern, rule.get("start", ""), rule.get("end", "")),
            )
            count += 1
        conn.commit()
        return count
    except Exception:
        return 0


# ── MISP Warning Lists ────────────────────────────────────────────────────────

# Attributes that identify domain/hostname lists
_WL_HOSTNAME_ATTRS = {'hostname', 'domain', 'url', 'domain|ip'}
# Attributes that identify IP lists
_WL_IP_ATTRS       = {'ip-src', 'ip-dst', 'ip', 'cidr'}
# Attributes that identify hash lists
_WL_HASH_ATTRS     = {'md5', 'sha1', 'sha224', 'sha256', 'sha512',
                      'filename|md5', 'filename|sha1', 'filename|sha256'}
# Skip lists larger than this — avoids multi-million-row lists
_WL_MAX_ENTRIES    = 50_000

_MISP_ZIP_URL = "https://github.com/MISP/misp-warninglists/archive/refs/heads/main.zip"


def sync_warninglists() -> dict:
    """Download MISP warning lists and store entries in the warninglists table."""
    import requests as _req
    r = _req.get(_MISP_ZIP_URL, timeout=120,
                 headers={"User-Agent": "ThreatBrowser/1.0"})
    r.raise_for_status()

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    conn = get_conn()
    conn.execute("DELETE FROM warninglists")
    conn.commit()

    lists_done = entries_total = skipped = 0

    for zname in zf.namelist():
        if not zname.endswith("/list.json"):
            continue
        try:
            data = json.loads(zf.read(zname))
        except Exception:
            continue

        attrs = set(data.get("matching_attributes", []))
        is_host  = bool(attrs & _WL_HOSTNAME_ATTRS)
        is_ip    = bool(attrs & _WL_IP_ATTRS)
        is_hash  = bool(attrs & _WL_HASH_ATTRS)

        if not (is_host or is_ip or is_hash):
            continue

        entries = data.get("list", [])
        if len(entries) > _WL_MAX_ENTRIES:
            skipped += 1
            continue

        if is_host:
            etype = "hostname"
        elif is_hash:
            etype = "hash"
        else:
            etype = "ip"

        batch = []
        for e in entries:
            e = e.strip().lstrip(".").lower()
            if e:
                batch.append((e, etype))

        if batch:
            conn.executemany(
                "INSERT OR IGNORE INTO warninglists (entry, entry_type) VALUES (?, ?)",
                batch,
            )
            conn.commit()
            entries_total += len(batch)
            lists_done += 1

    conn.close()
    zf.close()
    return {"lists": lists_done, "skipped_large": skipped, "entries": entries_total}


def get_warninglist_stats() -> dict:
    conn = get_conn()
    rows = conn.execute(
        "SELECT entry_type, COUNT(*) FROM warninglists GROUP BY entry_type"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM warninglists").fetchone()[0]
    conn.close()
    return {"total": total, **{r[0]: r[1] for r in rows}}


# ── IOC extraction ─────────────────────────────────────────────────────────────

_IP_RE     = re.compile(r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b')
_SHA256_RE = re.compile(r'\b[0-9a-fA-F]{64}\b')
_SHA1_RE   = re.compile(r'\b[0-9a-fA-F]{40}\b')
_MD5_RE    = re.compile(r'\b[0-9a-fA-F]{32}\b')
_CVE_RE    = re.compile(r'CVE-\d{4}-\d{4,7}', re.IGNORECASE)
_MDURL_RE  = re.compile(r'\]\(((https?|ftp)://[^)\s]+)\)')
_BAREURL_RE= re.compile(r'(?<!\()(https?://[^\s\])">,]+)')

_KNOWN_TLDS = (
    "com|net|org|io|gov|edu|info|biz|ru|cn|de|fr|jp|nl|pl|cz|sk|hu|ro|ua|tr|"
    "ir|sa|ae|il|in|pk|bd|sg|my|id|ph|vn|th|hk|kr|au|nz|cc|tk|xyz|top|"
    "site|online|tech|ly|me|tv|co|uk|us|ca|br|mx|ar|cl|pe|ch|be|at|se|no|"
    "dk|fi|pt|es|it|gr|bg|hr|rs|ba|mk|al|lt|lv|ee|by|kz|az|ge|am|md|so|su"
)
_DOM_RE = re.compile(
    rf'\b([a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?\.(?:[a-z0-9-]+\.)*(?:{_KNOWN_TLDS}))\b',
    re.IGNORECASE,
)


def _defang(text: str) -> str:
    text = re.sub(r'hxxps?', lambda m: m.group().replace('xx', 'tt'), text, flags=re.IGNORECASE)
    text = re.sub(r'\[\.?\]', '.', text)
    text = re.sub(r'(\d+)\[\.?\](\d+)', r'\1.\2', text)
    return text


def _is_private_ip(ip: str) -> bool:
    try:
        p = [int(x) for x in ip.split('.')]
        a, b = p[0], p[1]
        return (a in (0, 10, 127) or a >= 224
                or (a == 172 and 16 <= b <= 31)
                or (a == 192 and b == 168))
    except Exception:
        return True


def extract_iocs(markdown: str, rule_start: str = '', rule_end: str = '') -> dict:
    """Regex-extract IOCs from markdown after applying optional start/end slice."""
    md = markdown
    if rule_start:
        idx = md.find(rule_start)
        if idx >= 0:
            md = md[idx:]
    if rule_end:
        idx = md.find(rule_end)
        if idx >= 0:
            md = md[:idx]

    text = _defang(md)
    out  = {k: [] for k in ('ips', 'sha256', 'sha1', 'md5', 'cves', 'domains', 'urls')}
    seen = {k: set() for k in out}

    def add(cat, val):
        if val not in seen[cat]:
            seen[cat].add(val); out[cat].append(val)

    for m in _CVE_RE.finditer(text):
        add('cves', m.group().upper())

    no_urls = re.sub(r'https?://\S+', '', text)
    for m in _SHA256_RE.finditer(no_urls): add('sha256', m.group().lower())
    for m in _SHA1_RE.finditer(no_urls):
        v = m.group().lower()
        if v not in seen['sha256']: add('sha1', v)
    for m in _MD5_RE.finditer(no_urls):
        v = m.group().lower()
        if v not in seen['sha256'] and v not in seen['sha1']: add('md5', v)

    for m in _IP_RE.finditer(text):
        if not _is_private_ip(m.group()): add('ips', m.group())

    for m in _MDURL_RE.finditer(text): add('urls', m.group(1))
    for m in _BAREURL_RE.finditer(text): add('urls', m.group(1))

    for url in out['urls']:
        try:
            host = urlparse(url).hostname or ''
            if host and '.' in host:
                add('domains', host.lower())
        except Exception:
            pass

    for m in _DOM_RE.finditer(no_urls):
        d = m.group(1).lower()
        if len(d) > 4 and not re.match(r'^\d+\.\d+$', d):
            add('domains', d)

    return out


def _parse_ioc_patterns(raw: str | list | None) -> list[str]:
    """Normalize a raw exclusion value (str or list) into a clean list of patterns."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [p.strip() for p in raw if str(p).strip()]
    return [p.strip() for p in raw.replace('\n', ',').split(',') if p.strip()]


def filter_iocs(
    iocs: dict,
    exclude_patterns: list[str] | None = None,
    article_url: str | None = None,
    apply_warninglists: bool = True,
) -> tuple[dict, int]:
    """Remove entries matching MISP warning lists (optional), custom patterns, or the article's own domain."""
    benign_hosts: set[str] = set()
    benign_ips:   set[str] = set()
    benign_hashes: set[str] = set()
    if apply_warninglists:
        conn = get_conn()
        rows = conn.execute("SELECT entry, entry_type FROM warninglists").fetchall()
        conn.close()
        benign_hosts  = {r[0] for r in rows if r[1] == 'hostname'}
        benign_ips    = {r[0] for r in rows if r[1] == 'ip'}
        benign_hashes = {r[0] for r in rows if r[1] == 'hash'}

    # Automatically exclude the article's own domain (and its registered parent)
    auto_domains: set[str] = set()
    if article_url:
        try:
            host = urlparse(article_url).hostname or ''
            if host:
                auto_domains.add(host.lower())
                parts = host.lower().split('.')
                if len(parts) > 2:
                    auto_domains.add('.'.join(parts[-2:]))
        except Exception:
            pass

    patterns = list(exclude_patterns or [])

    def _domain_excluded(domain: str) -> bool:
        d = domain.lower()
        parts = d.split('.')
        # MISP warning list check (only when enabled)
        for i in range(len(parts) - 1):
            if '.'.join(parts[i:]) in benign_hosts:
                return True
        # Auto-excluded source domain
        for ad in auto_domains:
            if d == ad or d.endswith('.' + ad):
                return True
        # Custom patterns (domain suffix match)
        for pat in patterns:
            if d == pat or d.endswith('.' + pat):
                return True
        return False

    def _url_excluded(url: str) -> bool:
        for ad in auto_domains:
            if ad in url:
                return True
        for pat in patterns:
            if pat in url:
                return True
        return False

    removed = 0
    result  = {}

    result['ips'] = []
    for ip in iocs.get('ips', []):
        if ip in benign_ips or _url_excluded(ip): removed += 1
        else: result['ips'].append(ip)

    result['domains'] = []
    for d in iocs.get('domains', []):
        if _domain_excluded(d): removed += 1
        else: result['domains'].append(d)

    for cat in ('sha256', 'sha1', 'md5'):
        result[cat] = []
        for h in iocs.get(cat, []):
            if h in benign_hashes: removed += 1
            else: result[cat].append(h)

    result['cves'] = iocs.get('cves', [])

    result['urls'] = []
    for url in iocs.get('urls', []):
        if _url_excluded(url): removed += 1
        else: result['urls'].append(url)

    return result, removed


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    conn = get_conn()
    rows = conn.execute("""
        SELECT a.status, COUNT(*)
        FROM articles a JOIN sources s ON a.source_id = s.id
        WHERE (s.is_pseudo IS NULL OR s.is_pseudo = 0)
        GROUP BY a.status
    """).fetchall()
    uncached = conn.execute("""
        SELECT COUNT(*) FROM articles a JOIN sources s ON a.source_id = s.id
        WHERE (s.is_pseudo IS NULL OR s.is_pseudo = 0)
          AND a.cached_at IS NULL AND a.download_status IS NULL
    """).fetchone()[0]
    conn.close()
    d = {r[0]: r[1] for r in rows}
    d["total"] = sum(d.values())
    d["no_scenario"] = d.get("new", 0) + d.get("seen", 0)
    d["uncached"] = uncached
    return d
