#!/usr/bin/env python3
"""
NHTSA recall watcher -> static dashboard.

Each run:
  1. Downloads NHTSA's bulk recall file (refreshed daily by NHTSA).
  2. Finds campaigns we haven't seen before.
  3. Keeps the ones that say "Do not drive", mention fire, or affect more
     than 50,000 vehicles.
  4. Adds them to docs/recalls.json and rebuilds docs/index.html.
  5. (Optional) emails you a short summary linking to the dashboard.

Standard library only, Python 3.8+.

Usage:
  python recall_watch.py              # normal run
  python recall_watch.py --dry-run    # build the dashboard, but don't save state or email
"""

import csv
import io
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path

# ----------------------------- configuration -----------------------------

FLAT_FILE_URL = "https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL_POST_2010.zip"
VEHICLE_THRESHOLD = 50_000                              # flag if strictly MORE than this
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "30"))  # history to load on the first run

ROOT = Path(__file__).parent
SEEN_FILE = ROOT / "data" / "seen.json"
STORE_FILE = ROOT / "docs" / "recalls.json"
DASHBOARD_FILE = ROOT / "docs" / "index.html"

# Optional email (leave unset to skip). Use an app password, not your real one.
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", SMTP_USER)
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")

FIRE_RE = re.compile(r"\bfires?\b", re.I)
DND_RE = re.compile(r"do\s*not\s*drive|don'?t\s*drive", re.I)

# ---- Exclusions: recalls matching any of these are skipped entirely ----
# 1) Campaign numbers whose 3rd character is one of these (E = equipment recalls).
EXCLUDE_CAMPNO_THIRD_CHAR = {"E"}

# 2) Vehicle types. Matched (whole words, case-insensitive) against the make, model and
#    manufacturer names, and the first sentence of the defect description (which
#    normally just names the affected vehicles). Add or remove terms freely.
EXCLUDE_TERMS = [
    # RVs / trailers
    r"RVs?", r"recreational vehicles?", r"motor ?homes?", r"campers?", r"travel trailers?",
    r"fifth[- ]wheels?", r"trailers?",
    # motorcycles
    r"motorcycles?", r"mopeds?", r"scooters?", r"trikes?",
    # buses
    r"(?<!can )(?<!data )bus(?:es)?", r"motor ?coach(?:es)?",
    # fire apparatus
    r"fire trucks?", r"fire engines?", r"fire apparatus", r"pumpers?", r"aerial ladders?",
]
# 3) Makers that only build those vehicles (their names often don't say "bus" or "trailer").
EXCLUDE_MAKERS = [
    "Harley-Davidson", "Ducati", "Triumph", "KTM", "Kawasaki", "Yamaha", "Aprilia", "Vespa",
    "Piaggio", "Royal Enfield", "Moto Guzzi", "Indian Motorcycle",
    "Winnebago", "Thor", "Forest River", "Jayco", "Keystone", "Fleetwood", "Tiffin", "Newmar",
    "Airstream", "Coachmen", "Four Winds", "Gulf Stream", "Entegra", "Dutchmen", "Grand Design",
    "Heartland", "Cruiser RV", "Prevost", "Blue Bird", "Thomas Built", "New Flyer", "Gillig",
    "Motor Coach Industries", "Nova Bus", "Collins Bus", "Starcraft Bus",
    "Pierce", "E-One", "Sutphen", "Seagrave", "KME", "Rosenbauer", "Ferrara", "Spartan",
    "Utility Trailer", "Wabash", "Great Dane", "Stoughton", "Fontaine",
]
EXCLUDE_TERMS_RE = re.compile(r"\b(?:" + "|".join(EXCLUDE_TERMS) + r")\b", re.I)
EXCLUDE_MAKERS_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(m) for m in EXCLUDE_MAKERS) + r")(?![A-Za-z0-9])", re.I)


def is_excluded(campno, make, models, manufacturer, defect):
    """True for equipment recalls (campaign no. 3rd char E) and RV/trailer/motorcycle/bus/fire-truck recalls."""
    if campno[2:3].upper() in EXCLUDE_CAMPNO_THIRD_CHAR:
        return True
    names = " ".join([make, manufacturer] + list(models))
    if EXCLUDE_MAKERS_RE.search(names):
        return True
    first_sentence = re.split(r"(?<=[a-z0-9\)])\.\s", defect, maxsplit=1)[0][:300]
    return bool(EXCLUDE_TERMS_RE.search(names + " " + first_sentence))


# Column order of NHTSA's FLAT_RCL file (tab-delimited, no header row).
COLS = [
    "RECORD_ID", "CAMPNO", "MAKETXT", "MODELTXT", "YEARTXT", "MFGCAMPNO",
    "COMPNAME", "MFGNAME", "BGMAN", "ENDMAN", "RCLTYPECD", "POTAFF", "ODATE",
    "INFLUENCED_BY", "MFGTXT", "RCDATE", "DATEA", "RPNO", "FMVSS",
    "DESC_DEFECT", "CONEQUENCE_DEFECT", "CORRECTIVE_ACTION", "NOTES",
    "RCL_CMPT_ID", "MFR_COMP_NAME", "MFR_COMP_DESC", "MFR_COMP_PTNO",
    "DO_NOT_DRIVE", "PARK_OUTSIDE",
]


# ------------------------------- data fetch -------------------------------

def download_rows():
    req = urllib.request.Request(FLAT_FILE_URL, headers={"User-Agent": "recall-watch/2.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        text = zf.read(zf.namelist()[0]).decode("latin-1")
    for row in csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(row) < 22:
            continue
        row += [""] * (len(COLS) - len(row))
        yield dict(zip(COLS, row))


def to_int(s):
    try:
        return int(str(s).strip())
    except ValueError:
        return 0


def parse_date(s):
    try:
        return datetime.strptime(s.strip(), "%Y%m%d").date()
    except ValueError:
        return None


def group_by_campaign(rows):
    """The file has one row per make/model/year; collapse to one record per campaign."""
    camps = {}
    for r in rows:
        c = camps.setdefault(r["CAMPNO"], {
            "campno": r["CAMPNO"], "make": r["MAKETXT"], "models": set(), "years": set(),
            "component": r["COMPNAME"], "manufacturer": r["MFGNAME"], "potaff": 0,
            "received": parse_date(r["RCDATE"]), "added": parse_date(r["DATEA"]),
            "defect": r["DESC_DEFECT"], "consequence": r["CONEQUENCE_DEFECT"],
            "remedy": r["CORRECTIVE_ACTION"], "notes": r["NOTES"], "dnd_flag": False,
        })
        c["models"].add(r["MODELTXT"])
        c["years"].add(r["YEARTXT"])
        c["potaff"] = max(c["potaff"], to_int(r["POTAFF"]))   # campaign-wide total repeats per row
        if r["DO_NOT_DRIVE"].strip().upper() in ("Y", "YES", "1", "TRUE"):
            c["dnd_flag"] = True
    return camps


# -------------------------------- filtering --------------------------------

def flags_for(c):
    text = " ".join([c["defect"], c["consequence"], c["remedy"], c["notes"]])
    flags = []
    if c["dnd_flag"] or DND_RE.search(text):
        flags.append("dnd")
    if FIRE_RE.search(text):
        flags.append("fire")
    if c["potaff"] > VEHICLE_THRESHOLD:
        flags.append("big")
    return flags


def year_span(years):
    ys = sorted(y.strip() for y in years if y.strip().isdigit() and y.strip() != "9999")
    if not ys:
        return ""
    return ys[0] if ys[0] == ys[-1] else f"{ys[0]}\u2013{ys[-1]}"


# ---- Injury / crash / fatality / fire / property-damage counts in the recall text ----
NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}
NUM = r"(\d{1,3}(?:,\d{3})+|\d+|" + "|".join(NUM_WORDS) + r")"
CATEGORIES = {
    "injuries": re.compile(r"injur", re.I),
    "crashes": re.compile(r"crash|collision|accident", re.I),
    "fatalities": re.compile(r"fatalit|death|died|killed", re.I),
    "fires": re.compile(r"\bfires?\b", re.I),
    "property_damage": re.compile(r"property", re.I),
}
LABELS = {  # (singular, plural) used in the email summary
    "injuries": ("injury", "injuries"), "crashes": ("crash", "crashes"),
    "fatalities": ("fatality", "fatalities"), "fires": ("fire", "fires"),
    "property_damage": ("property damage report", "property damage reports"),
}
# Words that mean the number is NOT a count of incidents ("85,000 vehicles may catch fire").
STOP_WORDS = (r"vehicles|units|trucks|cars|may|might|could|can|will|would|should|must|to|that|which|"
              r"who|if|when|but|however")
COUNT_RE = re.compile(
    r"\b" + NUM + r"\s+(?:(?!(?:" + STOP_WORDS + r")\b)[A-Za-z-]+\s+){0,3}?"
    r"(injur\w*|crash\w*|collision\w*|accident\w*|fatalit\w*|death\w*|fires?\b|"
    r"property\s+damage|damage\s+to\s+property)", re.I)
NEGATION_RE = re.compile(r"\b(?:no|zero|none|not aware of any|not aware of)\b", re.I)
REPORT_CONTEXT_RE = re.compile(r"aware|report|claim|incident|received", re.I)
SPAN_END_RE = re.compile(r"\b(?:but|however|although|though|that|which|may|might|could|can|will|would|if)\b", re.I)


def _to_number(tok):
    tok = tok.lower().replace(",", "")
    return NUM_WORDS[tok] if tok in NUM_WORDS else int(tok)


def extract_incidents(text):
    """Return counts for whatever the text states, e.g. {'injuries': 3, 'fires': 12}.
    Categories the text doesn't mention are left out. 'No reports of injuries' gives 0."""
    out = {}
    for m in COUNT_RE.finditer(text):
        raw = m.group(1)
        if re.fullmatch(r"(19|20|21)\d\d", raw):      # a year, not a count
            continue
        n = _to_number(raw)
        for cat, rx in CATEGORIES.items():
            if rx.search(m.group(2)):
                out[cat] = max(out.get(cat, 0), n)
    for sent in re.split(r"(?<=[.;])\s+", text):
        if not REPORT_CONTEXT_RE.search(sent):
            continue
        for neg in NEGATION_RE.finditer(sent):
            tail = sent[neg.end():neg.end() + 140]
            words = re.match(r"(?:\s*[A-Za-z-]+,?){1,10}", tail)
            span = SPAN_END_RE.split(words.group(0))[0] if words else ""
            if re.match(r"\s*(?:other|additional|further)\b", span, re.I):
                continue
            for cat, rx in CATEGORIES.items():
                if cat not in out and rx.search(span):
                    out[cat] = 0
    return out


def incidents_for(c):
    return extract_incidents(" ".join([c["defect"], c["consequence"], c["remedy"], c["notes"]]))


def incident_summary(inc):
    return ", ".join(f"{inc[k]:,} {LABELS[k][0] if inc[k] == 1 else LABELS[k][1]}" for k in LABELS if k in inc)


def posted_date(c, fallback):
    """Date the recall was added to NHTSA's database (best available stand-in for the date it was posted)."""
    d = c["added"] or c["received"]
    return d.isoformat() if d else fallback.isoformat()


def to_record(c, flags, first_seen):
    return {
        "campno": c["campno"],
        "make": c["make"],
        "models": sorted(m.strip() for m in c["models"] if m.strip())[:40],
        "years": year_span(c["years"]),
        "component": c["component"],
        "manufacturer": c["manufacturer"],
        "potaff": c["potaff"],
        "received": c["received"].isoformat() if c["received"] else "",
        "posted": posted_date(c, first_seen),
        "incidents": incidents_for(c),
        "first_seen": first_seen.isoformat(),
        "defect": c["defect"][:1500],
        "consequence": c["consequence"][:1500],
        "remedy": c["remedy"][:1500],
        "flags": flags,
    }


# -------------------------------- dashboard --------------------------------

def write_dashboard(store, today):
    recalls = sorted(store.values(), key=lambda r: (r.get("posted") or r["first_seen"], r["received"]), reverse=True)
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "today": today.isoformat(),
        "threshold": VEHICLE_THRESHOLD,
        "recalls": recalls,
    }
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    DASHBOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_FILE.write_text(HTML_TEMPLATE.replace("__DATA__", data), encoding="utf-8")


def send_email(new_hits, total_tracked, today):
    """Daily digest. Sent every day by default, even when nothing new was flagged.
    Set EMAIL_ONLY_IF_NEW=1 to only get an email when something new matches."""
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        print("Email not configured; skipping.")
        return
    if not new_hits and os.environ.get("EMAIL_ONLY_IF_NEW") == "1":
        print("Nothing new and EMAIL_ONLY_IF_NEW=1; skipping email.")
        return

    label = {"dnd": "DO NOT DRIVE", "fire": "FIRE", "big": f"OVER {VEHICLE_THRESHOLD:,}"}
    colour = {"dnd": "#b3261e", "fire": "#a24a05", "big": "#1d5a94"}
    hits = sorted(new_hits, key=lambda r: ("dnd" not in r["flags"], "fire" not in r["flags"], -r["potaff"]))
    shown, extra = hits[:25], max(0, len(hits) - 25)
    link = lambda r: f"https://www.nhtsa.gov/recalls?nhtsaId={r['campno']}"
    name = lambda r: f"{r['make']} {', '.join(r['models'][:3])} {r['years']}".strip()

    # Plain-text version
    if hits:
        text = [f"{len(hits)} new flagged recall(s) on {today:%B %d, %Y}:\n"]
        for r in shown:
            text.append(f"[{', '.join(label[f] for f in r['flags'])}] {name(r)} - {r['potaff']:,} vehicles\n  {link(r)}")
        if extra:
            text.append(f"...and {extra} more on the dashboard.")
    else:
        text = [f"No new recalls matched your criteria on {today:%B %d, %Y}."]
    if DASHBOARD_URL:
        text.append(f"\nDashboard ({total_tracked} tracked): {DASHBOARD_URL}")

    # HTML version
    h = ["<div style='font-family:Arial,sans-serif;max-width:640px'>",
         f"<h2 style='margin:0 0 4px'>Vehicle recall watch</h2>",
         f"<p style='color:#555;margin:0 0 16px'>{today:%A, %B %d, %Y}</p>"]
    if hits:
        h.append(f"<p><b>{len(hits)} new flagged recall(s)</b></p>")
        for r in shown:
            tags = " ".join(
                f"<span style='background:{colour[f]};color:#fff;padding:1px 7px;border-radius:4px;"
                f"font-size:12px;font-weight:bold'>{label[f]}</span>" for f in r["flags"])
            h.append(
                f"<div style='border-left:5px solid {colour[r['flags'][0]]};background:#f5f7f9;"
                f"padding:8px 12px;margin:8px 0'>{tags}"
                f"<div style='font-weight:bold;margin-top:4px'>{escape(name(r))}</div>"
                f"<div style='color:#555;font-size:14px'>{r['potaff']:,} vehicles &ndash; "
                f"{escape(r['component'])} &ndash; posted {r['posted']}</div>"
                + (f"<div style='font-size:14px'>Reported: {incident_summary(r['incidents'])}</div>"
                   if r.get('incidents') else "")
                +
                f"<div style='font-size:14px'><a href='{link(r)}'>View on NHTSA.gov</a></div></div>")
        if extra:
            h.append(f"<p>...and {extra} more on the dashboard.</p>")
    else:
        h.append("<p>No new recalls matched your criteria today.</p>")
    if DASHBOARD_URL:
        h.append(f"<p><a href='{DASHBOARD_URL}'>Open the full dashboard</a> "
                 f"({total_tracked} recalls tracked)</p>")
    h.append("</div>")

    flagged = any("dnd" in r["flags"] for r in hits)
    subject = (f"[Recall alert] {'DO NOT DRIVE: ' if flagged else ''}{len(hits)} new flagged recall(s)"
               if hits else "Vehicle recall watch: nothing new today")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, SMTP_USER, EMAIL_TO
    msg.set_content("\n\n".join(text))
    msg.add_alternative("".join(h), subtype="html")
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context()) as srv:
        srv.login(SMTP_USER, SMTP_PASS)
        srv.send_message(msg)
    print(f"Emailed {EMAIL_TO}")


# ----------------------------------- main -----------------------------------

def main():
    dry_run = "--dry-run" in sys.argv
    today = date.today()

    first_run = not SEEN_FILE.exists()
    seen = set() if first_run else set(json.loads(SEEN_FILE.read_text()))
    store = json.loads(STORE_FILE.read_text())["recalls"] if STORE_FILE.exists() else []
    store = {r["campno"]: r for r in store
             if not is_excluded(r["campno"], r["make"], r["models"], r.get("manufacturer", ""), r["defect"])}

    camps = group_by_campaign(download_rows())
    print(f"Loaded {len(camps):,} campaigns from NHTSA.")
    for no, rec in store.items():          # fill in / refresh posted dates on older entries
        if no in camps:
            rec["posted"] = posted_date(camps[no], date.fromisoformat(rec["first_seen"]))
            rec["incidents"] = incidents_for(camps[no])

    cutoff = today - timedelta(days=BACKFILL_DAYS)
    new_hits = []
    excluded = 0
    for no, c in camps.items():
        if no in seen:
            continue
        if first_run and not ((c["received"] or c["added"] or date.min) >= cutoff):
            continue
        if is_excluded(no, c["make"], c["models"], c["manufacturer"], c["defect"]):
            excluded += 1
            continue
        flags = flags_for(c)
        if flags:
            rec = to_record(c, flags, today)
            store[no] = rec
            new_hits.append(rec)
    print(f"{excluded} new recalls skipped (equipment, RV, trailer, motorcycle, bus, fire truck).")
    print(f"{len(new_hits)} new recalls matched your criteria.")

    write_dashboard(store, today)
    print(f"Dashboard written to {DASHBOARD_FILE}")

    if not dry_run:
        STORE_FILE.write_text(json.dumps(
            {"recalls": sorted(store.values(), key=lambda r: r["first_seen"], reverse=True)},
            ensure_ascii=False), encoding="utf-8")
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        SEEN_FILE.write_text(json.dumps(sorted(seen | set(camps))))
        send_email(new_hits, len(store), today)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vehicle recall watch</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #eef1f4; --surface: #ffffff; --ink: #14202b; --muted: #55626f; --line: #d3dae1;
    --dnd: #b3261e; --on-dnd: #ffffff;
    --fire: #a24a05; --on-fire: #ffffff;
    --big: #1d5a94;
    --focus: #1d5a94;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #10161b; --surface: #19212a; --ink: #e7ecf0; --muted: #98a6b2; --line: #2c3842;
      --dnd: #ff8a80; --on-dnd: #2b0a07;
      --fire: #f2ad57; --on-fire: #2a1500;
      --big: #86bbea;
      --focus: #86bbea;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 920px; margin: 0 auto; padding: 28px 16px 64px; }
  h1 { font-size: 1.75rem; line-height: 1.2; margin: 0 0 4px; letter-spacing: -0.01em; }
  .status { color: var(--muted); margin: 0 0 24px; font-size: 0.95rem; }
  .status b { color: var(--ink); font-weight: 600; }

  .filters { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 14px; }
  .chip {
    appearance: none; text-align: left; cursor: pointer; font: inherit; color: var(--ink);
    background: var(--surface); border: 2px solid var(--line); border-radius: 6px;
    padding: 10px 12px; display: block;
  }
  .chip .n { display: block; font-size: 1.9rem; font-weight: 700; line-height: 1.1; font-variant-numeric: tabular-nums; }
  .chip .t { display: block; font-size: 0.9rem; color: var(--muted); }
  .chip[aria-pressed="true"] { border-color: currentColor; }
  .chip[aria-pressed="true"] .t { color: inherit; }
  .chip.dnd { color: var(--dnd); } .chip.fire { color: var(--fire); } .chip.big { color: var(--big); }
  .chip.dnd .n, .chip.fire .n, .chip.big .n { color: inherit; }

  .tools { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }
  .tools input, .tools select {
    font: inherit; color: var(--ink); background: var(--surface);
    border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px;
  }
  .tools input { flex: 1 1 240px; }

  .day { font-size: 0.95rem; font-weight: 600; color: var(--muted); margin: 22px 0 8px; }
  .row {
    background: var(--surface); border: 1px solid var(--line); border-left-width: 6px;
    border-radius: 6px; margin-bottom: 8px; padding: 12px 14px;
  }
  .row.sev-dnd { border-left-color: var(--dnd); }
  .row.sev-fire { border-left-color: var(--fire); }
  .row.sev-big { border-left-color: var(--big); }
  .top { display: flex; gap: 16px; justify-content: space-between; align-items: flex-start; }
  .title { font-weight: 650; font-size: 1.05rem; margin: 0; }
  .meta { color: var(--muted); font-size: 0.9rem; margin: 2px 0 0; }
  .meta a { color: inherit; }
  .count { text-align: right; flex: none; }
  .count b { display: block; font-size: 1.4rem; line-height: 1.1; font-variant-numeric: tabular-nums; }
  .count span { font-size: 0.8rem; color: var(--muted); }
  .tags { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
  .tag { font-size: 0.8rem; font-weight: 600; padding: 1px 8px; border-radius: 4px; border: 1px solid transparent; }
  .tag.dnd { background: var(--dnd); color: var(--on-dnd); }
  .tag.fire { background: var(--fire); color: var(--on-fire); }
  .tag.big { border-color: var(--big); color: var(--big); }
  .tag.posted { border-color: var(--line); color: var(--muted); font-weight: 500; }
  .tag.posted.fresh { background: var(--ink); border-color: var(--ink); color: var(--surface); font-weight: 600; }
  .incidents { margin-top: 8px; border: 1px solid var(--line); border-radius: 4px; padding: 6px 10px;
    font-size: 0.9rem; display: flex; gap: 6px 20px; flex-wrap: wrap; }
  .incidents b { font-variant-numeric: tabular-nums; margin-left: 4px; }
  .incidents .zero { color: var(--muted); }
  .incidents .deadly b { color: var(--dnd); }
  .incidents.none { color: var(--muted); }
  details { margin-top: 8px; }
  summary { cursor: pointer; color: var(--muted); font-size: 0.9rem; }
  details p { margin: 8px 0 0; max-width: 70ch; font-size: 0.95rem; }
  details p b { font-weight: 600; }
  a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible {
    outline: 3px solid var(--focus); outline-offset: 2px;
  }
  .empty { background: var(--surface); border: 1px dashed var(--line); border-radius: 6px; padding: 28px; text-align: center; color: var(--muted); }
  .empty button { font: inherit; margin-top: 8px; cursor: pointer; }
  footer { margin-top: 32px; color: var(--muted); font-size: 0.85rem; }
  footer a { color: inherit; }
  @media (max-width: 560px) {
    .filters { grid-template-columns: 1fr; }
    .top { flex-direction: column; gap: 6px; }
    .count { text-align: left; }
    .count b, .count span { display: inline; }
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>Vehicle recall watch</h1>
  <p class="status" id="status"></p>

  <div class="filters" role="group" aria-label="Filter by reason">
    <button class="chip dnd" data-flag="dnd" aria-pressed="false"><span class="n" id="n-dnd">0</span><span class="t">Do not drive</span></button>
    <button class="chip fire" data-flag="fire" aria-pressed="false"><span class="n" id="n-fire">0</span><span class="t">Fire risk</span></button>
    <button class="chip big" data-flag="big" aria-pressed="false"><span class="n" id="n-big">0</span><span class="t" id="t-big">Large recalls</span></button>
  </div>

  <div class="tools">
    <input id="q" type="search" placeholder="Search make, model, or component" aria-label="Search recalls">
    <select id="sort" aria-label="Sort recalls">
      <option value="new">Newest first</option>
      <option value="count">Most vehicles</option>
      <option value="severity">Most severe</option>
    </select>
  </div>

  <div id="list"></div>

  <footer>
    Source: <a href="https://www.nhtsa.gov/recalls">NHTSA recall data</a>, checked daily.
    Raw data: <a href="recalls.json">recalls.json</a>.
    Confirm any recall on nhtsa.gov before acting on it.
  </footer>
</div>

<script>
const DATA = __DATA__;
const fmt = new Intl.NumberFormat("en-US");
const state = { flags: new Set(), q: "", sort: "new" };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function dayLabel(iso) {
  if (!iso) return "Unknown date";
  const d = new Date(iso + "T12:00:00");
  return d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" });
}
function postedOf(r) { return r.posted || r.received || r.first_seen; }
function shortDate(iso) {
  return new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}
function sev(r) { return r.flags.includes("dnd") ? 3 : r.flags.includes("fire") ? 2 : 1; }
function topFlag(r) { return r.flags.includes("dnd") ? "dnd" : r.flags.includes("fire") ? "fire" : "big"; }
const TAG_TEXT = { dnd: "Do not drive", fire: "Fire risk", big: "Over " + fmt.format(DATA.threshold) + " vehicles" };

function buildRow(r) {
  const row = el("article", "row sev-" + topFlag(r));
  const top = el("div", "top");
  const left = el("div");
  const models = r.models.length > 4 ? r.models.slice(0, 4).join(", ") + " and " + (r.models.length - 4) + " more" : r.models.join(", ");
  left.append(el("h3", "title", [r.make, models, r.years ? "(" + r.years + ")" : ""].filter(Boolean).join(" ")));
  const meta = el("p", "meta");
  const a = el("a", null, "Campaign " + r.campno);
  a.href = "https://www.nhtsa.gov/recalls?nhtsaId=" + encodeURIComponent(r.campno);
  a.target = "_blank"; a.rel = "noopener";
  meta.append(a, document.createTextNode(" \u2013 " + (r.component || "Component not listed") + (r.received ? ", reported " + r.received : "")));
  left.append(meta);
  const count = el("div", "count");
  count.append(el("b", null, fmt.format(r.potaff)), el("span", null, "vehicles affected"));
  top.append(left, count);
  row.append(top);

  const tags = el("div", "tags");
  r.flags.forEach(f => tags.append(el("span", "tag " + f, TAG_TEXT[f])));
  tags.append(el("span", "tag posted" + (r.first_seen === DATA.today ? " fresh" : ""), "Posted " + shortDate(postedOf(r))));
  row.append(tags);

  const inc = r.incidents || {};
  const labels = [["injuries", "Injuries"], ["crashes", "Crashes"], ["fatalities", "Fatalities"], ["fires", "Fires"], ["property_damage", "Property damage"]];
  const found = labels.filter(([k]) => k in inc);
  const box = el("div", "incidents" + (found.length ? "" : " none"));
  if (found.length) {
    found.forEach(([k, label]) => {
      const item = el("span", inc[k] === 0 ? "zero" : (k === "fatalities" ? "deadly" : ""));
      item.append(document.createTextNode(label), el("b", null, fmt.format(inc[k])));
      box.append(item);
    });
  } else {
    box.textContent = "No injury, crash, fatality, fire or property damage counts in the recall text";
  }
  row.append(box);

  const det = el("details");
  det.append(el("summary", null, "What\u2019s wrong and what to do"));
  [["Defect", r.defect], ["Risk", r.consequence], ["Remedy", r.remedy]].forEach(([k, v]) => {
    if (!v) return;
    const p = el("p"); p.append(el("b", null, k + ": "), document.createTextNode(v)); det.append(p);
  });
  row.append(det);
  return row;
}

function render() {
  const q = state.q.trim().toLowerCase();
  let items = DATA.recalls.filter(r =>
    (state.flags.size === 0 || r.flags.some(f => state.flags.has(f))) &&
    (!q || (r.make + " " + r.models.join(" ") + " " + r.component + " " + r.manufacturer).toLowerCase().includes(q)));
  const cmp = {
    new: (a, b) => postedOf(b).localeCompare(postedOf(a)) || b.received.localeCompare(a.received),
    count: (a, b) => b.potaff - a.potaff,
    severity: (a, b) => sev(b) - sev(a) || b.potaff - a.potaff,
  }[state.sort];
  items.sort(cmp);

  const list = document.getElementById("list");
  list.replaceChildren();
  if (!items.length) {
    const e = el("div", "empty", DATA.recalls.length ? "No recalls match these filters." : "No flagged recalls yet. Check back after the next daily run.");
    if (DATA.recalls.length) {
      const b = el("button", null, "Clear filters");
      b.onclick = () => { state.flags.clear(); state.q = ""; document.getElementById("q").value = ""; sync(); };
      e.append(document.createElement("br"), b);
    }
    list.append(e); return;
  }
  let lastDay = null;
  items.forEach(r => {
    if (state.sort === "new" && postedOf(r) !== lastDay) {
      lastDay = postedOf(r);
      const n = items.filter(x => postedOf(x) === lastDay).length;
      list.append(el("h2", "day", dayLabel(lastDay) + " \u2013 " + n + (n === 1 ? " recall" : " recalls")));
    }
    list.append(buildRow(r));
  });
}

function sync() {
  document.querySelectorAll(".chip").forEach(c => c.setAttribute("aria-pressed", state.flags.has(c.dataset.flag)));
  render();
}

(function init() {
  ["dnd", "fire", "big"].forEach(f => {
    document.getElementById("n-" + f).textContent = DATA.recalls.filter(r => r.flags.includes(f)).length;
  });
  document.getElementById("t-big").textContent = "Over " + fmt.format(DATA.threshold) + " vehicles";
  const todayN = DATA.recalls.filter(r => r.first_seen === DATA.today).length;
  const when = new Date(DATA.generated).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" });
  const s = document.getElementById("status");
  s.append("Last checked ", el("b", null, when), ". ", el("b", null, todayN + " new"), " flagged since the last check.");
  document.querySelectorAll(".chip").forEach(c => c.onclick = () => {
    const f = c.dataset.flag; state.flags.has(f) ? state.flags.delete(f) : state.flags.add(f); sync();
  });
  document.getElementById("q").oninput = e => { state.q = e.target.value; render(); };
  document.getElementById("sort").onchange = e => { state.sort = e.target.value; render(); };
  sync();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
