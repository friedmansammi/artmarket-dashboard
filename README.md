# Provenance — setup

## 1. Create the repo
Create a new GitHub repo and put these files in it, alongside `index.html`
(the dashboard — the file called `provenance-dashboard.html` from the chat;
rename it to `index.html` at the repo root).

Folder layout should end up:
```
index.html
ingest.py
requirements.txt
config/feeds.json
config/watchlist.json
data/articles.json
data/watchlist.json
data/seen.json
data/mention_history.json
.github/workflows/ingest.yml
```

## 2. Get your Inoreader folder feed URLs
In Inoreader, open a folder, click its ••• menu, and choose **Generate feed**
(sometimes labeled "Folder RSS"). Copy that URL into `config/feeds.json` for
the matching entry. Do this for each folder you want to pull from. Delete or
leave blank any you don't use — the script skips entries still marked
`REPLACE_WITH_INOREADER_FOLDER_FEED_URL`.

Note: this feature requires an Inoreader Pro/Supporter plan. If you're on
the free plan, let me know and I'll write an alternative using the
Inoreader API (OAuth) instead — a bit more setup, but works on any plan.

## 3. Edit your watchlist
Add/remove entities in `config/watchlist.json` — auction houses, brands,
fairs, artists, dealers, whatever you want trend lines for.

## 4. Add your Anthropic API key
In the repo: Settings → Secrets and variables → Actions → New repository
secret → name it `ANTHROPIC_API_KEY`, paste your key.

## 5. Turn on GitHub Pages
Settings → Pages → Deploy from branch → select your main branch, root
folder. Your dashboard will be live at
`https://<your-username>.github.io/<repo-name>/`.

## 6. Run it
The workflow runs automatically every hour. To test it immediately: go to
the **Actions** tab → "Ingest feeds" → **Run workflow**. Check the `data/`
folder for updated JSON afterward, then refresh your Pages URL.

## Notes
- The script uses `claude-haiku-4-5-20251001` for classification/summarizing
  since it's fast and cheap for this volume — swap the `MODEL` constant in
  `ingest.py` for `claude-sonnet-5` if you want deeper analysis on the
  "week in brief" style copy later.
- Full article text is fetched best-effort from each link; if that fails it
  falls back to the RSS summary, so nothing breaks if a site blocks scraping.
- `data/seen.json` prevents reprocessing the same article twice — don't
  delete it unless you want a full re-ingest.
