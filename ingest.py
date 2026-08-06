"""
Provenance ingestion script — FREE VERSION (no API key required).

Pulls new items from Inoreader folder RSS feeds and tags them using simple
keyword matching instead of an AI model. Writes the same data/articles.json
and data/watchlist.json files the dashboard (index.html) reads.

Run manually with:  python ingest.py
Normally run on a schedule via .github/workflows/ingest.yml
"""

import json
import os
import re
import time
from datetime import datetime, timezone, timedelta

import feedparser
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")

MAX_ARTICLES_KEPT = 200
HISTORY_DAYS = 7

# Keyword lists used to guess a category from the headline + article text.
# Whichever category scores the most keyword hits wins; ties go to the
# feed's own "suggested_category" from config/feeds.json.
CATEGORY_KEYWORDS = {
    "Auction Results": [
        "auction", "hammer price", "sold for", "lot ", "evening sale",
        "day sale", "estimate", "winning bid", "sale total", "consignor",
    ],
    "Market Trends": [
        "market report", "index", "data show", "trend", "demand for",
        "sales fell", "sales rose", "quarterly", "outperform", "underperform",
        "private sales", "consignment volume",
    ],
    "Luxury Brands": [
        "lvmh", "kering", "richemont", "luxury", "conglomerate", "watch",
        "jewelry", "jewellery", "fashion house", "creative director",
        "hard luxury",
    ],
    "Galleries & Institutions": [
        "gallery", "museum", "institution", "exhibition", "biennale",
        "fair", "curator", "retrospective", "acquisition by",
    ],
}
DEFAULT_CATEGORY = "Art News"


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def clean_html(raw):
    return BeautifulSoup(raw or "", "html.parser").get_text(" ", strip=True)


def fetch_full_text(url, fallback):
    """Best-effort full article text, used only for better keyword matching."""
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "html.parser")
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = " ".join(paragraphs)
        return text[:6000] if len(text) > 200 else fallback
    except Exception:
        return fallback


def guess_category(text, suggested_category):
    text_lower = text.lower()
    scores = {cat: sum(text_lower.count(kw) for kw in kws) for cat, kws in CATEGORY_KEYWORDS.items()}
    best_cat = max(scores, key=scores.get)
    if scores[best_cat] > 0:
        return best_cat
    return suggested_category if suggested_category in CATEGORY_KEYWORDS or suggested_category == DEFAULT_CATEGORY else DEFAULT_CATEGORY


def make_summary(raw_summary, max_len=240):
    text = clean_html(raw_summary)
    if len(text) <= max_len:
        return text
    truncated = text[:max_len].rsplit(" ", 1)[0]
    return truncated + "…"


def find_entities(text, watchlist_names):
    text_lower = text.lower()
    return [name for name in watchlist_names if name.lower() in text_lower]


def main():
    feeds_cfg = load_json(os.path.join(CONFIG_DIR, "feeds.json"), {"feeds": []})["feeds"]
    watchlist_cfg = load_json(os.path.join(CONFIG_DIR, "watchlist.json"), {"entities": []})["entities"]
    watchlist_names = [e["name"] for e in watchlist_cfg]

    seen = set(load_json(os.path.join(DATA_DIR, "seen.json"), []))
    articles = load_json(os.path.join(DATA_DIR, "articles.json"), [])
    mention_history = load_json(os.path.join(DATA_DIR, "mention_history.json"), {})

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mention_history.setdefault(today, {})

    new_count = 0

    for feed_cfg in feeds_cfg:
        url = feed_cfg.get("feed_url", "")
        if not url or "REPLACE_WITH" in url:
            continue
        parsed = feedparser.parse(url)

        for entry in parsed.entries:
            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in seen:
                continue

            link = entry.get("link", "")
            raw_summary = entry.get("summary", "") or entry.get("title", "")
            headline = clean_html(entry.get("title", ""))

            full_text = fetch_full_text(link, raw_summary) if link else raw_summary
            classify_text = f"{headline} {full_text}"

            category = guess_category(classify_text, feed_cfg.get("suggested_category", DEFAULT_CATEGORY))
            summary = make_summary(raw_summary)
            entities = find_entities(classify_text, watchlist_names)

            source = ""
            if hasattr(parsed.feed, "title"):
                source = parsed.feed.title
            elif "source" in entry:
                source = entry.source.get("title", "")

            published = entry.get("published_parsed")
            time_str = (
                time.strftime("%m-%d %H:%M", published) if published
                else datetime.now(timezone.utc).strftime("%m-%d %H:%M")
            )

            article = {
                "cat": category,
                "src": source or feed_cfg.get("name", "Unknown"),
                "time": time_str,
                "headline": headline,
                "summary": summary,
                "entities": entities,
                "link": link,
            }
            articles.insert(0, article)
            seen.add(entry_id)
            new_count += 1

            for name in entities:
                mention_history[today][name] = mention_history[today].get(name, 0) + 1

    articles = articles[:MAX_ARTICLES_KEPT]

    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    mention_history = {d: v for d, v in mention_history.items() if d >= cutoff}

    days_sorted = sorted(mention_history.keys())
    watchlist_out = []
    for ent in watchlist_cfg:
        name = ent["name"]
        spark = [mention_history.get(d, {}).get(name, 0) for d in days_sorted] or [0]
        first, last = spark[0], spark[-1]
        delta_pct = ((last - first) / first * 100) if first else (100.0 if last else 0.0)
        watchlist_out.append({
            "name": name,
            "meta": ent.get("type", ""),
            "delta": f"{'+' if delta_pct >= 0 else ''}{delta_pct:.1f}%",
            "dir": "up" if delta_pct >= 0 else "down",
            "spark": spark,
        })

    save_json(os.path.join(DATA_DIR, "articles.json"), articles)
    save_json(os.path.join(DATA_DIR, "watchlist.json"), watchlist_out)
    save_json(os.path.join(DATA_DIR, "mention_history.json"), mention_history)
    save_json(os.path.join(DATA_DIR, "seen.json"), list(seen))

    print(f"Done. {new_count} new article(s) processed.")


if __name__ == "__main__":
    main()
