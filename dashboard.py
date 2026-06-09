"""On-demand dashboard of every month's T-Mobile bill -> Zelle transaction.

Reads the per-bill state files the pipeline already writes
(~/.tmo_state/bill_<YYYY-MM>.json) and renders:
  * a self-contained HTML dashboard (default: <repo>/dashboard.html), and
  * a plain-text summary table to stdout.

It only reads local state files - no network, Keychain, chat.db, or browser -
so it is safe to run at any time, including after every pipeline run.

Usage:
    python dashboard.py [output_html_path]
"""
import glob
import html
import json
import os
import sys

import state as state_mod


STATUS_PAID = "PAID"
STATUS_PAID_NO_ID = "PAID (no confirmation id)"
STATUS_UNCONFIRMED = "UNCONFIRMED - verify in bank"
STATUS_ATTEMPTED = "ATTEMPTED - no confirmation"
STATUS_EMAILED = "EMAILED - awaiting Zelle"
STATUS_PARSED = "PARSED - not yet paid"
STATUS_PENDING = "PENDING"

_ATTENTION = {STATUS_PAID_NO_ID, STATUS_UNCONFIRMED, STATUS_ATTEMPTED}
_PAID = {STATUS_PAID, STATUS_PAID_NO_ID}


def classify(rec: dict) -> str:
    """Human-readable status for one month's state record."""
    if rec.get("zelle_confirmed_at"):
        return STATUS_PAID if rec.get("zelle_confirmation_id") else STATUS_PAID_NO_ID
    if rec.get("zelle_unconfirmed_at"):
        return STATUS_UNCONFIRMED
    if rec.get("zelle_attempted_at"):
        return STATUS_ATTEMPTED
    if rec.get("summary_emailed_at"):
        return STATUS_EMAILED
    if rec.get("parsed_total") is not None:
        return STATUS_PARSED
    return STATUS_PENDING


def load_transactions(state_dir=None) -> list:
    """Load every bill_<YYYY-MM>.json as a transaction record, newest first."""
    d = str(state_dir if state_dir is not None else state_mod.STATE_DIR)
    records = []
    for path in glob.glob(os.path.join(d, "bill_*.json")):
        base = os.path.basename(path)
        month = base[len("bill_"):-len(".json")]
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        data["month"] = month
        data["status"] = classify(data)
        records.append(data)
    records.sort(key=lambda r: r.get("month", ""), reverse=True)
    return records


def _money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def summarize(records: list) -> dict:
    paid_total = 0.0
    paid_count = 0
    attention = 0
    for r in records:
        st = r.get("status", "")
        if st in _PAID:
            paid_count += 1
            try:
                paid_total += float(r.get("special_amount") or 0)
            except (TypeError, ValueError):
                pass
        if st in _ATTENTION:
            attention += 1
    return {
        "months": len(records),
        "paid_count": paid_count,
        "paid_total": paid_total,
        "attention": attention,
    }


def _row_class(status: str) -> str:
    if status == STATUS_PAID:
        return "paid"
    if status in _ATTENTION:
        return "warn"
    return "pend"


def render_html(records: list, generated_at: str) -> str:
    s = summarize(records)
    body_rows = []
    for r in records:
        st = r.get("status", "")
        body_rows.append(
            '      <tr class="{cls}">'
            "<td>{month}</td>"
            '<td class="num">{total}</td>'
            '<td class="num">{special}</td>'
            "<td>{status}</td>"
            '<td class="mono">{conf}</td>'
            '<td class="mono">{paid_at}</td>'
            "</tr>".format(
                cls=_row_class(st),
                month=html.escape(r.get("month", "")),
                total=_money(r.get("parsed_total")),
                special=_money(r.get("special_amount")),
                status=html.escape(st),
                conf=html.escape(str(r.get("zelle_confirmation_id") or "—")),
                paid_at=html.escape(str(r.get("zelle_confirmed_at") or "—")),
            )
        )
    rows_html = "\n".join(body_rows) or (
        '      <tr><td colspan="6" class="empty">No transactions recorded yet.</td></tr>'
    )
    return _TEMPLATE.format(
        generated_at=html.escape(generated_at),
        months=s["months"],
        paid_count=s["paid_count"],
        paid_total=_money(s["paid_total"]),
        attention=s["attention"],
        attention_cls="warn" if s["attention"] else "ok",
        rows=rows_html,
    )


def render_text(records: list) -> str:
    header = f"{'MONTH':<9}{'BILL TOTAL':>12}{'SPECIAL':>12}  STATUS"
    lines = [header, "-" * (len(header) + 8)]
    for r in records:
        lines.append(
            f"{r.get('month', ''):<9}{_money(r.get('parsed_total')):>12}"
            f"{_money(r.get('special_amount')):>12}  {r.get('status', '')}"
        )
    s = summarize(records)
    lines.append("")
    lines.append(
        f"{s['months']} month(s) · {s['paid_count']} paid · "
        f"total paid {_money(s['paid_total'])} · {s['attention']} need attention"
    )
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo = os.path.dirname(os.path.abspath(__file__))
    out_path = argv[0] if argv else os.path.join(repo, "dashboard.html")
    records = load_transactions()
    with open(out_path, "w") as f:
        f.write(render_html(records, state_mod.now_iso()))
    print(render_text(records))
    print(f"\nDashboard written to {out_path}")
    return 0


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>T-Mobile to Zelle - Monthly Transactions</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, system-ui, Segoe UI, sans-serif; margin: 2rem; color: #1d1d1f; background: #f5f5f7; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
  .sub {{ color: #6e6e73; font-size: .85rem; margin-bottom: 1.5rem; }}
  .cards {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem; }}
  .card {{ background: #fff; border-radius: 12px; padding: 1rem 1.25rem; box-shadow: 0 1px 3px rgba(0,0,0,.08); min-width: 150px; }}
  .card .k {{ color: #6e6e73; font-size: .72rem; text-transform: uppercase; letter-spacing: .04em; }}
  .card .v {{ font-size: 1.7rem; font-weight: 600; margin-top: .25rem; }}
  .card.warn .v {{ color: #bf4800; }}
  .card.ok .v {{ color: #1d7a3e; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  th, td {{ text-align: left; padding: .6rem .8rem; border-bottom: 1px solid #ececec; font-size: .9rem; }}
  th {{ background: #fafafa; font-size: .7rem; text-transform: uppercase; letter-spacing: .04em; color: #6e6e73; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.mono {{ font-family: ui-monospace, Menlo, monospace; font-size: .8rem; color: #424245; }}
  tr.paid td:first-child {{ box-shadow: inset 3px 0 #1d7a3e; }}
  tr.warn td:first-child {{ box-shadow: inset 3px 0 #bf4800; }}
  tr.warn {{ background: #fff8f3; }}
  tr.pend td:first-child {{ box-shadow: inset 3px 0 #aeaeb2; }}
  td.empty {{ text-align: center; color: #6e6e73; padding: 2rem; }}
</style>
</head>
<body>
  <h1>T-Mobile &rarr; Zelle &middot; Monthly Transactions</h1>
  <div class="sub">Generated {generated_at} &middot; source: ~/.tmo_state/bill_*.json</div>
  <div class="cards">
    <div class="card"><div class="k">Months tracked</div><div class="v">{months}</div></div>
    <div class="card ok"><div class="k">Zelle paid</div><div class="v">{paid_count}</div></div>
    <div class="card"><div class="k">Total paid</div><div class="v">{paid_total}</div></div>
    <div class="card {attention_cls}"><div class="k">Needs attention</div><div class="v">{attention}</div></div>
  </div>
  <table>
    <thead>
      <tr><th>Month</th><th>Bill Total</th><th>Special (Zelle)</th><th>Status</th><th>Confirmation</th><th>Paid At</th></tr>
    </thead>
    <tbody>
{rows}
    </tbody>
  </table>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
