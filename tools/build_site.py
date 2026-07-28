#!/usr/bin/env python3
"""Rebuild the static site data from the version-controlled sources + last publish.

This is what the GitHub Action runs on every schedule tick and every push that
touches sources/. It reuses the real fetcher and DB code, so a CI build behaves
exactly like the desktop app's "Fetch All".

Flow:
  1. Fresh throwaway SQLite (TB_DB points at a temp file).
  2. Load feed definitions from sources/*.json  (what the user edits).
  3. Seed prior articles from web/data/articles.json so history ACCUMULATES —
     RSS feeds only list recent items, so without this every old article vanishes.
     first_seen is preserved; only genuinely new URLs get today's timestamp.
  4. Fetch every active source (threaded), upsert new articles.
  5. Export sources + articles to web/data/ for Pages.

Usage: build_site.py <repo_root>
"""

import concurrent.futures
import glob
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone


def load_source_configs(repo):
    """Every {"version":1,"sources":[...]} file under sources/ (sekoia.json format)."""
    feeds = []
    for path in sorted(glob.glob(os.path.join(repo, "sources", "*.json"))):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        feeds.extend(doc.get("sources", []) if isinstance(doc, dict) else doc)
    return feeds


def seed_prior_articles(db, feeds_by_url):
    """Re-insert previously published articles so the feed accumulates over time."""
    prior = os.path.join("web", "data", "articles.json")
    if not os.path.exists(prior):
        return 0
    with open(prior, encoding="utf-8") as fh:
        doc = json.load(fh)
    conn = sqlite3.connect(db)
    n = 0
    now = datetime.now(timezone.utc).isoformat()
    for a in doc.get("articles", []):
        sid = feeds_by_url.get_source_id_for_article(a)
        if sid is None:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO articles (uuid, source_id, title, url, published_at, first_seen)"
            " VALUES (?,?,?,?,?,?)",
            (_uuid(), sid, a.get("t"), a["u"], a.get("d"), a.get("fs") or now),
        )
        n += cur.rowcount
    conn.commit()
    conn.close()
    return n


def _uuid():
    import uuid
    return str(uuid.uuid4())


class SourceIndex:
    """Maps an exported article back to a source_id via its source's stable id/name."""
    def __init__(self, db):
        import uuid
        ns = uuid.UUID("5f3e9b1c-0000-4000-8000-746872656174")
        conn = sqlite3.connect(db)
        self.by_stable = {}
        for sid, url in conn.execute("SELECT id, url FROM sources").fetchall():
            self.by_stable[str(uuid.uuid5(ns, url))] = sid
        conn.close()

    def get_source_id_for_article(self, a):
        return self.by_stable.get(a.get("s"))


def main():
    repo = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    # db.py / fetcher.py live in the repo root, but this script lives in tools/,
    # so sys.path[0] is tools/. Put the repo root on the path so `import db` works
    # regardless of how the workflow invokes us.
    sys.path.insert(0, repo)
    os.chdir(repo)

    tmp = tempfile.mkdtemp(prefix="tb_build_")
    os.environ["TB_DB"] = os.path.join(tmp, "build.db")
    os.environ["TB_CACHE"] = os.path.join(tmp, "cache")
    os.environ.setdefault("TB_CONTENT", os.path.join(tmp, "content"))

    import db
    import fetcher
    db.init_db()
    # init_db() seeds a default source set into an empty DB. The hosted build is
    # driven entirely by sources/*.json, so start from a clean slate — no articles
    # exist yet, so nothing cascades.
    _c = sqlite3.connect(os.environ["TB_DB"]); _c.execute("DELETE FROM sources"); _c.commit(); _c.close()

    feeds = load_source_configs(repo)
    print(f"Loaded {len(feeds)} source definitions from sources/")

    url_to_id = {}
    dupes = 0
    for f in feeds:
        if not f.get("url"):
            continue
        if f["url"] in url_to_id:   # same feed URL in two config files — first wins
            dupes += 1
            continue
        row = db.add_source(
            f["name"], f["url"], f.get("scraper", "rss"),
            f.get("config") or {}, f.get("tags") or [],
        )
        url_to_id[f["url"]] = row["id"]
        if not f.get("active", True):
            db.update_source(row["uuid"], active=False)
    if dupes:
        print(f"Skipped {dupes} duplicate source URL(s) across sources/*.json")

    seeded = seed_prior_articles(os.environ["TB_DB"], SourceIndex(os.environ["TB_DB"]))
    print(f"Seeded {seeded} prior articles (accumulated history)")

    active = [s for s in db.get_sources(active_only=True)]
    # TB_ONLY=<comma-separated substrings> fetches just the matching sources (by
    # name or URL) — for adding/debugging one feed without re-fetching all of them.
    # Prior articles for every source were already seeded above, so the others'
    # history is preserved in the export; only the matched sources are re-fetched.
    only = os.environ.get("TB_ONLY", "").strip()
    if only:
        needles = [n.strip().lower() for n in only.split(",") if n.strip()]
        active = [s for s in active
                  if any(n in s["name"].lower() or n in s["url"].lower() for n in needles)]
        print(f"TB_ONLY={only!r} → fetching {len(active)} matching source(s): "
              f"{', '.join(s['name'] for s in active) or '(none matched)'}")
    print(f"Fetching {len(active)} active sources…")

    def _one(src):
        try:
            arts = fetcher.fetch_source(src)
            return db.upsert_articles(arts), None
        except Exception as exc:
            return 0, f"{src['name']}: {exc}"

    new_total, errors = 0, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for n, err in pool.map(_one, active):
            new_total += n
            if err:
                errors.append(err)
    print(f"Fetched: {new_total} new articles, {len(errors)} source errors")
    for e in errors[:20]:
        print("  !", e)

    # Export for Pages.
    sys.path.insert(0, os.path.join(repo, "tools"))
    import export_web
    conn = export_web._load(os.environ["TB_DB"])
    generated_at = datetime.now(timezone.utc).isoformat()
    # Publish a *derived* sources file for the client — never touch the user's
    # hand-edited sources/ tree.
    export_web.export_sources(conn, os.path.join(repo, "web", "data", "sources.json"))
    _, narts, _ = export_web.export_articles(conn, repo, generated_at)

    # Publish the one-and-only UI into web/ so Pages serves the same interface the
    # desktop app bundles. static/index.html is the single source of truth.
    import shutil
    shutil.copyfile(os.path.join(repo, "static", "index.html"),
                    os.path.join(repo, "web", "index.html"))
    print(f"Exported {narts} articles + sources + UI to web/")


if __name__ == "__main__":
    main()
