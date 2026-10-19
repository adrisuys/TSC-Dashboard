#!/usr/bin/env python3
"""Where the developers' time went over a period — one horizontal bar each:

    planifié · quickstart · interne · congé · non pointé

Every segment is time actually logged (presté), never time assigned: a
developer allocated three days a week on a planned project who logged two
shows two.

    planifié     hours logged on a project with at least one allocation slot
                 in the period (the `dev_plan_scope` rule, applied to the
                 period read)
    quickstart   hours logged on any other *client* project
    interne      hours logged on a project without a client, or on no project
    congé        hours logged on the leave projects (holidays, public holidays
                 — pre-booked there), capped at the day's calendar hours
    non pointé   calendar − everything above, never below zero

The bar is 100 % of the developer's own calendar (`dev_capacity_report`
schedules, `dev_schedules` overrides included), day by day — leave is a slice
of it, not a deduction from it, so everyone on the same calendar has the same
bar length. It counts from the developer's first timesheet line, so "depuis
toujours" does not charge a newcomer for the years before they joined, and
stops at today for a running period (today included, like the occupation).

Caveat: the calendar is today's. Someone whose working week changed long ago
is measured against the current one over the whole history.

Read-only: search_read and read_group. No figure is cached here; the web app
caches the result per period.
"""
import datetime
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_capacity_report as cap  # noqa: E402
import dev_periods as periods  # noqa: E402
import dev_plan_scope  # noqa: E402

# The periods this block offers, in the order of the picker.
PERIOD_KEYS = ["week", "last_week", "month", "last_month", "quarter",
               "last_quarter", "year", "last_year", "all"]
PERIODS = periods.choices(PERIOD_KEYS)
DEFAULT_PERIOD = "month"


def _first_dates(kw, devs):
    """{developer name: date of their first timesheet line, on any of their
    employee records}."""
    out = {}
    for _uid, eids, name in devs:
        rows = kw("account.analytic.line", "search_read",
                  [[["employee_id", "in", eids]]],
                  {"fields": ["date"], "order": "date asc", "limit": 1})
        if rows:
            out[name] = datetime.date.fromisoformat(rows[0]["date"])
    return out


def gather(kw, cfg, today, period):
    start, end = periods.period_range(period, today)
    devs, employees, _dup = cap.select_devs(kw, cfg, None)
    if not devs:
        sys.exit("Aucun développeur trouvé dans Odoo.")
    # Every employee record of each developer, lines summed under their
    # configured name (`cap.select_devs`).
    emp_ids, owner = cap.employee_ids(devs), cap.employee_owner(devs)
    schedules = cap.fetch_schedules(kw, cfg, employees)
    leave_ids = list(cfg.get("leave_project_ids") or [])
    first = _first_dates(kw, devs)
    if period == "all" and first:
        start = min(first.values())

    span = [["employee_id", "in", emp_ids],
            ["date", ">=", start.isoformat()], ["date", "<=", end.isoformat()]]
    # Every line, leave included, grouped by employee and project: leave is
    # set aside below, by project id, so no domain subtlety on `not in` and
    # empty projects can lose or double a line.
    logged = [g for g in kw("account.analytic.line", "read_group",
                            [span, ["unit_amount:sum"], ["employee_id", "project_id"]],
                            {"lazy": False})
              if not (g["project_id"] and g["project_id"][0] in leave_ids)]
    leave = defaultdict(float)     # (name, date) -> h
    if leave_ids:
        for line in kw("account.analytic.line", "search_read",
                       [span + [["project_id", "in", leave_ids]]],
                       {"fields": ["employee_id", "date", "unit_amount"]}):
            if line["employee_id"]:
                leave[(owner.get(line["employee_id"][0]),
                       datetime.date.fromisoformat(line["date"]))] += line["unit_amount"]

    planned, _dd, _roles, _n = cap.fetch_plan(kw, emp_ids, start, end, owner)
    scope = dev_plan_scope.Scope(
        {pid for (_n, _w, pid), d in planned.items() if pid and d > 0}, cfg)
    pids = {g["project_id"][0] for g in logged if g["project_id"]}
    projects = cap.fetch_project_names(kw, pids)

    split = defaultdict(lambda: defaultdict(float))   # name -> kind -> h
    for g in logged:
        if not g["employee_id"]:
            continue
        pid = g["project_id"][0] if g["project_id"] else 0
        name, client = projects.get(pid, (f"#{pid}", False))
        if pid and scope.is_planned([pid, name]):
            kind = "plan"
        else:
            kind = "quick" if (pid and client) else "internal"
        split[owner.get(g["employee_id"][0])][kind] += g["unit_amount"]
    rows, team = [], defaultdict(float)
    for _uid, _eids, name in devs:
        hours = schedules[name]["hours"]
        dev_start = max(start, first.get(name, end + datetime.timedelta(days=1)))
        # `capacity` is the full calendar; leave is a segment of it, capped at
        # the day's own hours (a half day off on a 4 h day is not 8 h off).
        capacity = off = 0.0
        day = dev_start
        while day <= end:
            gross = hours.get(day.weekday(), 0.0)
            capacity += gross
            off += min(leave[(name, day)], gross)
            day += datetime.timedelta(days=1)
        s = split[name]
        worked = s["plan"] + s["quick"] + s["internal"]
        # Not yet in Odoo during the period: no row. On leave all along: a row
        # that says so, rather than a developer silently missing.
        if dev_start > end and worked < 0.05:
            continue
        row = {"name": name, "capacity": capacity, "worked": worked,
               "plan": s["plan"], "quick": s["quick"], "internal": s["internal"],
               "leave": off,
               "unlogged": max(capacity - off - worked, 0.0),
               "over": max(worked + off - capacity, 0.0),
               "since": dev_start if dev_start > start else None}
        for k in KEYS:
            team[k] += row[k]
        rows.append(row)
    return {"period": period, "start": start, "end": end, "rows": rows,
            "team": dict(team)}


KEYS = ("capacity", "worked", "plan", "quick", "internal", "leave", "unlogged", "over")
SEGMENTS = ("plan", "quick", "internal", "leave", "unlogged")


def view(ctx, devs=None):
    """Bar widths (% of the larger of capacity and hours logged) and shares
    (% of capacity), filtered to `devs` when given."""
    rows = [dict(r) for r in ctx["rows"] if not devs or r["name"] in devs]
    team = defaultdict(float)
    for r in rows:
        for k in KEYS:
            team[k] += r[k]
    team = dict(team, name="Équipe" if len(rows) > 1 else None)

    def bars(r):
        base = max(r["capacity"], r["worked"] + r["leave"]) or 1.0
        cap_ = r["capacity"] or None
        for k in SEGMENTS:
            r[k + "_w"] = r[k] / base * 100.0
            r[k + "_pct"] = r[k] / cap_ * 100.0 if cap_ else None
        return r

    return {"rows": [bars(r) for r in rows],
            "team": bars(team) if len(rows) > 1 else None,
            "start": ctx["start"], "end": ctx["end"]}
