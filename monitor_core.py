"""Pure logic for the automation health monitor: no network, fully unit-tested.

A check gives each automation a status ("ok" or "down") plus the reasons, and
compares it with the previous run so Slack hears only about changes.
"""

from datetime import datetime, timedelta, timezone

FAILED = {"failure", "timed_out", "startup_failure"}
IGNORED = {"cancelled", "skipped", "neutral", "action_required", "stale"}
HISTORY_HOURS = 48          # cells in the status page strip
REMIND_AFTER_HOURS = 24     # repeat a "still down" alert at most once a day


def parse_time(value):
    """GitHub ISO times ('2026-10-23T06:01:12Z') and the dashboard's '2026-10-23 06:15 UTC'."""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M UTC", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def hours_since(dt, now):
    return (now - dt).total_seconds() / 3600


def ago(hours):
    if hours is None:
        return "never"
    if hours < 1:
        return f"{max(1, round(hours * 60))} min ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f} days ago"


def evaluate(monitor, workflow, runs, now, data_generated_at=None):
    """Health of one automation.

    workflow: GitHub workflow object ({"state": "active", ...}) or None if not found.
    runs: recent workflow runs, newest first (GitHub's order).
    data_generated_at: for automations that publish data, the timestamp inside it.
    """
    reasons = []
    if workflow is None:
        reasons.append("workflow file not found")
    elif workflow.get("state") != "active":
        state = workflow.get("state", "unknown")
        if state == "disabled_inactivity":
            reasons.append("schedule disabled by GitHub after 60 days without repo activity")
        else:
            reasons.append(f"workflow is {state.replace('_', ' ')}")

    completed = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") not in IGNORED]
    last = completed[0] if completed else None
    last_success = next((r for r in completed if r.get("conclusion") == "success"), None)
    success_time = parse_time(last_success["updated_at"]) if last_success else None
    success_age = hours_since(success_time, now) if success_time else None

    if last and last.get("conclusion") in FAILED:
        reasons.append(f"last run failed ({ago(hours_since(parse_time(last['updated_at']), now))})")
    if success_age is None:
        reasons.append("no successful run found")
    elif success_age > monitor["max_age_hours"]:
        reasons.append(f"no successful run for {ago(success_age).replace(' ago', '')}"
                       f" (expected within {monitor['max_age_hours']}h)")

    data_age = None
    if monitor.get("data_url"):
        generated = parse_time(data_generated_at) if data_generated_at else None
        if generated is None:
            reasons.append("published data could not be read")
        else:
            data_age = hours_since(generated, now)
            if data_age > monitor.get("data_max_age_hours", monitor["max_age_hours"]):
                reasons.append(f"published data is {ago(data_age).replace(' ago', '')} old")

    rate_runs = completed[:50]
    success_rate = (sum(r.get("conclusion") == "success" for r in rate_runs) / len(rate_runs)
                    if rate_runs else None)
    return {
        "name": monitor["name"],
        "repo": monitor["repo"],
        "workflow": monitor["workflow"],
        "schedule": monitor.get("schedule", ""),
        "status": "down" if reasons else "ok",
        "reasons": reasons,
        "last_success": success_time.strftime("%Y-%m-%dT%H:%M:%SZ") if success_time else None,
        "last_success_ago": ago(success_age),
        "data_age_hours": round(data_age, 1) if data_age is not None else None,
        "success_rate": round(success_rate, 3) if success_rate is not None else None,
        "runs_counted": len(rate_runs),
        "disabled": workflow is not None and workflow.get("state") != "active",
    }


def alerts_for(results, previous, now):
    """Slack messages for changes since the last check, plus a daily reminder while down.

    previous: {name: {"status": ..., "since": iso, "last_alert": iso}} from the last run.
    Returns (messages, new_state).
    """
    messages, state = [], {}
    iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in results:
        before = previous.get(r["name"], {})
        was = before.get("status")
        entry = {"status": r["status"], "since": before.get("since", iso), "last_alert": before.get("last_alert")}
        if r["status"] != was:
            entry["since"] = iso
        if r["status"] == "down":
            last_alert = parse_time(before.get("last_alert"))
            due = was != "down" or not last_alert or hours_since(last_alert, now) >= REMIND_AFTER_HOURS
            if due:
                prefix = "still down" if was == "down" else "DOWN"
                messages.append(f":red_circle: *{r['name']}* {prefix}: " + "; ".join(r["reasons"])
                                + f"\n<https://github.com/{r['repo']}/actions/workflows/{r['workflow']}|Open runs>")
                entry["last_alert"] = iso
        elif was == "down":
            down_for = hours_since(parse_time(before["since"]), now) if before.get("since") else None
            messages.append(f":large_green_circle: *{r['name']}* recovered"
                            + (f" after {ago(down_for).replace(' ago', '')}" if down_for else "")
                            + f". Last success {r['last_success_ago']}.")
            entry["last_alert"] = None
        state[r["name"]] = entry
    return messages, state


def digest_text(results):
    healthy = [r for r in results if r["status"] == "ok"]
    head = (f":white_check_mark: All {len(results)} automations healthy."
            if len(healthy) == len(results)
            else f":warning: {len(results) - len(healthy)} of {len(results)} automations need attention.")
    lines = [head]
    for r in results:
        icon = ":large_green_circle:" if r["status"] == "ok" else ":red_circle:"
        rate = f", {r['success_rate']:.0%} of last {r['runs_counted']} runs succeeded" if r["success_rate"] is not None else ""
        lines.append(f"{icon} {r['name']}: last success {r['last_success_ago']}{rate}")
    return "\n".join(lines)


def add_history(history, results, now):
    """Append this hour's status per automation and keep the last HISTORY_HOURS entries."""
    hour = now.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00Z")
    cutoff = now - timedelta(hours=HISTORY_HOURS)
    out = {}
    for r in results:
        entries = [e for e in history.get(r["name"], []) if e["hour"] != hour
                   and (parse_time(e["hour"].replace("Z", ":00Z")) or now) > cutoff]
        entries.append({"hour": hour, "status": r["status"]})
        out[r["name"]] = entries[-HISTORY_HOURS:]
    return out
