#!/usr/bin/env python3
"""
Task 2 -- validate every listing and produce a review queue.

    # Static checks only (no network, instant, catches most problems)
    python scripts/validate_listings.py --input data/opps_raw.json

    # Full validation including live URL probing
    python scripts/validate_listings.py --input data/opps_raw.json --network

    # Extract listings straight out of the site's HTML
    python scripts/validate_listings.py --from-html site/index.html --network

Outputs:
    reports/validation_report.json   machine-readable, for the pipeline
    reports/review_queue.csv         open in Sheets, one row per item to fix
    reports/review_queue.html        sortable dashboard for the site owner
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.validate import (ARCHIVE, CRITICAL, INFO, PUBLISH, QUARANTINE,  # noqa: E402
                               REVIEW,
                               WARNING, find_shared_urls, normalize_status,
                               validate_static)

GREEN, RED, YELLOW, BLUE, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[2m", "\033[1m", "\033[0m")

VERDICT_COLOUR = {PUBLISH: GREEN, ARCHIVE: BLUE, REVIEW: YELLOW, QUARANTINE: RED}


def load_from_html(path: Path) -> list[dict]:
    """Pull the hardcoded OPPS array out of the site's HTML."""
    src = path.read_text()
    match = re.search(r"const OPPS\s*=\s*(\[.*?\]);", src, re.S)
    if not match:
        raise SystemExit(f"Could not find 'const OPPS = [...]' in {path}")
    return json.loads(match.group(1))


def build_html_dashboard(results: list, shared: dict, out_path: Path) -> None:
    rows = []
    order = {QUARANTINE: 0, REVIEW: 1, ARCHIVE: 2, PUBLISH: 3}
    for r in sorted(results, key=lambda x: (order[x.verdict], -len(x.findings))):
        badge = {QUARANTINE: "q", REVIEW: "r", ARCHIVE: "a", PUBLISH: "p"}[r.verdict]
        findings_html = "".join(
            f'<li class="f-{f.severity}"><b>{f.code}</b> — {esc(f.message)}'
            + (f'<br><i>{esc(f.hint)}</i>' if f.hint else "")
            + "</li>"
            for f in r.findings
        ) or "<li class='f-ok'>No issues found</li>"
        rows.append(f"""
        <tr class="row-{badge}">
          <td><span class="badge b-{badge}">{r.verdict}</span></td>
          <td class="conf">{r.confidence}</td>
          <td>
            <div class="title">{esc(r.title)}</div>
            <div class="org">{esc(r.organization)}</div>
            <a class="url" href="{esc(r.url)}" target="_blank" rel="noopener">{esc(r.url[:90])}</a>
          </td>
          <td><ul class="findings">{findings_html}</ul></td>
        </tr>""")

    counts = Counter(r.verdict for r in results)
    shared_html = ""
    if shared:
        items = "".join(
            f"<li><code>{esc(u[:100])}</code><br><small>{esc(', '.join(t[:4]))}"
            f"{' …' if len(t) > 4 else ''}</small></li>"
            for u, t in list(shared.items())[:12]
        )
        shared_html = f"""<section class="panel warn">
          <h2>URLs shared by multiple records ({len(shared)})</h2>
          <p>Several distinct listings pointing at one URL almost always means
          that URL is a search or hub page, not a specific posting.</p>
          <ul class="shared">{items}</ul></section>"""

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Listing validation review queue</title><style>
*{{box-sizing:border-box}}
body{{font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
margin:0;background:#f6f7f9;color:#1a1d21}}
header{{background:#12263f;color:#fff;padding:28px 32px}}
header h1{{margin:0 0 6px;font-size:22px}}
header p{{margin:0;opacity:.75;font-size:14px}}
.wrap{{max-width:1240px;margin:0 auto;padding:24px 32px 60px}}
.stats{{display:flex;gap:14px;flex-wrap:wrap;margin:22px 0}}
.stat{{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:16px 22px;min-width:150px}}
.stat .n{{font-size:30px;font-weight:700;line-height:1}}
.stat .l{{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;margin-top:6px}}
.s-q .n{{color:#c0392b}} .s-r .n{{color:#b7791f}} .s-p .n{{color:#1e8449}}
.panel{{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:20px 24px;margin-bottom:22px}}
.panel.warn{{border-left:4px solid #d68910}}
.panel h2{{margin:0 0 8px;font-size:16px}}
.panel p{{margin:0 0 12px;color:#4b5563;font-size:14px}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e6ea;border-radius:10px;overflow:hidden}}
th{{background:#f0f2f5;text-align:left;padding:11px 14px;font-size:12px;
text-transform:uppercase;letter-spacing:.05em;color:#4b5563}}
td{{padding:14px;border-top:1px solid #eceff2;vertical-align:top}}
.row-q{{background:#fff8f7}} .row-r{{background:#fffdf5}}
.badge{{display:inline-block;padding:4px 10px;border-radius:20px;font-size:11px;
font-weight:700;text-transform:uppercase;letter-spacing:.04em}}
.b-q{{background:#fdeceb;color:#c0392b}} .b-r{{background:#fdf5e3;color:#b7791f}}
.b-p{{background:#e9f7ef;color:#1e8449}} .b-a{{background:#eaf0f6;color:#2c5282}}
.row-a{{background:#fafbfc}} .s-a .n{{color:#2c5282}}
.conf{{font-weight:700;font-size:17px;color:#4b5563;text-align:center;width:60px}}
.title{{font-weight:600;margin-bottom:2px}}
.org{{color:#6b7280;font-size:13px;margin-bottom:5px}}
.url{{font-size:11px;color:#2563eb;word-break:break-all;text-decoration:none}}
.findings{{margin:0;padding-left:17px}}
.findings li{{margin-bottom:7px;font-size:13px}}
.findings i{{color:#6b7280;font-size:12px}}
.f-critical b{{color:#c0392b}} .f-warning b{{color:#b7791f}} .f-info b{{color:#5b6570}}
.f-ok{{color:#1e8449;list-style:none;margin-left:-17px}}
.shared li{{margin-bottom:8px;font-size:13px}}
code{{background:#f0f2f5;padding:1px 5px;border-radius:4px;font-size:12px}}
</style></head><body>
<header><h1>Listing validation — review queue</h1>
<p>Generated {date.today().isoformat()} · {len(results)} records checked</p></header>
<div class="wrap">
<div class="stats">
  <div class="stat s-q"><div class="n">{counts.get(QUARANTINE,0)}</div><div class="l">Quarantine</div></div>
  <div class="stat s-r"><div class="n">{counts.get(REVIEW,0)}</div><div class="l">Needs review</div></div>
  <div class="stat s-a"><div class="n">{counts.get(ARCHIVE,0)}</div><div class="l">Archive</div></div>
  <div class="stat s-p"><div class="n">{counts.get(PUBLISH,0)}</div><div class="l">Ready to publish</div></div>
</div>
{shared_html}
<section class="panel"><h2>How to read this</h2>
<p><b>Quarantine</b> — a critical problem. Do not show these to students until fixed.
<b>Needs review</b> — publishable, but a person should confirm first.
<b>Ready to publish</b> — passed every automated check.</p>
<p>Confidence starts at 100 and loses 100 per critical finding, 15 per warning, 3 per note.</p></section>
<table><thead><tr><th>Verdict</th><th>Conf</th><th>Listing</th><th>Findings</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</div></body></html>"""
    out_path.write_text(html)


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate listings (Task 2)")
    ap.add_argument("--input", help="JSON array of listings")
    ap.add_argument("--from-html", help="extract listings from the site's HTML")
    ap.add_argument("--network", action="store_true", help="also probe URLs live")
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "reports"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.from_html:
        records = load_from_html(Path(args.from_html))
    elif args.input:
        payload = json.loads(Path(args.input).read_text())
        records = payload.get("opportunities", payload) if isinstance(payload, dict) else payload
    else:
        ap.error("give either --input or --from-html")

    print(f"\n{BOLD}Validating {len(records)} listings{RESET}"
          f"{'' if args.network else f' {DIM}(static only — add --network to probe URLs){RESET}'}\n")

    results = [validate_static(r) for r in records]

    if args.network:
        from pipeline.netcheck import validate_network_batch
        print(f"{DIM}Probing URLs…{RESET}")
        results = validate_network_batch(results)

    shared = find_shared_urls(records)

    # --- terminal report -----------------------------------------------------
    if not args.quiet:
        by_verdict = {QUARANTINE: [], REVIEW: [], ARCHIVE: [], PUBLISH: []}
        for r in results:
            by_verdict[r.verdict].append(r)

        for verdict in (QUARANTINE, REVIEW):
            group = by_verdict[verdict]
            if not group:
                continue
            colour = VERDICT_COLOUR[verdict]
            print(f"{colour}{BOLD}{verdict.upper()} ({len(group)}){RESET}")
            print("-" * 78)
            for r in sorted(group, key=lambda x: x.confidence):
                print(f"  {colour}[{r.confidence:3}]{RESET} {r.title[:58]}")
                print(f"        {DIM}{r.organization[:60]}{RESET}")
                for f in r.findings:
                    if f.severity == INFO:
                        continue
                    mark = "!!" if f.severity == CRITICAL else " ~"
                    print(f"        {mark} {f.message[:96]}")
                print()
            print()

    # --- summary -------------------------------------------------------------
    counts = Counter(r.verdict for r in results)
    code_counts = Counter(f.code for r in results for f in r.findings)

    print(f"{BOLD}SUMMARY{RESET}")
    print("-" * 78)
    print(f"  {RED}Quarantine{RESET}      {counts.get(QUARANTINE,0):3}   critical problems, do not publish")
    print(f"  {YELLOW}Needs review{RESET}    {counts.get(REVIEW,0):3}   human confirmation required")
    print(f"  {BLUE}Archive{RESET}         {counts.get(ARCHIVE,0):3}   sound data, but not a current opening")
    print(f"  {GREEN}Ready{RESET}           {counts.get(PUBLISH,0):3}   passed every check")
    if shared:
        print(f"  {YELLOW}Shared URLs{RESET}     {len(shared):3}   URLs used by 2+ records")
    print(f"\n{BOLD}MOST COMMON FINDINGS{RESET}")
    print("-" * 78)
    for code, n in code_counts.most_common(10):
        print(f"  {n:3}  {code}")

    # --- write outputs -------------------------------------------------------
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "validation_report.json").write_text(json.dumps({
        "generated": date.today().isoformat(),
        "network_checked": args.network,
        "counts": dict(counts),
        "finding_counts": dict(code_counts),
        "shared_urls": shared,
        "results": [r.to_dict() for r in results],
    }, indent=2))

    with (out_dir / "review_queue.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["verdict", "confidence", "organization", "title", "url",
                         "severity", "code", "problem", "suggested_fix"])
        for r in sorted(results, key=lambda x: x.confidence):
            if r.verdict == PUBLISH:
                continue
            for f in r.findings:
                if f.severity == INFO:
                    continue
                writer.writerow([r.verdict, r.confidence, r.organization, r.title,
                                 r.url, f.severity, f.code, f.message, f.hint])

    build_html_dashboard(results, shared, out_dir / "review_queue.html")

    print(f"\n{BOLD}WROTE{RESET}")
    print(f"  {out_dir / 'review_queue.html'}   {DIM}← open this one{RESET}")
    print(f"  {out_dir / 'review_queue.csv'}")
    print(f"  {out_dir / 'validation_report.json'}\n")

    return 1 if counts.get(QUARANTINE, 0) else 0


if __name__ == "__main__":
    sys.exit(main())
