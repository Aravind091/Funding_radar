# Funding Radar

Scans two official public sources daily for new open funding calls matching your keywords, and keeps a browsable dashboard. No email setup needed.

- **BDNS** (infosubvenciones.es) — covers all Spanish public administrations: state ministries (MCIN/AEI included), autonomous communities (Catalonia/AGAUR included), provincial and local bodies.
- **EU Funding & Tenders Portal** (SEDIA) — Horizon Europe, Clean Aviation, and other centrally-managed EU calls.

## Setup (5 minutes)

1. **Create the repo.** Go to github.com → New repository (e.g. `funding-radar`, private is fine). Upload everything in this folder — drag-and-drop on the GitHub web UI works, no git command line needed. **Make sure the `.github/workflows/scan.yml` file lands at that exact path** — GitHub only picks up workflows from `.github/workflows/`, so if your upload flattens folders, create that path manually and paste the file in.

2. **Turn on the dashboard.** In the repo: **Settings → Pages** → under "Build and deployment" set Source to **Deploy from a branch**, branch `main`, folder `/docs` → Save. After a minute or two it's live at `https://<your-username>.github.io/funding-radar/`.

3. **Run it once to test.** Go to the **Actions** tab → "Funding Radar Scan" → **Run workflow**. Wait for it to finish (green check), then open your dashboard link — you should see today's scan logged, and any matching calls listed.

That's it. It'll now run automatically every day at 07:00 UTC. Bookmark the dashboard URL on your phone and check it whenever.

## Edit your keywords any time

Open `keywords.json` on GitHub (click the file → pencil icon → edit → commit). No need to touch the Python code.

## Change the schedule

Edit the `cron` line in `.github/workflows/scan.yml`. It's currently daily at 07:00 UTC.

## Want email alerts later?

Add three repo secrets (**Settings → Secrets and variables → Actions**): `EMAIL_USER`, `EMAIL_PASS` (a Gmail app password), `EMAIL_TO`. Then add an `env:` block back into the "Run scan" step in `.github/workflows/scan.yml` passing those secrets through. `scan.py` already has the sending logic — it just skips silently if those aren't set, which is why dashboard-only mode works out of the box.

## Notes and honest limitations

- Both APIs are the same ones the public portals use in-browser, reverse-engineered since neither publishes a formal developer schema. Stable in practice, not contractually guaranteed. If a scan shows 0 results from one source when you'd expect matches, check the dashboard's "warnings" count for that run.
- BDNS's search only exposes the call **title**, not full text, so matching happens against titles — keep keywords specific.
- The EU search filters out calls whose status text contains "closed"; forthcoming and open calls both come through.
- Nothing here submits anything anywhere — it only reads public data. Applying is still on you.
