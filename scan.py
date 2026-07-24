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

REQUEST_TIMEOUT = 30
BDNS_PAGES_TO_SCAN = 6  # 6 pages x 50 = most recent 300 convocatorias published across Spain
BDNS_PAGE_SIZE = 50


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
                    results.append({
                        "source": "BDNS (Spain)",
                        "id": f"bdns-{conv_id}",
                        "title": title,
                        "organism": " / ".join(filter(None, [
                            item.get("nivel1"), item.get("nivel2"), item.get("nivel3")
                        ])),
                        "date": item.get("fechaRecepcion"),
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
                topic_id = meta.get("identifier", item.get("reference", item.get("id", "")))
                if isinstance(topic_id, list):
                    topic_id = topic_id[0] if topic_id else ""
                results.append({
                    "source": "EU Funding & Tenders",
                    "id": f"eu-{topic_id}",
                    "title": title,
                    "organism": meta.get("programme", meta.get("frameworkProgramme", "EU")),
                    "date": meta.get("deadlineDate", meta.get("startDate", "")),
                    "matched_keywords": [kw],
                    "url": f"https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{topic_id}".lower(),
                })
        except requests.RequestException as e:
            errors.append(f"EU scan failed for keyword '{kw}': {e}")
        except (KeyError, ValueError, TypeError) as e:
            errors.append(f"EU response format unexpected for keyword '{kw}': {e}")

    # de-dupe (same topic can match multiple keywords)
    dedup = {}
    for r in results:
        if r["id"] in dedup:
            dedup[r["id"]]["matched_keywords"] = list(set(dedup[r["id"]]["matched_keywords"] + r["matched_keywords"]))
        else:
            dedup[r["id"]] = r
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

    lines = [f"Funding Radar found {len(new_items)} new call(s):\n"]
    for it in new_items:
        lines.append(f"- [{it['source']}] {it['title']}")
        lines.append(f"  {it['organism']}")
        lines.append(f"  Matched: {', '.join(it['matched_keywords'])}")
        lines.append(f"  {it['url']}\n")
    if errors:
        lines.append("\n--- Warnings from this run ---")
        lines.extend(errors)
    body = "\n".join(lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Funding Radar: {len(new_items)} new call(s)"
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
