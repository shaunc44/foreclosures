#!/usr/bin/env python3
"""
foreclosure_watch.py - weekly digest of NJ sheriff's foreclosure sales.

Pulls the county list from CivilView (salesweb.civilview.com), the free site
the Bergen County Sheriff uses to publish sale notices, compares it to the
last run, and emails/prints what's NEW, what CHANGED (sale date / status),
and what DROPPED OFF (sold, cancelled, or settled).

Usage:
    python foreclosure_watch.py                  # run with config.json
    python foreclosure_watch.py --no-email       # print only
    python foreclosure_watch.py --details        # also pull judgment amount + status per new/changed property
    python foreclosure_watch.py --html-file page.html   # parse a saved copy of the list page
    python foreclosure_watch.py --reset          # forget history; everything shows as new

Requires: pip install requests beautifulsoup4
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://salesweb.civilview.com"
HERE = Path(__file__).resolve().parent
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

DEFAULT_CONFIG = {
    "county_id": 7,
    "county_name": "Bergen",
    # Leave both empty to get the whole county. Matching is case-insensitive.
    "towns": [],          # e.g. ["Teaneck", "Hackensack", "Fair Lawn"]
    "zips": [],           # e.g. ["07666", "07601"]
    "fetch_details": True,
    "email": {
        "enabled": False,
        "to": "",
        "from": "",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        # Password comes from the FORECLOSURE_SMTP_PASSWORD env var, never this file.
    },
}


# --------------------------------------------------------------------------- config / state

def load_config(path: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        user = json.loads(path.read_text())
        email = {**cfg["email"], **user.get("email", {})}
        cfg.update(user)
        cfg["email"] = email
    return cfg


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"properties": {}, "last_run": None}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


# --------------------------------------------------------------------------- scraping

def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def norm_header(h: str) -> str:
    h = clean(h).lower().rstrip(":")
    if "sheriff" in h:
        return "sheriff_no"
    if "sale" in h and "date" in h:
        return "sale_date"
    if "plaintiff" in h:
        return "plaintiff"
    if "defendant" in h:
        return "defendant"
    if "address" in h:
        return "address"
    if "upset" in h:
        return "upset"
    if "judgment" in h:
        return "judgment"
    if "status" in h:
        return "status"
    if "case" in h or "docket" in h:
        return "case_no"
    return re.sub(r"[^a-z0-9]+", "_", h).strip("_")


def fetch(session: requests.Session, url: str, retries: int = 3) -> str:
    last = None
    for attempt in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:  # pragma: no cover - network
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}: {last}")


def parse_list(page_html: str) -> list[dict]:
    """Parse the CivilView SalesSearch results table into dicts, keyed by header name."""
    soup = BeautifulSoup(page_html, "html.parser")
    table = None
    for t in soup.find_all("table"):
        heads = " ".join(clean(th.get_text()) for th in t.find_all("th")).lower()
        if "sheriff" in heads and "address" in heads:
            table = t
            break
    if table is None:
        raise ValueError(
            "Couldn't find the sales table. The site layout may have changed, "
            "or the request was blocked (try --html-file with a saved copy)."
        )

    header_row = table.find("tr")
    headers = [norm_header(th.get_text()) for th in header_row.find_all(["th", "td"])]

    rows = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        rec = {}
        for h, td in zip(headers, cells):
            if h:
                rec[h] = clean(td.get_text(" "))
        link = tr.find("a", href=re.compile(r"PropertyId=", re.I))
        if link:
            href = link["href"]
            rec["details_url"] = href if href.startswith("http") else BASE + href
            m = re.search(r"PropertyId=(\d+)", href, re.I)
            rec["property_id"] = m.group(1) if m else ""
        rec["key"] = rec.get("property_id") or rec.get("sheriff_no") or rec.get("address", "")
        if rec["key"]:
            rows.append(rec)
    return rows


def parse_details(page_html: str) -> dict:
    """Pull label/value pairs (judgment, case #, description...) and status history."""
    soup = BeautifulSoup(page_html, "html.parser")
    out: dict = {}
    history = []
    for t in soup.find_all("table"):
        ths = [clean(th.get_text()).lower() for th in t.find_all("th")]
        if ths and "status" in ths[0]:
            for tr in t.find_all("tr")[1:]:
                tds = [clean(td.get_text(" ")) for td in tr.find_all("td")]
                if len(tds) >= 2:
                    history.append({"status": tds[0], "date": tds[1]})
            continue
        for tr in t.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) == 2:
                label = clean(tds[0].get_text())
                if label.endswith(":"):
                    out[norm_header(label)] = clean(tds[1].get_text(" "))
    if history:
        def as_date(h):
            try:
                return datetime.strptime(h["date"], "%m/%d/%Y")
            except ValueError:
                return datetime.min
        history.sort(key=as_date)
        out["status"] = history[-1]["status"]
        out["status_history"] = history
    return out


# --------------------------------------------------------------------------- filtering / diffing

ZIP_RE = re.compile(r"\b(0\d{4})(?:-\d{4})?\b")


def zip_of(address: str) -> str:
    m = ZIP_RE.findall(address or "")
    return m[-1] if m else ""


def matches_area(rec: dict, towns: list[str], zips: list[str]) -> bool:
    if not towns and not zips:
        return True
    addr = (rec.get("address") or "").upper()
    if zips and zip_of(addr) in {z.strip() for z in zips}:
        return True
    return any(re.search(rf"\b{re.escape(t.strip().upper())}\b", addr) for t in towns)


WATCHED_FIELDS = ("sale_date", "status")


def diff(current: dict[str, dict], previous: dict[str, dict]):
    new, changed, gone = [], [], []
    for k, rec in current.items():
        old = previous.get(k)
        if old is None:
            new.append(rec)
            continue
        deltas = {
            f: (old.get(f, ""), rec.get(f, ""))
            for f in WATCHED_FIELDS
            if rec.get(f) and old.get(f, "") != rec.get(f, "")
        }
        if deltas:
            changed.append({**rec, "_changes": deltas})
    for k, old in previous.items():
        if k not in current:
            gone.append(old)
    key = lambda r: (sale_sort(r.get("sale_date")), r.get("address", ""))
    return sorted(new, key=key), sorted(changed, key=key), sorted(gone, key=key)


def sale_sort(d: str | None):
    try:
        return datetime.strptime(d or "", "%m/%d/%Y")
    except ValueError:
        return datetime.max


# --------------------------------------------------------------------------- output

def text_report(county, new, changed, gone, total) -> str:
    lines = [f"{county} County sheriff's sales - {datetime.now():%a %b %d, %Y}",
             f"{total} properties on the list | {len(new)} new | {len(changed)} changed | {len(gone)} dropped off", ""]

    def row(r):
        bits = [r.get("sale_date", "?"), r.get("address", "")]
        if r.get("judgment") or r.get("upset"):
            bits.append(f"judgment {r.get('judgment') or r.get('upset')}")
        if r.get("status"):
            bits.append(r["status"])
        bits.append(f"Sheriff #{r.get('sheriff_no', '?')}")
        return "  - " + " | ".join(b for b in bits if b)

    if new:
        lines.append("NEW")
        lines += [row(r) for r in new]
        lines.append("")
    if changed:
        lines.append("CHANGED")
        for r in changed:
            lines.append(row(r))
            for f, (a, b) in r["_changes"].items():
                lines.append(f"      {f.replace('_', ' ')}: {a or '-'} -> {b}")
        lines.append("")
    if gone:
        lines.append("DROPPED OFF (sold, cancelled, or settled)")
        lines += [row(r) for r in gone]
        lines.append("")
    if not (new or changed or gone):
        lines.append("No changes since last run.")
    return "\n".join(lines)


def html_report(county, new, changed, gone, total) -> str:
    e = html.escape

    def table(title, recs, show_changes=False):
        if not recs:
            return ""
        trs = []
        for r in recs:
            addr = e(r.get("address", ""))
            if r.get("details_url"):
                addr = f'<a href="{e(r["details_url"])}">{addr}</a>'
            note = ""
            if show_changes:
                note = "<br>".join(
                    f"{e(f.replace('_', ' '))}: {e(a or '-')} &rarr; <b>{e(b)}</b>"
                    for f, (a, b) in r["_changes"].items()
                )
            trs.append(
                "<tr>"
                f"<td>{e(r.get('sale_date', ''))}</td>"
                f"<td>{addr}</td>"
                f"<td>{e(r.get('judgment') or r.get('upset') or '')}</td>"
                f"<td>{e(r.get('status', ''))}{('<br><small>' + note + '</small>') if note else ''}</td>"
                f"<td>{e(r.get('plaintiff', ''))}</td>"
                f"<td>{e(r.get('sheriff_no', ''))}</td>"
                "</tr>"
            )
        return (
            f"<h3>{e(title)} ({len(recs)})</h3>"
            '<table cellpadding="6" cellspacing="0" border="1" style="border-collapse:collapse;font-size:13px">'
            "<tr style='background:#f0f0f0'><th>Sale date</th><th>Address</th><th>Judgment</th>"
            "<th>Status</th><th>Plaintiff (lender)</th><th>Sheriff #</th></tr>"
            + "".join(trs) + "</table>"
        )

    body = (
        f"<h2>{e(county)} County sheriff's sales &ndash; {datetime.now():%a %b %d, %Y}</h2>"
        f"<p>{total} on the list &middot; <b>{len(new)} new</b> &middot; {len(changed)} changed &middot; {len(gone)} dropped off</p>"
        + table("New", new)
        + table("Changed", changed, show_changes=True)
        + table("Dropped off (sold, cancelled, or settled)", gone)
    )
    if not (new or changed or gone):
        body += "<p>No changes since last run.</p>"
    body += (
        "<p style='color:#777;font-size:12px'>Source: Bergen County Sheriff via salesweb.civilview.com. "
        "Sales are often adjourned; confirm status before the sale date. Sheriff's sales are subject to "
        "prior liens and taxes &ndash; get a title search before bidding.</p>"
    )
    return f"<html><body style='font-family:-apple-system,Helvetica,Arial,sans-serif'>{body}</body></html>"


def write_csv(path: Path, recs: list[dict]) -> None:
    cols = ["sale_date", "address", "zip", "status", "judgment", "upset", "plaintiff",
            "defendant", "sheriff_no", "case_no", "details_url", "first_seen"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(recs, key=lambda r: (sale_sort(r.get("sale_date")), r.get("address", ""))):
            w.writerow({**r, "zip": zip_of(r.get("address", ""))})


def send_email(cfg: dict, subject: str, text: str, html_body: str, attachment: Path | None) -> None:
    from email.mime.application import MIMEApplication

    em = cfg["email"]
    pw = os.environ.get("FORECLOSURE_SMTP_PASSWORD")
    if not pw:
        raise RuntimeError("Set FORECLOSURE_SMTP_PASSWORD (a Gmail app password) to send email.")
    msg = MIMEMultipart("mixed")
    msg["Subject"], msg["From"], msg["To"] = subject, em["from"], em["to"]
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain"))
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)
    if attachment and attachment.exists():
        part = MIMEApplication(attachment.read_bytes(), Name=attachment.name)
        part["Content-Disposition"] = f'attachment; filename="{attachment.name}"'
        msg.attach(part)
    with smtplib.SMTP_SSL(em["smtp_host"], int(em["smtp_port"])) as s:
        s.login(em["from"], pw)
        s.sendmail(em["from"], [a.strip() for a in em["to"].split(",")], msg.as_string())


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--state", default=str(HERE / "state.json"))
    ap.add_argument("--html-file", help="parse a saved copy of the SalesSearch page instead of fetching")
    ap.add_argument("--details", action="store_true", help="fetch the detail page for new/changed properties")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--reset", action="store_true", help="ignore saved history")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    state_path = Path(args.state)
    state = {"properties": {}, "last_run": None} if args.reset else load_state(state_path)
    county = cfg.get("county_name", "Bergen")
    session = requests.Session()

    if args.html_file:
        page = Path(args.html_file).read_text(errors="replace")
    else:
        page = fetch(session, f"{BASE}/Sales/SalesSearch?countyId={int(cfg['county_id'])}")

    rows = [r for r in parse_list(page) if matches_area(r, cfg.get("towns", []), cfg.get("zips", []))]
    previous = state.get("properties", {})
    current = {}
    for r in rows:
        old = previous.get(r["key"], {})
        # Carry forward detail-page fields we fetched before.
        merged = {**{k: v for k, v in old.items() if k not in r}, **r}
        merged["first_seen"] = old.get("first_seen") or datetime.now().strftime("%Y-%m-%d")
        current[r["key"]] = merged

    new, changed, gone = diff(current, previous)

    if (args.details or cfg.get("fetch_details")) and not args.html_file:
        for r in new + changed:
            if r.get("details_url"):
                try:
                    d = parse_details(fetch(session, r["details_url"]))
                    current[r["key"]].update(d)
                    r.update(d)
                    time.sleep(1)  # be polite to the county's site
                except Exception as ex:  # keep going if one page fails
                    print(f"warn: details for {r.get('address')}: {ex}", file=sys.stderr)

    total = len(current)
    text = text_report(county, new, changed, gone, total)
    print(text)

    stamp = datetime.now().strftime("%Y-%m-%d")
    out_dir = HERE / "reports"
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"{county.lower()}_sheriff_sales_{stamp}.csv"
    write_csv(csv_path, list(current.values()))
    html_body = html_report(county, new, changed, gone, total)
    (out_dir / f"{county.lower()}_digest_{stamp}.html").write_text(html_body)

    if cfg["email"].get("enabled") and not args.no_email:
        subject = f"{county} foreclosures: {len(new)} new, {len(changed)} changed ({stamp})"
        send_email(cfg, subject, text, html_body, csv_path)
        print(f"\nEmailed {cfg['email']['to']}")

    state["properties"] = current
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
