"""
Provenance ingestion script — free AI version, using Google's Gemini API
(free tier, no credit card) for summaries/categorization. Falls back to
keyword categorization + extractive summarization if the API call fails
for any reason, so the pipeline never breaks.

Requires a GEMINI_API_KEY environment variable / repo secret.
Get a free key (no credit card) at https://aistudio.google.com

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
SUMMARY_SENTENCES = 3
SUMMARY_MAX_CHARS = 500

CATEGORIES = [
    "Auction Results",
    "Market Trends",
    "Luxury Brands",
    "Galleries & Institutions",
    "Art News",
]

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

STOPWORDS = set("""
a an the and or but if while is are was were be been being of to in on for
with as by at from this that these those it its its' you your he she they
them his her their i we our us not no nor so than then too very can will
just should now also more most other some such only own same all any both
each few had has have having do does did doing what which who whom into
about above below up down out over under again further once here there when
where why how s t can will don should
""".split())

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)


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


BOILERPLATE_MARKERS = [
    "cookie", "newsletter", "privacy policy", "we use vendors",
    "accept all", "manage your privacy", "third-party partners",
    "terms of service", "sign up", "subscribe",
]

def fetch_full_text(url, fallback):
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "html.parser")
        paragraphs = []
        for p in soup.find_all("p"):
            text = p.get_text(" ", strip=True)
            if len(text) < 40:
                continue
            if any(marker in text.lower() for marker in BOILERPLATE_MARKERS):
                continue
            paragraphs.append(text)
        text = " ".join(paragraphs)
        return text[:8000] if len(text) > 200 else fallback
    except Exception:
        return fallback


# ---------- AI path (Gemini) ----------

def classify_with_gemini(headline, text, suggested_category, watchlist_names):
    if not GEMINI_API_KEY:
        raise RuntimeError("No GEMINI_API_KEY set")

    prompt = f"""You are tagging one article for an art & luxury market intelligence dashboard.

Headline: {headline}
Suggested category (a hint, may be wrong): {suggested_category}
Article text: {text[:6000]}

Watchlist entities to check for (match if clearly mentioned, by name or obvious alias):
{", ".join(watchlist_names)}

Respond with ONLY a JSON object, no other text, no markdown fences, in this exact shape:
{{
  "category": one of {json.dumps(CATEGORIES)},
  "headline": "a clean, concise headline (rewrite the original if needed, under 100 chars)",
  "summary": "two plain sentences summarizing what happened and why it matters",
  "entities": ["names of watchlist entities mentioned, plus any other notable named artists, dealers, houses, or brands in the piece"]
}}"""

    resp = requests.post(
        GEMINI_URL,
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    return json.loads(raw)


# ---------- Free fallback path (keyword + extractive) ----------

def guess_category(text, suggested_category):
    text_lower = text.lower()
    scores = {cat: sum(text_lower.count(kw) for kw in kws) for cat, kws in CATEGORY_KEYWORDS.items()}
    best_cat = max(scores, key=scores.get)
    if scores[best_cat] > 0:
        return best_cat
    return suggested_category if suggested_category in CATEGORY_KEYWORDS or suggested_category == DEFAULT_CATEGORY else DEFAULT_CATEGORY


def split_sentences(text):
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return [s.strip() for s in sentences if len(s.strip()) > 20]


def extractive_summary(text, num_sentences=SUMMARY_SENTENCES, max_chars=SUMMARY_MAX_CHARS):
    text = clean_html(text)
    sentences = split_sentences(text)
    if not sentences:
        return text[:max_chars]

    if len(sentences) <= num_sentences:
        summary = " ".join(sentences)
    else:
        words = re.findall(r"[a-zA-Z']+", text.lower())
        freq = {}
        for w in words:
            if w in STOPWORDS or len(w) < 3:
                continue
            freq[w] = freq.get(w, 0) + 1
        if not freq:
            summary = " ".join(sentences[:num_sentences])
        else:
            max_freq = max(freq.values())
            for w in freq:
                freq[w] /= max_freq
            scored = []
            for idx, sent in enumerate(sentences):
                sent_words = re.findall(r"[a-zA-Z']+", sent.lower())
                if not sent_words:
                    continue
                score = sum(freq.get(w, 0) for w in sent_words) / len(sent_words)
                scored.append((score, idx, sent))
            top = sorted(scored, key=lambda x: -x[0])[:num_sentences]
            top_in_order = sorted(top, key=lambda x: x[1])
            summary = " ".join(s for _, _, s in top_in_order)

    if len(summary) > max_chars:
        summary = summary[:max_chars].rsplit(" ", 1)[0] + "…"
    return summary


def find_entities(text, watchlist_names):
    text_lower = text.lower()
    return [name for name in watchlist_names if name.lower() in text_lower]


def process_article(headline, full_text, suggested_category, watchlist_names):
    """Try Gemini first; fall back to free keyword+extractive path on any failure."""
    try:
        result = classify_with_gemini(headline, full_text, suggested_category, watchlist_names)
        return {
            "cat": result.get("category", DEFAULT_CATEGORY),
            "headline": result.get("headline", headline),
            "summary": result.get("summary", ""),
            "entities": result.get("entities", []),
        }
    except Exception as e:
        print(f"Gemini call failed, using free fallback for: {headline} — {e}")
        classify_text = f"{headline} {full_text}"
        return {
            "cat": guess_category(classify_text, suggested_category),
            "headline": headline,
            "summary": extractive_summary(full_text),
            "entities": find_entities(classify_text, watchlist_names),
        }


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

            result = process_article(
                headline, full_text,
                feed_cfg.get("suggested_category", DEFAULT_CATEGORY),
                watchlist_names,
            )
            time.sleep(4)  # stay comfortably under Gemini's free-tier rate limit

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
                "cat": result["cat"],
                "src": source or feed_cfg.get("name", "Unknown"),
                "time": time_str,
                "headline": result["headline"],
                "summary": result["summary"],
                "entities": result["entities"],
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
