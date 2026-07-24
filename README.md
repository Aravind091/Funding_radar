# Funding Radar

Scans two official public sources every Monday for new open funding calls matching your keywords, emails you a short summary, and keeps a browsable dashboard.

- **BDNS** (infosubvenciones.es) — covers all Spanish public administrations: state ministries (MCIN/AEI included), autonomous communities (Catalonia/AGAUR included), provincial and local bodies. Also picks up Spanish co-funding of ERA-NET / M-ERA.NET calls, since AEI publishes those into BDNS too.
- **EU Funding & Tenders Portal** (SEDIA) — Horizon Europe, Clean Aviation, and other centrally-managed EU calls.

## Setup (5 minutes)

1. **Create the repo.** Go to github.com → New repository (e.g. `funding-radar`, private is fine). Upload everything in this folder — drag-and-drop on the GitHub web UI works, no git command line needed. **Make sure the `.github/workflows/scan.yml` file lands at that exact path** — GitHub only picks up workflows from `.github/workflows/`, so if your upload flattens folders, create that path manually and paste the file in.

2. **Turn on the dashboard.** In the repo: **Settings → Pages** → under "Build and deployment" set Source to **Deploy from a branch**, branch `main`, folder `/docs` → Save. After a minute or two it's live at `https://<your-username>.github.io/funding-radar/`.

3. **Turn on email alerts (optional but recommended).** Add three repo secrets: **Settings → Secrets and variables → Actions → New repository secret**:
   - `EMAIL_USER` — your Gmail address
   - `EMAIL_PASS` — a [Gmail app password](https://myaccount.google.com/apppasswords) (not your real password — needs 2-Step Verification turned on first)
   - `EMAIL_TO` — where alerts should go (can be the same address)

   `scan.yml` already passes these through to `scan.py`, which sends a short email ("Funding Radar: N new call(s) this week") listing just the new items with deadlines and links. If you skip this step, it silently stays dashboard-only.

4. **Run it once to test.** Go to the **Actions** tab → "Funding Radar Scan" → **Run workflow**. Wait for it to finish (green check, normally well under a minute), then open your dashboard link — you should see today's scan logged, and any matching calls listed.

That's it. It now runs automatically every **Monday at 07:00 UTC** (09:00 Girona time in summer). Bookmark the dashboard URL on your phone.

## Edit your keywords any time

Open `keywords.json` on GitHub (click the file → pencil icon → edit → commit). No need to touch the Python code.

## Change the schedule

Edit the `cron` line in `.github/workflows/scan.yml`. It's currently `0 7 * * 1` — Monday at 07:00 UTC. The last number is the day of week (0=Sunday ... 6=Saturday).

## Deadlines

For EU calls, the deadline comes directly from the search result. For BDNS calls, the search endpoint only returns a publication date, so `scan.py` makes one extra lookup per match to fetch the actual deadline from the record's detail page. This is a best-effort lookup against an undocumented field — if it can't find a deadline, the dashboard just falls back to showing the publication date instead.

## Notes and honest limitations

- Both APIs are the same ones the public portals use in-browser, reverse-engineered since neither publishes a formal developer schema. Stable in practice, not contractually guaranteed. If a scan shows 0 results from one source when you'd expect matches, check the dashboard's "warnings" count for that run.
- BDNS's search only exposes the call **title**, not full text, so matching happens against titles — keep keywords specific.
- The EU search filters out calls whose status text contains "closed" and anything with a deadline already in the past, and only counts a match if the keyword genuinely appears in the title (not just loosely "related" per the search engine's own relevance ranking).
- The workflow has a 10-minute hard timeout, so a stalled request can't hang it indefinitely — a normal run finishes in well under a minute.
- Nothing here submits anything anywhere — it only reads public data. Applying is still on you.
- **POCTEFA** (Spain-France-Andorra cross-border cooperation) isn't covered — it has no public API, just a plain webpage, so it would need a separate HTML-scraping source. Left out for now as a deliberate scope decision.
