#!/usr/bin/env python3
"""Export the ThreatBrowser DB into the static assets a browser-hosted build needs.

Two outputs, both git-friendly and small:

  sources/sources.json   Version-controlled source definitions (sekoia.json format).
                         Becomes the source-of-truth the GitHub Action fetches from.

  web/data/articles.json Pruned article *metadata* (no cached HTML/favicons). Sharded
  web/data/shard-*.json  by month so the browser lazy-loads recent data first.
  web/data/manifest.json Index of shards + counts for the client.

Per-user state (seen/read, notes, local source toggles) is intentionally NOT exported —
that lives in the browser's IndexedDB in the hosted build.

Usage: export_web.py <db_path> <out_dir>
"""

import gzip
import json
import os
import sys
import uuid

# Stable namespace so a source URL always maps to the same id across runs and
# machines — per-user state and article->source links stay valid between builds.
_SRC_NS = uuid.UUID("5f3e9b1c-0000-4000-8000-746872656174")


def source_id(url: str) -> str:
    return str(uuid.uuid5(_SRC_NS, url))


def _load(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def export_sources(conn, dest_path):
    rows = conn.execute(
        "SELECT name, url, scraper, config, tags, active FROM sources "
        "WHERE (is_pseudo IS NULL OR is_pseudo=0) ORDER BY name"
    ).fetchall()
    sources = []
    for r in rows:
        sources.append({
            "id": source_id(r["url"]),   # stable, derived from URL
            "name": r["name"],
            "url": r["url"],
            "scraper": r["scraper"],
            "config": json.loads(r["config"] or "{}"),
            "tags": json.loads(r["tags"] or "[]"),
            "active": bool(r["active"]),
        })
    doc = {"version": 1, "sources": sources}
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
    return dest_path, len(sources)


def export_articles(conn, out_dir, generated_at=""):
    src_url = {r["id"]: r["url"] for r in conn.execute("SELECT id, url FROM sources").fetchall()}
    src_name = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM sources").fetchall()}

    rows = conn.execute(
        "SELECT a.title, a.url, a.published_at, a.first_seen, a.source_id "
        "FROM articles a JOIN sources s ON a.source_id=s.id "
        "WHERE (s.is_pseudo IS NULL OR s.is_pseudo=0) "
        "ORDER BY COALESCE(a.published_at, a.first_seen) DESC"
    ).fetchall()

    articles = []
    for r in rows:
        sid = r["source_id"]
        articles.append({
            "u": r["url"],                       # url is the stable article key
            "t": r["title"],
            "d": r["published_at"],              # published (may be null)
            "fs": r["first_seen"],               # first seen — preserved to accumulate
            "s": source_id(src_url.get(sid, "")),
            "sn": src_name.get(sid, ""),
        })

    data_dir = os.path.join(out_dir, "web", "data")
    os.makedirs(data_dir, exist_ok=True)
    # Single file — the whole set is <1 MB gzipped, so the client loads it in one
    # request and filters/searches locally. Revisit sharding only if it grows large.
    doc = {"generated_at": generated_at, "total": len(articles), "articles": articles}
    with open(os.path.join(data_dir, "articles.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    return data_dir, len(articles), 1


def _dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total


def _gz_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            with open(os.path.join(root, f), "rb") as fh:
                total += len(gzip.compress(fh.read()))
    return total


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    db_path, out_dir = sys.argv[1], sys.argv[2]
    from datetime import datetime, timezone
    generated_at = datetime.now(timezone.utc).isoformat()
    conn = _load(db_path)

    spath, nsrc = export_sources(conn, os.path.join(out_dir, "sources", "sources.json"))
    dpath, narts, _ = export_articles(conn, out_dir, generated_at)

    web_data = os.path.join(out_dir, "web", "data")
    raw = _dir_size(web_data)
    gz = _gz_size(web_data)
    print(f"sources : {nsrc} -> {spath}")
    print(f"articles: {narts} -> {dpath}/articles.json")
    print(f"web/data raw:  {raw/1e6:.2f} MB")
    print(f"web/data gzip: {gz/1e6:.2f} MB  (what Pages actually serves)")


if __name__ == "__main__":
    main()
