#!/usr/bin/env python3
"""
AMADE Proposal Radar
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
from datetime import datetime, timezone, timedelta

import requests

ROOT = Path(__file__).parent
# Data lives inside docs/ (not at the repo root) because GitHub Pages only
# publishes the /docs folder - if the data files were outside it, the
# dashboard's fetch() calls would silently 404 even though the files exist
# in the repo.
DATA_DIR = ROOT / "docs" / "data"
SEEN_FILE = DATA_DIR / "seen.json"
MATCHES_FILE = DATA_DIR / "matches.json"
KEYWORDS_FILE = ROOT / "keywords.json"

BDNS_SEARCH_URL = "https://www.infosubvenciones.es/bdnstrans/api/convocatorias/busqueda"
EU_SEARCH_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
EU_DOCUMENT_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/document"

BDNS_DETAIL_URL = "https://www.infosubvenciones.es/bdnstrans/api/convocatorias"

# (connect timeout, read timeout) in seconds. A plain single number only caps
# connection time in some edge cases - a stalled response after connecting
# could hang far longer. This is a hard belt-and-suspenders cap so a single
# bad request can never stall the whole workflow (which also has its own
# job-level timeout-minutes cap in scan.yml as a second line of defense).
REQUEST_TIMEOUT = (10, 20)
BDNS_PAGE_SIZE = 50
# Candidate field names for a BDNS convocatoria's actual application deadline.
# The detail endpoint isn't formally documented, so we try each in order and
# use whichever is present - if none are, we simply don't show a deadline.
BDNS_DEADLINE_FIELDS = ("plazoPresentacionSolicitudes", "fechaFinSolicitud", "fechaFin", "plazo")
BDNS_OPEN_FIELDS = ("fechaInicioSolicitud", "fechaInicio")


def load_keywords():
    data = json.loads(KEYWORDS_FILE.read_text(encoding="utf-8"))
    return [k.lower() for k in data["keywords"]]


def load_standing_calls():
    data = json.loads(KEYWORDS_FILE.read_text(encoding="utf-8"))
    return [k.lower() for k in data.get("standing_calls", [])]


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


def _flatten_text(obj, out):
    """Collect every string value in an arbitrary JSON structure into a flat
    list, so we can search a whole document response without needing to know
    its exact field names in advance (the SEDIA document endpoint isn't
    formally documented, same situation as BDNS's detail endpoint)."""
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, list):
        for v in obj:
            _flatten_text(v, out)
    elif isinstance(obj, dict):
        for v in obj.values():
            _flatten_text(v, out)


def fetch_eu_description(topic_id, errors):
    """Fetch the full document record for an EU topic and return all its text
    concatenated, so a keyword can be checked against the actual call
    description/objective - not just the title. Best-effort: on any failure,
    returns "" and the caller just treats it as no additional match found.
    """
    try:
        resp = requests.get(
            f"{EU_DOCUMENT_URL}/{topic_id}en",
            params={"apiKey": "SEDIA"},
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        parts = []
        _flatten_text(data, parts)
        return " ".join(parts)
    except requests.RequestException as e:
        errors.append(f"EU description lookup failed for {topic_id}: {e}")
    except (ValueError, AttributeError):
        pass
    return ""


def fetch_bdns_deadline(conv_id, vpd, errors):
    """Best-effort fetch of a BDNS call's actual application deadline.

    The search endpoint only returns a publication date. The full deadline
    (and opening date) live on the per-convocatoria detail endpoint, which
    isn't documented, so this tries a few known candidate field names and
    gives up quietly (returns (None, None)) if none are present - a missing
    value just means the dashboard falls back to showing the publication
    date instead, with no status label.
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
        deadline = next((data.get(f) for f in BDNS_DEADLINE_FIELDS if data.get(f)), None)
        opens = next((data.get(f) for f in BDNS_OPEN_FIELDS if data.get(f)), None)
        return opens, deadline
    except requests.RequestException as e:
        errors.append(f"BDNS deadline lookup failed for {conv_id}: {e}")
    except (ValueError, AttributeError):
        pass  # unparsable response - just skip this one
    return None, None


def compute_status(opens_raw, deadline_raw, today):
    """Turn raw opening/deadline date strings into a status label the
    dashboard can show directly, e.g. "Opens 2026-09-01" or "Open until
    2026-10-15". Returns (status_label, is_closed). Any date that fails to
    parse is treated as unknown rather than guessed at.
    """
    def parse(raw):
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
        except ValueError:
            return None

    opens = parse(opens_raw)
    deadline = parse(deadline_raw)

    if deadline and deadline < today:
        return None, True  # closed - caller should drop this result
    if opens and opens > today:
        return f"Opens {opens.isoformat()}", False
    if deadline:
        return f"Open until {deadline.isoformat()}", False
    return None, False


# ---------------------------------------------------------------------------
# Source 1: BDNS (Spain, all administrations)
# ---------------------------------------------------------------------------

def scan_bdns(standing_terms, errors):
    """BDNS only checks the standing_calls watchlist, not the technical topic
    keywords. Two reasons: BDNS titles are Spanish/Catalan/Basque/Galician,
    so English tech terms essentially never appear verbatim (a rare "hit"
    like a bare English word matching inside an unrelated Spanish/Catalan
    word - e.g. "structures" hiding inside "infraestructures" - is a false
    positive, not a real one). And more fundamentally: Spanish national/
    regional calls are typically broad standing programs open to any field,
    not narrow topic-specific calls the way EU Horizon topics are - so
    tracking known program names is the right model here, not keyword
    matching. EU keeps using the technical keyword list separately.
    """
    results = []
    today = datetime.now(timezone.utc).date()
    # Search from ~14 months back: BDNS's own filter narrows the volume
    # server-side (rather than us scrolling and filtering client-side, which
    # only ever saw the most-recent 300 records across ALL of Spain and
    # missed anything published more than a few days ago). Anything older
    # than ~14 months is essentially guaranteed closed anyway.
    fecha_desde = (today - timedelta(days=420)).strftime("%d/%m/%Y")
    detail_lookups_done = 0
    # Hard cap on per-item detail lookups (each is a second HTTP request) so
    # a broadly-matching term can't balloon into hundreds of slow requests
    # and blow past the workflow's timeout - see the incident where a run
    # hung for 19+ minutes before this cap and the (connect, read) timeout
    # tuple were added.
    max_detail_lookups = 25

    all_terms = [(kw, "standing") for kw in standing_terms]

    for term, kind in all_terms:
        try:
            resp = requests.get(
                BDNS_SEARCH_URL,
                params={
                    "descripcion": term,
                    "fechaDesde": fecha_desde,
                    "page": 0,
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
            for item in content:
                title = item.get("descripcion", "")
                # BDNS's own search is literal (not semantic/fuzzy) per its
                # documentation, but re-confirm client-side anyway - same
                # standard the EU source is held to, cheap insurance against
                # any edge case in how their search actually matches.
                if not text_matches(title, [term]):
                    continue

                conv_id = item.get("numeroConvocatoria") or item.get("id")
                vpd = item.get("vpd", "GE")

                if detail_lookups_done >= max_detail_lookups:
                    opens_raw, deadline_raw = None, None
                else:
                    opens_raw, deadline_raw = fetch_bdns_deadline(conv_id, vpd, errors)
                    detail_lookups_done += 1

                status, is_closed = compute_status(opens_raw, deadline_raw, today)
                if is_closed:
                    continue

                results.append({
                    "source": "BDNS (Spain)",
                    "id": f"bdns-{conv_id}",
                    "title": title,
                    "organism": " / ".join(filter(None, [
                        item.get("nivel1"), item.get("nivel2"), item.get("nivel3")
                    ])),
                    "date": item.get("fechaRecepcion"),
                    "deadline": deadline_raw or "",
                    "status": status,
                    # A standing call is a broad, always-relevant program (open to
                    # any technical field) rather than a topic-specific match -
                    # flagged separately so the dashboard can badge it distinctly.
                    "kind": kind,
                    "matched_keywords": [term],
                    "url": f"https://www.infosubvenciones.es/bdnstrans/GE/es/convocatoria/{conv_id}",
                })
        except requests.RequestException as e:
            errors.append(f"BDNS scan failed for term '{term}': {e}")
        except (KeyError, ValueError, TypeError) as e:
            errors.append(f"BDNS response format unexpected for term '{term}': {e}")

    # de-dupe: the same convocatoria can match more than one search term
    dedup = {}
    for r in results:
        if r["id"] in dedup:
            dedup[r["id"]]["matched_keywords"] = sorted(set(dedup[r["id"]]["matched_keywords"] + r["matched_keywords"]))
        else:
            dedup[r["id"]] = r
    return list(dedup.values())


# ---------------------------------------------------------------------------
# Source 2: EU Funding & Tenders Portal (SEDIA)
# ---------------------------------------------------------------------------

def scan_eu(keywords, errors):
    results = []
    today = datetime.now(timezone.utc).date()
    detail_lookups_done = 0
    # Same protective cap used for BDNS detail lookups - fetching a full
    # document is a second HTTP request per candidate, so bound it to avoid
    # ballooning into a slow run if many candidates fail the title check.
    max_detail_lookups = 25

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
                status_text = str(meta.get("status", meta.get("statusDescription", ""))).lower()
                if status_text and "closed" in status_text:
                    continue  # skip calls we know are already closed

                title = " ".join(meta.get("title", [meta.get("title", "")])) if isinstance(meta.get("title"), list) else str(meta.get("title", ""))
                title = title.strip()

                topic_id = meta.get("identifier", item.get("reference", item.get("id", "")))
                if isinstance(topic_id, list):
                    topic_id = topic_id[0] if topic_id else ""

                # The EU search API ranks results by its own relevance scoring, which
                # is loose enough to surface unrelated hits (e.g. a "tank" keyword
                # pulling in "anti-tank capabilities" or "Think Tanks"). Require the
                # keyword to genuinely appear in the title first (cheap, no extra
                # request); if it doesn't, fall back to checking the call's full
                # description/objective text (one extra request, bounded by the cap
                # above) before giving up - titles are often coded/generic while the
                # actual technical relevance lives in the body text.
                matched_via = "title"
                if not text_matches(title, [kw]):
                    if topic_id and detail_lookups_done < max_detail_lookups:
                        detail_lookups_done += 1
                        full_text = fetch_eu_description(topic_id, errors)
                        if not text_matches(full_text, [kw]):
                            continue
                        matched_via = "description"
                    else:
                        continue

                # Best-effort recency filter: if a deadline is present and clearly in
                # the past, skip it. Historical/archived topics often don't carry
                # "closed" in their status text, so this catches what that check misses.
                deadline_raw = meta.get("deadlineDate", "")
                if isinstance(deadline_raw, list):
                    deadline_raw = deadline_raw[0] if deadline_raw else ""
                start_raw = meta.get("startDate", "")
                if isinstance(start_raw, list):
                    start_raw = start_raw[0] if start_raw else ""

                if deadline_raw:
                    try:
                        deadline = datetime.fromisoformat(str(deadline_raw).replace("Z", "+00:00")).date()
                        if deadline < today:
                            continue
                    except ValueError:
                        pass  # unparsable date - don't let that block a real match
                elif start_raw:
                    # No deadline field at all is itself a signal: older/archived
                    # topics (pre-2021 FCH JU etc.) often only carry a startDate in
                    # the search index. Give a generous grace window in case it's a
                    # genuinely long-running call, but a startDate more than ~18
                    # months old with no deadline info is almost certainly closed.
                    try:
                        start = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00")).date()
                        if start < today - timedelta(days=548):
                            continue
                    except ValueError:
                        pass

                call_status, _ = compute_status(start_raw, deadline_raw, today)
                results.append({
                    "source": "EU Funding & Tenders",
                    "id": f"eu-{topic_id}",
                    "title": title,
                    "organism": meta.get("programme", meta.get("frameworkProgramme", "EU")),
                    "date": deadline_raw or start_raw,
                    "deadline": deadline_raw or "",
                    "status": call_status,
                    "matched_via": matched_via,
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

    lines = [f"AMADE Proposal Radar: {len(new_items)} new call(s) found.\n"]
    for it in new_items:
        detail = f" - {it['status']}" if it.get("status") else (f" - deadline {it['deadline']}" if it.get("deadline") else "")
        lines.append(f"- [{it['source']}] {it['title']}{detail}")
        lines.append(f"  {it['url']}")
    if dashboard_url:
        lines.append(f"\nFull list: {dashboard_url}")
    if errors:
        lines.append(f"\n({len(errors)} warning(s) this run - check the dashboard for details)")
    body = "\n".join(lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"AMADE Proposal Radar: {len(new_items)} new call(s)"
    msg["From"] = user
    msg["To"] = to

    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
        server.login(user, pw)
        server.sendmail(user, [to], msg.as_string())


# ---------------------------------------------------------------------------

def main():
    DATA_DIR.mkdir(exist_ok=True)
    keywords = load_keywords()
    standing_terms = load_standing_calls()
    errors = []

    bdns_results = scan_bdns(standing_terms, errors)
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
