"""Automation health monitor.

Every hour: checks each automation in monitors.json on GitHub (workflow enabled,
last run, last success, published data age), alerts Slack on changes, sends a
daily digest, and writes a status page to site/.

  python monitor.py              # normal run
  python monitor.py --digest     # also send the daily digest now
  python monitor.py --dry-run    # print what would be sent, post nothing
"""

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import monitor_core as core

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "state.json"
SITE = ROOT / "site"
API = "https://api.github.com"


def http_json(url, token=None, method="GET", payload=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "automation-monitor",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        return json.loads(body) if body else {}


def fetch_github(monitor, token):
    base = f"{API}/repos/{monitor['repo']}/actions/workflows/{monitor['workflow']}"
    try:
        workflow = http_json(base, token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, []
        raise
    runs = http_json(f"{base}/runs?per_page=60&exclude_pull_requests=true", token).get("workflow_runs", [])
    return workflow, runs


def fetch_generated_at(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "automation-monitor", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()).get("generated_at")
    except Exception as e:  # unreadable data is itself a finding, reported by evaluate()
        print(f"  could not read {url}: {e}")
        return None


def try_reenable(monitor, token):
    """With a token that has Actions write access, switch a disabled schedule back on."""
    url = f"{API}/repos/{monitor['repo']}/actions/workflows/{monitor['workflow']}/enable"
    try:
        http_json(url, token, method="PUT")
        return True
    except urllib.error.HTTPError as e:
        print(f"  re-enable failed for {monitor['name']}: HTTP {e.code}")
        return False


def slack(text, dry_run):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    print("SLACK:", text.replace("\n", " | "))
    if dry_run or not url:
        return
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"alerts": {}, "history": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def render_page(results, history, now):
    e = html.escape
    rows = []
    for r in results:
        ok = r["status"] == "ok"
        hist = history.get(r["name"], [])[-core.HISTORY_HOURS:]
        cells = '<i title="no data yet"></i>' * (core.HISTORY_HOURS - len(hist)) + "".join(
            f'<i class="{h["status"]}" title="{e(h["hour"])}: {e(h["status"])}"></i>' for h in hist)
        rate = f'{r["success_rate"]:.0%}' if r["success_rate"] is not None else "–"
        reasons = "".join(f"<li>{e(x)}</li>" for x in r["reasons"])
        rows.append(f"""
  <section class="card">
    <div class="top">
      <div><h2>{e(r["name"])}</h2><div class="sub">{e(r["schedule"])}</div></div>
      <span class="pill {'ok' if ok else 'down'}">{'Healthy' if ok else 'Down'}</span>
    </div>
    {'<ul class="why">' + reasons + '</ul>' if reasons else ''}
    <div class="stats">
      <div><span>Last success</span><b>{e(r["last_success_ago"])}</b></div>
      <div><span>Success rate</span><b>{rate}</b><em>last {r["runs_counted"]} runs</em></div>
      <div><span>Source</span><a href="https://github.com/{e(r["repo"])}/actions/workflows/{e(r["workflow"])}">{e(r["repo"].split("/")[1])}</a></div>
    </div>
    <div class="strip" aria-label="Hourly status, last 48 hours">{cells}</div>
    <div class="axis"><span>48h ago</span><span>now</span></div>
  </section>""")
    healthy = sum(r["status"] == "ok" for r in results)
    headline = ("All systems healthy" if healthy == len(results)
                else f"{len(results) - healthy} of {len(results)} need attention")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Automation Status</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
:root{{--bg:#0E141B;--surface:#141C25;--line:#253141;--ink:#F3F1EC;--ink-2:#B9BDC3;--ink-3:#7E8792;
  --accent:#F27A2E;--ok:#199e70;--down:#e5484d;--none:#253141;
  --sans:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;--mono:"JetBrains Mono",ui-monospace,Consolas,monospace}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:860px;margin:0 auto;padding:32px 16px 48px}}
.eyebrow{{font-family:var(--mono);font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)}}
h1{{font-size:28px;font-weight:600;letter-spacing:-.02em;margin:4px 0 2px}}
.head{{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:24px}}
.meta{{font-size:13px;color:var(--ink-3)}} .meta b{{color:var(--ink-2);font-weight:500}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;background:var(--ok)}}
.dot.down{{background:var(--down)}}
.card{{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:18px;margin-bottom:14px}}
.top{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}}
h2{{font-size:16px;font-weight:600;margin:0}} .sub{{font-size:12px;color:var(--ink-3)}}
.pill{{font-size:12px;font-weight:500;padding:3px 10px;border-radius:999px;border:1px solid}}
.pill.ok{{color:#5fd3a8;border-color:#1d5c46;background:#11281f}}
.pill.down{{color:#ff8b8e;border-color:#6b2427;background:#2a1314}}
.why{{margin:10px 0 0;padding-left:18px;color:#ffb3b5;font-size:13px}}
.stats{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:14px 0}}
.stats span{{display:block;font-size:12px;color:var(--ink-3)}}
.stats b{{font-weight:600;font-variant-numeric:tabular-nums}} .stats em{{font-style:normal;font-size:12px;color:var(--ink-3);margin-left:6px}}
.stats a{{color:var(--ink-2)}}
.strip{{display:grid;grid-template-columns:repeat(48,1fr);gap:2px;height:26px}}
.strip i{{border-radius:2px;background:var(--none)}} .strip i.ok{{background:var(--ok)}} .strip i.down{{background:var(--down)}}
.axis{{display:flex;justify-content:space-between;font-size:11px;color:var(--ink-3);margin-top:4px}}
footer{{font-size:12px;color:var(--ink-3);margin-top:20px}} footer a{{color:var(--ink-2)}}
@media (max-width:560px){{.stats{{grid-template-columns:1fr 1fr}}}}
</style></head><body><div class="wrap">
<div class="head">
  <div><div class="eyebrow">Automation health monitor</div><h1><span class="dot {'down' if healthy < len(results) else ''}"></span>{headline}</h1></div>
  <div class="meta">Checked <b>{now.strftime("%Y-%m-%d %H:%M")} UTC</b><br>Every hour</div>
</div>
{''.join(rows)}
<footer>Each automation is checked hourly: workflow still enabled, last run passed, a recent successful run, and fresh published data.
Slack is alerted on every change. Built with Python and GitHub Actions. <a href="https://github.com/riadmrad1992-blip/shopify-automation-monitor">Source on GitHub</a></footer>
</div></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", action="store_true", help="send the daily digest regardless of the hour")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of posting them")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    token = os.environ.get("GITHUB_TOKEN")
    fix_token = os.environ.get("MONITOR_TOKEN")
    monitors = json.loads((ROOT / "monitors.json").read_text(encoding="utf-8"))
    state = load_state()

    results, check_errors = [], []
    for m in monitors:
        try:
            workflow, runs = fetch_github(m, token)
        except Exception as e:
            check_errors.append(f"{m['name']}: {e}")
            continue
        generated = fetch_generated_at(m["data_url"]) if m.get("data_url") else None
        r = core.evaluate(m, workflow, runs, now, generated)
        if r["disabled"] and fix_token and try_reenable(m, fix_token):
            r["reasons"].append("schedule re-enabled automatically")
        results.append(r)
        print(f"- {r['name']}: {r['status']}" + (f" ({'; '.join(r['reasons'])})" if r["reasons"] else ""))

    messages, state["alerts"] = core.alerts_for(results, state.get("alerts", {}), now)
    state["history"] = core.add_history(state.get("history", {}), results, now)
    for msg in messages:
        slack(msg, args.dry_run)

    digest_hour = int(os.environ.get("DIGEST_HOUR_UTC", "7"))
    if args.digest or now.hour == digest_hour:
        slack(core.digest_text(results), args.dry_run)

    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(render_page(results, state["history"], now), encoding="utf-8")
    (SITE / "status.json").write_text(json.dumps(
        {"checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "automations": results}, indent=1), encoding="utf-8")
    save_state(state)

    if check_errors:
        slack(":warning: *Automation monitor* could not check: " + "; ".join(check_errors), args.dry_run)
        sys.exit(1)


if __name__ == "__main__":
    main()
