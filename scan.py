#!/usr/bin/env python3
"""
Funding Radar
=============
Scans two official, free public data sources for new open funding calls
matching a keyword list, and alerts on anything new:

1. BDNS (Base de Datos Nacional de Subvenciones) - covers ALL Spanish public
   administrations that publish grants: state ministries (incl. MCIN/AEI),
   autonomous communities (incl. Catalonia/AGAUR), provincial and local
   bodies. Official free API, no key required.
   https://www.infosubvenciones.es/bdnstrans/GE/es/index

2. EU Funding & Tenders Portal (SEDIA search API) - covers Horizon Europe,
   Clean Aviation, and other centrally-managed EU calls.
   https://ec.europa.eu/info/funding-tenders/opportunities/portal

Both are undocumented-but-public REST endpoints reverse-engineered from the
portals' own web front-ends (there is no official published schema for
either). That means: they work today, but a future redesign of either portal
could change field names or break the request format. If a run produces
zero results from one source when you'd expect matches, check the "errors"
section emailed/logged for that run before assuming there's simply nothing
new - see the try/except blocks below for how failures surface.

State is kept in data/seen.json (IDs already alerted on) and data/matches.json
(full history, used by the dashboard in docs/index.html).
"""

import json
import os
import sys
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime, timezone

import requests

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
SEEN_FILE = DATA_DIR / "seen.json"
MATCHES_FILE = DATA_DIR / "matches.json"
KEYWORDS_FILE = ROOT / "keywords.json"

BDNS_SEARCH_URL = "https://www.infosubvenciones.es/bdnstrans/api/convocatorias/busqueda"
EU_SEARCH_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"

BDNS_DETAIL_URL = "https://www.infosubvenciones.es/bdnstrans/api/convocatorias"

# (connect timeout, read timeout) in seconds. A plain single number only caps
# connection time in some edge cases - a stalled response after connecting
# could hang far longer. This is a hard belt-and-suspenders cap so a single
# bad request can never stall the whole workflow (which also has its own
# job-level timeout-minutes cap in scan.yml as a second line of defense).
REQUEST_TIMEOUT = (10, 20)
BDNS_PAGES_TO_SCAN = 6  # 6 pages x 50 = most recent 300 convocatorias published across Spain
BDNS_PAGE_SIZE = 50
# Candidate field names for a BDNS convocatoria's actual application deadline.
# The detail endpoint isn't formally documented, so we try each in order and
# use whichever is present - if none are, we simply don't show a deadline.
BDNS_DEADLINE_FIELDS = ("plazoPresentacionSolicitudes", "fechaFinSolicitud", "fechaFin", "plazo")


def load_keywords():
    data = json.loads(KEYWORDS_FILE.read_text(encoding="utf-8"))
    return [k.lower() for k in data["keywords"]]


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def text_matches(text, keywords):
    text_l = (text or "").lower()
    return [kw for kw in keywords if kw in text_l]


def fetch_bdns_deadline(conv_id, vpd, errors):
    """Best-effort fetch of a BDNS call's actual application deadline.

    The search endpoint only returns a publication date. The full deadline
    lives on the per-convocatoria detail endpoint, which isn't documented,
    so this tries a few known candidate field names and gives up quietly
    (returns None) if none are present - a missing deadline just means the
    dashboard falls back to showing the publication date instead.
    """
    try:
        resp = requests.get(
            BDNS_DETAIL_URL,
            params={"numConv": conv_id, "vpd": vpd},
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        for field in BDNS_DEADLINE_FIELDS:
            val = data.get(field)
            if val:
                return val
    except requests.RequestException as e:
        errors.append(f"BDNS deadline lookup failed for {conv_id}: {e}")
    except (ValueError, AttributeError):
        pass  # unparsable response - just skip the deadline for this one
    return None


# ---------------------------------------------------------------------------
# Source 1: BDNS (Spain, all administrations)
# ---------------------------------------------------------------------------

def scan_bdns(keywords, errors):
    results = []
    try:
        for page in range(BDNS_PAGES_TO_SCAN):
            resp = requests.get(
                BDNS_SEARCH_URL,
                params={
                    "page": page,
                    "pageSize": BDNS_PAGE_SIZE,
                    "order": "fechaRecepcion",
                    "direccion": "desc",
                    "vpd": "GE",
                },
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            payload = resp.json()
            content = payload.get("content", [])
            if not content:
                break
            for item in content:
                title = item.get("descripcion", "")
                matched = text_matches(title, keywords)
                if matched:
                    conv_id = item.get("numeroConvocatoria") or item.get("id")
                    vpd = item.get("vpd", "GE")
                    results.append({
                        "source": "BDNS (Spain)",
                        "id": f"bdns-{conv_id}",
                        "title": title,
                        "organism": " / ".join(filter(None, [
                            item.get("nivel1"), item.get("nivel2"), item.get("nivel3")
                        ])),
                        "date": item.get("fechaRecepcion"),
                        "deadline": fetch_bdns_deadline(conv_id, vpd, errors),
                        "matched_keywords": matched,
                        "url": f"https://www.infosubvenciones.es/bdnstrans/GE/es/convocatoria/{conv_id}",
                    })
    except requests.RequestException as e:
        errors.append(f"BDNS scan failed: {e}")
    except (KeyError, ValueError) as e:
        errors.append(f"BDNS response format unexpected: {e}")
    return results


# ---------------------------------------------------------------------------
# Source 2: EU Funding & Tenders Portal (SEDIA)
# ---------------------------------------------------------------------------

def scan_eu(keywords, errors):
    results = []
    today = datetime.now(timezone.utc).date()

    for kw in keywords:
        try:
            resp = requests.post(
                EU_SEARCH_URL,
                params={"apiKey": "SEDIA", "text": f"***{kw}***"},
                json={
                    "query": {
                        "bool": {
                            "must": [
                                {"terms": {"type": ["1"]}},  # 1 = call topics
                            ]
                        }
                    },
                    "pageSize": 25,
                    "pageNumber": 1,
                },
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            payload = resp.json()
            hits = payload.get("results") or payload.get("result") or []
            for item in hits:
                meta = item.get("metadata", item)
                status = str(meta.get("status", meta.get("statusDescription", ""))).lower()
                if status and "closed" in status:
                    continue  # skip calls we know are already closed

                title = " ".join(meta.get("title", [meta.get("title", "")])) if isinstance(meta.get("title"), list) else str(meta.get("title", ""))
                title = title.strip()

                # The EU search API ranks results by its own relevance scoring, which
                # is loose enough to surface unrelated hits (e.g. a "tank" keyword
                # pulling in "anti-tank capabilities" or "Think Tanks"). Only keep a
                # result if the keyword genuinely appears in the title - same standard
                # BDNS is held to.
                if not text_matches(title, [kw]):
                    continue

                # Best-effort recency filter: if a deadline is present and clearly in
                # the past, skip it. Historical/archived topics often don't carry
                # "closed" in their status text, so this catches what that check misses.
                deadline_raw = meta.get("deadlineDate", "")
                if isinstance(deadline_raw, list):
                    deadline_raw = deadline_raw[0] if deadline_raw else ""
                if deadline_raw:
                    try:
                        deadline = datetime.fromisoformat(str(deadline_raw).replace("Z", "+00:00")).date()
                        if deadline < today:
                            continue
                    except ValueError:
                        pass  # unparsable date - don't let that block a real match

                topic_id = meta.get("identifier", item.get("reference", item.get("id", "")))
                if isinstance(topic_id, list):
                    topic_id = topic_id[0] if topic_id else ""
                results.append({
                    "source": "EU Funding & Tenders",
                    "id": f"eu-{topic_id}",
                    "title": title,
                    "organism": meta.get("programme", meta.get("frameworkProgramme", "EU")),
                    "date": meta.get("deadlineDate", meta.get("startDate", "")),
                    "deadline": deadline_raw or "",
                    "matched_keywords": [kw],
                    "url": f"https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{topic_id}".lower(),
                })
        except requests.RequestException as e:
            errors.append(f"EU scan failed for keyword '{kw}': {e}")
        except (KeyError, ValueError, TypeError) as e:
            errors.append(f"EU response format unexpected for keyword '{kw}': {e}")

    # de-dupe: the EU portal republishes the same call once per language, so the
    # same underlying topic shows up under many different topic_ids. Group by the
    # normalized title instead of id - that's the actual signal of "same call".
    dedup = {}
    for r in results:
        key = r["title"].lower() or r["id"]
        if key in dedup:
            dedup[key]["matched_keywords"] = sorted(set(dedup[key]["matched_keywords"] + r["matched_keywords"]))
        else:
            dedup[key] = r
    return list(dedup.values())


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

def send_email(new_items, errors):
    user = os.environ.get("EMAIL_USER")
    pw = os.environ.get("EMAIL_PASS")
    to = os.environ.get("EMAIL_TO", user)
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))

    if not (user and pw and to):
        # Email is optional - dashboard-only mode. Not an error, just skip.
        return

    dashboard_url = os.environ.get("DASHBOARD_URL", "")

    lines = [f"Funding Radar: {len(new_items)} new call(s) found this week.\n"]
    for it in new_items:
        deadline = f" - deadline {it['deadline']}" if it.get("deadline") else ""
        lines.append(f"- [{it['source']}] {it['title']}{deadline}")
        lines.append(f"  {it['url']}")
    if dashboard_url:
        lines.append(f"\nFull list: {dashboard_url}")
    if errors:
        lines.append(f"\n({len(errors)} warning(s) this run - check the dashboard for details)")
    body = "\n".join(lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Funding Radar: {len(new_items)} new call(s) this week"
    msg["From"] = user
    msg["To"] = to

    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
        server.login(user, pw)
        server.sendmail(user, [to], msg.as_string())


# ---------------------------------------------------------------------------

def main():
    DATA_DIR.mkdir(exist_ok=True)
    keywords = load_keywords()
    errors = []

    bdns_results = scan_bdns(keywords, errors)
    eu_results = scan_eu(keywords, errors)
    all_results = bdns_results + eu_results

    seen = set(load_json(SEEN_FILE, []))
    matches_history = load_json(MATCHES_FILE, [])

    new_items = [r for r in all_results if r["id"] not in seen]
    now = datetime.now(timezone.utc).isoformat()
    for it in new_items:
        it["found_at"] = now

    if new_items:
        matches_history = new_items + matches_history
        matches_history = matches_history[:500]  # keep dashboard file bounded
        save_json(MATCHES_FILE, matches_history)

    seen.update(r["id"] for r in all_results)
    save_json(SEEN_FILE, sorted(seen))

    send_email(new_items, errors)

    if new_items:
        print(f"Found {len(new_items)} new call(s). Emailed alert.")
    else:
        print("No new calls this run.")

    # Always record a run log entry for the dashboard, even on a quiet run
    run_log = load_json(DATA_DIR / "run_log.json", [])
    run_log.insert(0, {
        "timestamp": now,
        "new_matches": len(new_items),
        "total_scanned": len(all_results),
        "errors": errors,
    })
    save_json(DATA_DIR / "run_log.json", run_log[:100])

    if errors:
        print("Warnings during this run:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
