"""
Provenance ingestion script.

Pulls new items from Inoreader folder RSS feeds, classifies + summarizes
them with Claude, and writes the JSON files the dashboard (index.html)
reads: data/articles.json and data/watchlist.json.

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
from anthropic import Anthropic

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")

CATEGORIES = [
    "Auction Results",
    "Market Trends",
    "Luxury Brands",
    "Galleries & Institutions",
    "Art News",
]

MAX_ARTICLES_KEPT = 200
HISTORY_DAYS = 7

client = Anthropic()  # reads ANTHROPIC_API_KEY from env
MODEL = "claude-haiku-4-5-20251001"  # fast/cheap; swap for claude-sonnet-5 for deeper analysis


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def fetch_full_text(url, fallback):
    """Best-effort full article text; falls back to the RSS summary."""
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "html.parser")
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = " ".join(paragraphs)
        return text[:6000] if len(text) > 200 else fallback
    except Exception:
        return fallback


def classify_with_claude(headline, text, suggested_category, watchlist_names):
    prompt = f"""You are tagging one article for an art & luxury market intelligence dashboard.

Headline: {headline}
Suggested category (a hint, may be wrong): {suggested_category}
Article text: {text}

Watchlist entities to check for (match if clearly mentioned, by name or obvious alias):
{", ".join(watchlist_names)}

Respond with ONLY a JSON object, no other text, in this exact shape:
{{
  "category": one of {json.dumps(CATEGORIES)},
  "headline": "a clean, concise headline (rewrite the original if needed, under 100 chars)",
  "summary": "two plain sentences summarizing what happened and why it matters",
  "entities": ["names of watchlist entities mentioned, plus any other notable named artists, dealers, houses, or brands in the piece"]
}}"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    return json.loads(raw)


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
            fallback_text = entry.get("summary", "") or entry.get("title", "")
            full_text = fetch_full_text(link, fallback_text) if link else fallback_text

            try:
                result = classify_with_claude(
                    entry.get("title", ""), full_text,
                    feed_cfg.get("suggested_category", "Art News"),
                    watchlist_names,
                )
            except Exception as e:
                print(f"Skipping (classification failed): {entry.get('title')} — {e}")
                continue

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
                "cat": result.get("category", "Art News"),
                "src": source or feed_cfg.get("name", "Unknown"),
                "time": time_str,
                "headline": result.get("headline", entry.get("title", "")),
                "summary": result.get("summary", ""),
                "entities": result.get("entities", []),
                "link": link,
            }
            articles.insert(0, article)
            seen.add(entry_id)
            new_count += 1

            for name in article["entities"]:
                for wl_name in watchlist_names:
                    if wl_name.lower() in name.lower() or name.lower() in wl_name.lower():
                        mention_history[today][wl_name] = mention_history[today].get(wl_name, 0) + 1

    articles = articles[:MAX_ARTICLES_KEPT]

    # trim history to last HISTORY_DAYS days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    mention_history = {d: v for d, v in mention_history.items() if d >= cutoff}

    # build watchlist.json in the shape the dashboard expects
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
