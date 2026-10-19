#!/usr/bin/env python3
"""Per-developer capacity from the production Odoo: plan vs actual vs backlog.

Usage:
    python3 dev_capacity_report.py                    # whole team
    python3 dev_capacity_report.py "Nathan"           # one developer
    python3 dev_capacity_report.py --weeks-back 8
    python3 dev_capacity_report.py --format md      # no page, Markdown only

`dev_workload_report.py` answers "what is on their plate" — a stock of remaining
ticket hours. The capacity plan (`project.allocation.slot`, the grid behind
/odoo/action-2407) answers "what we promised they would do" — days per project
per week. Neither answers "what they actually did", nor "out of how much". This
report joins four numbers on (developer, project, ISO week):

  capacity  resource.calendar attendance hours, minus absences
  planned   project.allocation.slot days x 8
  actual    account.analytic.line hours, client and internal apart
  backlog   dev_time minus hours logged by developers, per project

and prints five sections: occupation, adherence, forward commitment, runway per
project, and a data-quality block.

Two definitions carry most of the value:

*Capacity is per developer, and is never assumed to be 40h over 5 days.* It is
read from each employee's `resource.calendar`, overridden from
`cfg["dev_schedules"]` where production is known to be wrong, and cross-checked
against the observed timesheet pattern (see `detect_pattern`). Nathan works four
days but sits on a 40h calendar: on 40h he reads 80-85% occupied and looks like
he has room, on his real 32h he reads 101-105% and is the most loaded developer
on the team. The running week is prorated to *that* developer's elapsed working
days — on a Thursday a four-day week is already 100% elapsed and a five-day week
only 80%, so a flat proration makes every four-day developer look idle.

*Absences come from timesheets, not from hr.leave.* An ordinary account only
sees its own `hr.leave` records (a record rule), so the denominator cannot be
built on them. Absences are booked on a partner-less "Congés" project instead
(`cfg["leave_project_ids"]`), which is visible for everyone and, because public
holidays are pre-booked there, also nets future capacity for free. A week fully
on leave prints "— leave" rather than 0%, and drops out of the averages.

*The report itself is written in French*, headings, legends and notes alike —
it is read by the team, not by the code. Everything a developer reads instead of
a manager (docstrings, comments, argparse help, error text of the module itself)
stays in English, and the two never mix: no control flow tests a French string,
which is why the schedule override and the timesheet-gap flag are carried by
`overridden` and `GAP_FLAG` rather than by a substring of a label.

Read-only: search, search_read, read and read_group only.
"""
import argparse
import datetime
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import digest_config  # noqa: E402
import dev_workload_report as workload  # noqa: E402
import md_to_html  # noqa: E402  (section_tags only — the folded sections)

# A plan "day" is a man-day: the allocation grid is filled in days, timesheets in
# hours. This converts one to the other, and is NOT the same thing as a
# developer's own hours per day — Nathan's week is 4 x 8h, not 5 x 6.4h.
HOURS_PER_DAY = 8.0
WEEKDAYS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]

# Occupation bands, in percent of net capacity.
ROOM_BELOW = 70.0    # 🟢 has room
FULL_ABOVE = 95.0    # 🔴 full
# A weekday under this share of the median worked weekday is an off day. 25% and
# the median (not the max) are both deliberate: Nathan's residual Fridays are 12%
# of his best day but 14% of his median, and a single heavy Monday must not move
# the bar. Below MIN_DETECT_HOURS in the window there is not enough signal to
# judge, and a developer with a timesheet gap would be read as working one day.
OFF_DAY_RATIO = 0.25
MIN_DETECT_HOURS = 40.0

NO_PROJECT = "(sans projet)"
# Flag texts are compared by identity rather than by substring: the report is
# written in French and a check on the English word ("gap" in flag) would have
# gone quietly false the moment the label was translated.
GAP_FLAG = "⚠️ pointage incomplet"
MISMATCH_FLAG = "⚠️ régime incohérent"
POOL_FLAG = "(renfort)"
# Unplanned projects listed per developer before the rest is summed into one row.
UNPLANNED_ROWS = 10


def monday(date):
    return date - datetime.timedelta(days=date.weekday())


def week_of(date):
    return monday(date).isoformat()


def wlabel(iso_monday):
    """ISO week, French-style: s34 for semaine 34."""
    return "s%d" % datetime.date.fromisoformat(iso_monday).isocalendar()[1]


def pct(worked, capacity):
    return None if capacity <= 0.05 else worked / capacity * 100.0


def occ_badge(percent, gap=False):
    """🟢 has room · 🟡 loaded · 🔴 full · ⚠️ unmeasurable.

    A timesheet gap outranks the percentage: a developer with almost no hours
    logged and no absence to explain it is not free capacity, and a green badge
    on 1% would invite handing them a ticket on the strength of missing data."""
    if gap:
        return "⚠️"
    if percent is None:
        return "—"
    if percent < ROOM_BELOW:
        return "🟢"
    if percent < FULL_ABOVE:
        return "🟡"
    return "🔴"


def cell(text):
    return str(text).replace("|", "\\|")


def legend(title, rows):
    """Emit a collapsible symbol legend under a section's own table.

    `<details>` is passed through untouched by `md_to_html` and styled there
    (digest.py does the same for its error block), so the page opens on the
    numbers and the reader unfolds the key only when a cell puzzles them.
    Every symbol the section can print is listed: a legend that covers half the
    glyphs is worse than none, because the missing one reads as decoration."""
    print(f"<details><summary>Légende — {title}</summary>\n")
    print("| Symbole | Signification |")
    print("|---|---|")
    for symbol, meaning in rows:
        print(f"| {symbol} | {meaning} |")
    print("\n</details>\n")


# --- scope -------------------------------------------------------------------

def select_devs(kw, cfg, needle):
    """([(user_id, [employee_ids], name)], {name: primary employee},
    {name: [every employee record]} for those holding more than one) — in
    configured order, so runs are stable.

    A developer is found through their *user*, not the employee name: the same
    person can hold one `hr.employee` per company (`David Andrian` at TSC
    France and `David Andrian (BE)` at The Service Company, almost all his
    hours on the second). Matching the name kept only the first record and read
    him as idle. Every record of the user is kept and their lines summed —
    `dev_timesheet_report.resolve_devs` does the same. An employee with the
    configured name but no user link is still picked up, by name.

    The primary record — whose calendar is the developer's week — is the one
    they book the most hours on over the last year (`primary_employee`)."""
    names = cfg["dev_names"]
    if needle:
        matches = [n for n in names if needle.lower() in n.lower()]
        if not matches:
            sys.exit(f"Aucun développeur ne correspond à {needle!r}. "
                     f"Connus : {', '.join(names)}")
        if len(matches) > 1:
            sys.exit(f"{needle!r} est ambigu : {', '.join(matches)}")
        names = matches
    users = {u["name"]: u["id"] for u in
             kw("res.users", "search_read", [[["name", "in", names]]], {"fields": ["name"]})}
    found = kw("hr.employee", "search_read",
               [["|", ["user_id", "in", sorted(users.values())], ["name", "in", names]]],
               {"fields": ["name", "user_id", "resource_calendar_id", "company_id"]})
    name_of_uid = {uid: n for n, uid in users.items()}
    records = defaultdict(list)          # configured name -> [employee]
    for e in sorted(found, key=lambda e: e["id"]):
        uid = e["user_id"][0] if e["user_id"] else None
        name = name_of_uid.get(uid) or (e["name"] if e["name"] in names else None)
        if name:
            records[name].append(e)
    hours = _hours_by_employee(
        kw, [e["id"] for recs in records.values() if len(recs) > 1 for e in recs])
    out, employees, multiple = [], {}, {}
    for name in names:
        if name not in users or not records.get(name):
            continue
        recs = records[name]
        employees[name] = primary_employee(recs, name, hours)
        out.append((users[name], [e["id"] for e in recs], name))
        if len(recs) > 1:
            multiple[name] = recs
    return out, employees, multiple


def _hours_by_employee(kw, emp_ids, days=365):
    """{employee_id: hours logged over the last `days`} — only asked for the
    developers holding several records, to pick the one they work on."""
    if not emp_ids:
        return {}
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    groups = kw("account.analytic.line", "read_group",
                [[["employee_id", "in", emp_ids], ["date", ">=", since]],
                 ["unit_amount:sum"], ["employee_id"]], {"lazy": False})
    return {g["employee_id"][0]: g["unit_amount"] for g in groups if g["employee_id"]}


def primary_employee(records, name, hours):
    """The record booked on the most, then one with a calendar, then the one
    carrying the configured name, then the oldest."""
    return sorted(records, key=lambda e: (-hours.get(e["id"], 0.0),
                                          0 if e["resource_calendar_id"] else 1,
                                          0 if e["name"] == name else 1,
                                          e["id"]))[0]


def employee_ids(devs):
    """Every employee id of the selected developers, flattened."""
    return [eid for _uid, eids, _name in devs for eid in eids]


def employee_owner(devs):
    """{employee_id: configured developer name} — how a timesheet line or a
    slot booked on `David Andrian (BE)` is counted for David Andrian."""
    return {eid: name for _uid, eids, name in devs for eid in eids}


def fetch_schedules(kw, cfg, employees):
    """{name: {"hours": {weekday: h}, "source": str, "calendar": str}}.

    Only `hr.employee` public fields are read. `job_title`, `department_id` and
    anything else reaching through `version_id` raise on Odoo 19 for an ordinary
    account, and `hr.version` is refused outright."""
    cal_ids = sorted({e["resource_calendar_id"][0] for e in employees.values()
                      if e["resource_calendar_id"]})
    per_cal = defaultdict(lambda: defaultdict(float))
    if cal_ids:
        for a in kw("resource.calendar.attendance", "search_read",
                    [[["calendar_id", "in", cal_ids]]],
                    {"fields": ["calendar_id", "dayofweek", "hour_from", "hour_to"]}):
            per_cal[a["calendar_id"][0]][int(a["dayofweek"])] += a["hour_to"] - a["hour_from"]
    schedules = {}
    for name, emp in employees.items():
        cal = emp["resource_calendar_id"]
        hours = dict(per_cal.get(cal[0], {})) if cal else {}
        source = cal[1] if cal else "pas de calendrier"
        override = cfg.get("dev_schedules", {}).get(name)
        if override:
            days = sorted(int(d) for d in override.get("days", []))
            total = float(override.get("hours", len(days) * HOURS_PER_DAY))
            hours = {d: total / len(days) for d in days} if days else {}
            source = "surcharge : %s, %.0fh" % ("/".join(WEEKDAYS[d] for d in days), total)
        # An explicit flag, not a substring test on `source`: the wording is
        # French and translated text is no place to hide control flow.
        schedules[name] = {"hours": hours, "source": source,
                           "overridden": bool(override),
                           "calendar": cal[1] if cal else None,
                           "cal_days": sorted(per_cal.get(cal[0], {})) if cal else []}
    return schedules


def net(schedule, name, week, leave, leave_todate, today, current_week):
    """Capacity of one week: calendar hours minus absences. The running week is
    prorated to the developer's own elapsed working days, and only against the
    leave that has already elapsed."""
    if week == current_week:
        return max(gross(schedule, today.weekday()) - leave_todate[(name, week)], 0.0)
    return max(gross(schedule) - leave[(name, week)], 0.0)


def gross(schedule, upto_weekday=None):
    """Calendar hours of a week, or of its part elapsed through `upto_weekday`."""
    return sum(h for d, h in schedule["hours"].items()
               if upto_weekday is None or d <= upto_weekday)


def detect_pattern(weekday_hours, schedule):
    """The weekdays actually worked, when they disagree with the calendar.

    Returns None when the calendar is confirmed, or there is too little data to
    judge. This is what caught Nathan's four-day week; the point is that the
    next wrong calendar is caught without anyone remembering to look."""
    values = [weekday_hours.get(d, 0.0) for d in range(7)]
    if sum(values) < MIN_DETECT_HOURS:
        return None
    worked = sorted(v for v in values if v > 0)
    if not worked:
        return None
    median = worked[len(worked) // 2]
    observed = {d for d in range(7) if values[d] >= OFF_DAY_RATIO * median}
    expected = set(schedule["hours"])
    return observed if observed and expected and observed != expected else None


# --- data --------------------------------------------------------------------

def fetch_actuals(kw, cfg, emp_ids, date_from, date_to, detect_span, owner=None):
    """Timesheet lines bucketed by week, project, and weekday.

    Leave lines are split out rather than dropped: they are what turns a
    calendar week into a net capacity, and a week that is fully leave must read
    "— leave" rather than 0% occupation.

    The weekday bucket that feeds the schedule detector is confined to
    `detect_span`, which is longer than the report window and stops at today:
    four weeks cannot tell a four-day week from a fortnight of leave, and future
    pre-booked holidays would show up as an off day for everyone."""
    leave_ids = set(cfg.get("leave_project_ids") or [])
    detect_from, detect_to = detect_span
    lines = kw("account.analytic.line", "search_read",
               [[["employee_id", "in", emp_ids],
                 ["date", ">=", date_from.isoformat()],
                 ["date", "<=", date_to.isoformat()]]],
               {"fields": ["employee_id", "date", "unit_amount", "project_id"]})
    leave = defaultdict(float)     # (name, week) -> h
    # Leave already elapsed. The running week is prorated to the days worked so
    # far, so a Friday booked off must not shrink a Thursday's capacity — that
    # read as 0.0/24h for someone who has simply not reached their day off yet.
    leave_todate = defaultdict(float)
    actual = defaultdict(float)    # (name, week, project_id) -> h
    weekday = defaultdict(float)   # (name, weekday) -> h, leave excluded
    for line in lines:
        if not line["employee_id"]:
            continue
        name = _owner_name(line["employee_id"], owner)
        date = datetime.date.fromisoformat(line["date"])
        week = week_of(date)
        pid = line["project_id"][0] if line["project_id"] else 0
        hours = line["unit_amount"]
        if pid in leave_ids:
            leave[(name, week)] += hours
            if date <= detect_to:
                leave_todate[(name, week)] += hours
        else:
            actual[(name, week, pid)] += hours
            if detect_from <= date <= detect_to:
                weekday[(name, date.weekday())] += hours
    return leave, leave_todate, actual, weekday, len(lines)


def _owner_name(employee, owner):
    """The developer a line or slot belongs to: the configured name through
    `owner` ({employee_id: name}), else the employee record's own name."""
    return (owner or {}).get(employee[0], employee[1])


def fetch_plan(kw, emp_ids, date_from, date_to, owner=None):
    """Allocation slots bucketed by week. `state` is ignored on purpose: all 374
    slots in this database are `draft`, so filtering on it returns nothing."""
    slots = kw("project.allocation.slot", "search_read",
               [[["employee_id", "in", emp_ids],
                 ["date", ">=", date_from.isoformat()],
                 ["date", "<=", date_to.isoformat()]]],
               {"fields": ["employee_id", "project_id", "role_id", "date", "days"]})
    planned = defaultdict(float)    # (name, week, project_id) -> days
    dev_days = defaultdict(float)   # (project_id, week) -> days, dev roles only
    roles = defaultdict(set)
    for s in slots:
        if not s["employee_id"]:
            continue
        name = _owner_name(s["employee_id"], owner)
        week = week_of(datetime.date.fromisoformat(s["date"]))
        pid = s["project_id"][0] if s["project_id"] else 0
        planned[(name, week, pid)] += s["days"]
        role = s["role_id"][1] if s["role_id"] else None
        roles[(name, week, pid)].add(role)
        dev_days[(pid, week, role)] += s["days"]
    return planned, dev_days, roles, len(slots)


def fetch_backlog(kw, cfg, user_ids):
    """{project_id: hours of dev work left} — dev_workload's own definition, so
    the two reports can never disagree: dev_time minus the hours logged *by
    developers*, on tickets in the counted dev stages. Each task is counted once
    for the project even when two developers share it."""
    tag = kw("project.tags", "search", [[["name", "=", cfg["tag"]]]])
    if not tag:
        return {}, 0
    tasks = [t for t in workload.fetch_tasks(kw, tag[0], user_ids)
             if workload.stage_name(t) in workload.DEV_STAGES]
    sheets = workload.fetch_timesheets(kw, [t["id"] for t in tasks], user_ids)
    logged = defaultdict(float)
    for (_uid, task_id), hours in sheets.items():
        logged[task_id] += hours
    backlog = defaultdict(float)
    for task in tasks:
        pid = task["project_id"][0] if task["project_id"] else 0
        backlog[pid] += max(task["dev_time"] - logged[task["id"]], 0.0)
    return backlog, len(tasks)


def fetch_project_names(kw, pids):
    """{id: (name, is_client)}. `partner_id` is the internal/client
    discriminator — `allow_billable` is not: "Interne" is flagged billable."""
    pids = sorted(p for p in pids if p)
    names = {0: (NO_PROJECT, False)}
    if pids:
        for p in kw("project.project", "read", [pids], {"fields": ["name", "partner_id"]}):
            names[p["id"]] = (p["name"], bool(p["partner_id"]))
    return names


# --- gather ------------------------------------------------------------------

def gather(kw, cfg, today, weeks_back=4, weeks_fwd=4, needle=None):
    """Every figure the five sections are built from, in one dict.

    Lifted out of build() so the web app lays the same numbers out natively:
    each section is a `*_data(ctx)` function returning plain rows, printed by
    its `section_*` here and rendered by a template there — one computation,
    two layouts, no chance of the two pages disagreeing."""
    current = monday(today)
    past = [(current - datetime.timedelta(weeks=n)).isoformat()
            for n in range(weeks_back, -1, -1)]
    future = [(current + datetime.timedelta(weeks=n)).isoformat()
              for n in range(0, weeks_fwd + 1)]
    weeks = {"current": current.isoformat()}

    devs, employees, duplicated = select_devs(kw, cfg, needle)
    if not devs:
        sys.exit("Aucun développeur trouvé dans Odoo.")
    emp_ids, owner = employee_ids(devs), employee_owner(devs)
    user_ids = [d[0] for d in devs]
    schedules = fetch_schedules(kw, cfg, employees)

    # The detector needs a longer history than the report itself, so one query
    # covers whichever is wider and the sections filter it down.
    detect_from = current - datetime.timedelta(weeks=cfg["schedule_detect_weeks"])
    date_from = min(datetime.date.fromisoformat(past[0]), detect_from)
    date_to = datetime.date.fromisoformat(future[-1]) + datetime.timedelta(days=6)
    leave, leave_todate, actual, weekday, n_lines = fetch_actuals(
        kw, cfg, emp_ids, date_from, date_to, (detect_from, today), owner)
    planned, dev_days, _roles, n_slots = fetch_plan(kw, emp_ids, date_from, date_to, owner)
    backlog, n_tasks = fetch_backlog(kw, cfg, user_ids)
    projects = fetch_project_names(
        kw, {pid for (_n, _w, pid) in actual} | {pid for (_n, _w, pid) in planned}
        | set(backlog) | set(cfg["leave_project_ids"]))

    notes = []
    for name in sorted(duplicated):
        others = ", ".join(e["name"] for e in duplicated[name])
        notes.append(f"ℹ️ **{name}** — plusieurs fiches `hr.employee` ({others}) : "
                     "heures et allocations additionnées ; calendrier lu sur "
                     f"« {employees[name]['name']} », la fiche la plus pointée.")
    missing = [n for n in (cfg["dev_names"] if not needle else [])
               if n not in {d[2] for d in devs}]
    if missing:
        notes.append("⚠️ Aucun utilisateur ni employé Odoo pour "
                     + ", ".join(missing) + " — configuré dans `dev_names` mais "
                     "absent de cette base : absent, donc, de toutes les sections.")

    # Flags for section A, computed before it so the notes and the table agree.
    flags = defaultdict(list)
    for _uid, _eid, name in devs:
        sched = schedules[name]
        worked = sum(h for (n, w, _p), h in actual.items() if n == name and w in past)
        capacity = sum(net(sched, name, week, leave, leave_todate, today,
                           weeks["current"]) for week in past)
        if capacity > 0.05 and worked < capacity * cfg["gap_ratio"]:
            flags[name].append(GAP_FLAG)
        if detect_pattern({d: weekday[(name, d)] for d in range(7)}, sched):
            flags[name].append(MISMATCH_FLAG)
        if name in cfg["pool_devs"]:
            flags[name].append(POOL_FLAG)

    return {"cfg": cfg, "today": today, "past": past, "future": future,
            "weeks": weeks, "devs": devs, "employees": employees,
            "duplicated": duplicated, "schedules": schedules, "leave": leave,
            "leave_todate": leave_todate, "actual": actual, "weekday": weekday,
            "n_lines": n_lines, "planned": planned, "dev_days": dev_days,
            "n_slots": n_slots, "backlog": backlog, "n_tasks": n_tasks,
            "projects": projects, "notes": notes, "flags": flags}


def _leave_names(ctx):
    projects, cfg = ctx["projects"], ctx["cfg"]
    return (", ".join(projects[p][0] for p in cfg["leave_project_ids"]
                      if p in projects) or "le projet de congés")


# --- section data ------------------------------------------------------------

def occupation_data(ctx):
    """A — per developer: one cell per week (worked / net capacity), the
    average over complete weeks, client vs internal hours, flags."""
    past, weeks, today = ctx["past"], ctx["weeks"], ctx["today"]
    leave, leave_todate, actual = ctx["leave"], ctx["leave_todate"], ctx["actual"]
    projects, flags = ctx["projects"], ctx["flags"]
    rows, occupancy = [], {}
    for _uid, _eid, name in ctx["devs"]:
        sched = ctx["schedules"][name]
        cells, percents, partial = [], [], False
        for week in past:
            capacity = net(sched, name, week, leave, leave_todate, today,
                           weeks["current"])
            worked = sum(h for (n, w, _p), h in actual.items() if n == name and w == week)
            share = pct(worked, capacity)
            cells.append({"week": week, "label": wlabel(week),
                          "current": week == weeks["current"], "worked": worked,
                          "capacity": capacity, "share": share,
                          "leave": bool(leave[(name, week)]),
                          # Over 120% is either overtime or hours logged on a
                          # day booked off — worth seeing.
                          "over": share is not None and share > 120})
            if share is None:
                continue
            # The running week is deliberately left out of the average even
            # though it is prorated: timesheets are often filled at the end of
            # the week, so a Thursday reading drags everyone down and would
            # make a full team look like it has room.
            if week != weeks["current"]:
                percents.append(share)
            elif len(past) == 1:
                # --weeks-back 0: the running week is all there is, so it has
                # to carry the average. Marked, because a mid-week reading is
                # not a verdict.
                percents.append(share)
                partial = True
        avg = sum(percents) / len(percents) if percents else None
        occupancy[name] = avg
        client = internal = 0.0
        for (n, w, pid), hours in actual.items():
            if n != name or w not in past:
                continue
            if projects.get(pid, ("", False))[1]:
                client += hours
            else:
                internal += hours
        rows.append({"name": name, "days": len(sched["hours"]),
                     "hours": gross(sched), "cells": cells, "avg": avg,
                     "partial": partial, "client": client, "internal": internal,
                     "flags": list(flags.get(name, [])),
                     "gap": GAP_FLAG in flags.get(name, []),
                     "badge": occ_badge(avg, GAP_FLAG in flags.get(name, []))})
    return {"rows": rows, "occupancy": occupancy}


def adherence_data(ctx):
    """B — per developer: net capacity, planned vs logged per planned project,
    and the off-plan hours split client / internal."""
    past, weeks, today, cfg = ctx["past"], ctx["weeks"], ctx["today"], ctx["cfg"]
    leave, leave_todate = ctx["leave"], ctx["leave_todate"]
    actual, planned, projects = ctx["actual"], ctx["planned"], ctx["projects"]
    out = []
    for _uid, _eid, name in ctx["devs"]:
        sched = ctx["schedules"][name]
        capacity = sum(net(sched, name, week, leave, leave_todate, today,
                           weeks["current"]) for week in past)
        planned_weeks = len({w for (n, w, _p), d in planned.items()
                             if n == name and w in past and d})
        plan_h, act_h = {}, {}
        for (n, w, pid), days in planned.items():
            if n == name and w in past:
                plan_h[pid] = plan_h.get(pid, 0.0) + days * HOURS_PER_DAY
        for (n, w, pid), hours in actual.items():
            if n == name and w in past:
                act_h[pid] = act_h.get(pid, 0.0) + hours
        # A slot can exist with zero days on it — the grid keeps the cell once
        # it has been touched. Nothing planned and nothing logged is not a
        # finding, so the row is dropped.
        plan_rows = [(pid, hours) for pid, hours in plan_h.items()
                     if hours >= 0.05 or act_h.get(pid, 0.0) >= 0.05]
        planned_pids = {pid for pid, _h in plan_rows}
        unplanned = [(pid, h) for pid, h in act_h.items() if pid not in planned_pids]
        groups = []
        for is_client, title in ((True, "hors plan — client"),
                                 (False, "hors plan — interne")):
            ranked = sorted([(pid, h) for pid, h in unplanned
                             if projects.get(pid, ("", False))[1] is is_client],
                            key=lambda kv: -kv[1])
            if ranked:
                extra = ranked[UNPLANNED_ROWS:]
                groups.append({
                    "title": title, "client": is_client,
                    "rows": [{"project": projects.get(pid, (f"#{pid}", False))[0],
                              "hours": h} for pid, h in ranked[:UNPLANNED_ROWS]],
                    "extra_n": len(extra), "extra_h": sum(h for _p, h in extra)})
        out.append({
            "name": name, "capacity": capacity,
            "plan_total": sum(plan_h.values()), "act_total": sum(act_h.values()),
            "plan_days": sum(plan_h.values()) / HOURS_PER_DAY,
            "planned_weeks": planned_weeks, "n_weeks": len(past),
            "pool": name in cfg["pool_devs"], "empty": not plan_h and not act_h,
            "planned_rows": [{"project": projects.get(pid, (f"#{pid}", False))[0],
                              "planned": hours, "logged": act_h.get(pid, 0.0),
                              "no_project": pid == 0,
                              "delta": None if pid == 0 else act_h.get(pid, 0.0) - hours}
                             for pid, hours in sorted(plan_rows, key=lambda kv: -kv[1])],
            "unplanned": groups})
    return out


def quickstart_share_data(ctx):
    """Per developer: the share of logged hours that went to quickstart, week
    by week over the history window, and on average.

    Every logged hour (leave already out of `actual`) falls in one of three:
    - **plan**: a project that had at least one allocation slot (> 0 day) in
      the window, for anyone — the `dev_plan_scope` rule, applied to the
      window read rather than to the weeks ahead, so a project planned last
      month still counts as planned for last month's hours;
    - **quickstart**: any other *client* project;
    - **interne**: a non-client project (or no project) — meetings, internal
      tools. Kept out of quickstart, which is client work outside the plan.

    The share is quickstart ÷ all logged hours, not ÷ capacity: it answers
    "of the time he logged, how much went to quickstart". The running week is
    shown but, like the occupation, left out of the average."""
    import dev_plan_scope
    past, current = ctx["past"], ctx["weeks"]["current"]
    actual, planned, projects = ctx["actual"], ctx["planned"], ctx["projects"]
    scope = dev_plan_scope.Scope(
        {pid for (_n, w, pid), d in planned.items() if pid and d > 0 and w in past},
        ctx["cfg"])

    def kind(pid):
        name, client = projects.get(pid, (f"#{pid}", False)) if pid else ("", False)
        if scope.is_planned([pid, name] if pid else None):
            return "plan"
        return "quick" if client else "internal"

    rows, team = [], defaultdict(float)
    for _uid, _eid, name in ctx["devs"]:
        per_week = {w: defaultdict(float) for w in past}
        for (n, w, pid), hours in actual.items():
            if n == name and w in per_week:
                per_week[w][kind(pid)] += hours
        cells, totals = [], defaultdict(float)
        for week in past:
            split = per_week[week]
            worked = sum(split.values())
            cells.append({"label": wlabel(week), "current": week == current,
                          "worked": worked, "quick": split["quick"],
                          "share": pct(split["quick"], worked)})
            if week != current or len(past) == 1:
                for k, v in split.items():
                    totals[k] += v
        worked = sum(totals.values())
        if worked < 0.05 and not any(c["worked"] for c in cells):
            continue
        for k, v in totals.items():
            team[k] += v
        rows.append({"name": name, "cells": cells, "worked": worked,
                     "plan": totals["plan"], "quick": totals["quick"],
                     "internal": totals["internal"],
                     "share": pct(totals["quick"], worked)})
    worked = sum(team.values())
    return {"rows": rows,
            "team": {"worked": worked, "plan": team["plan"],
                     "quick": team["quick"], "internal": team["internal"],
                     "share": pct(team["quick"], worked)}}


def forward_data(ctx, occupancy):
    """C — per developer: days committed per week ahead (out of the days left
    after leave), the average free days the plan leaves, and the occupation
    badge that must be read beside it."""
    future, planned, projects = ctx["future"], ctx["planned"], ctx["projects"]
    leave, flags, cfg = ctx["leave"], ctx["flags"], ctx["cfg"]
    rows = []
    for _uid, _eid, name in ctx["devs"]:
        sched = ctx["schedules"][name]
        basis_days = len(sched["hours"])
        cells, frees, targets = [], [], set()
        for week in future:
            days = sum(d for (n, w, _p), d in planned.items() if n == name and w == week)
            leave_days = leave[(name, week)] / HOURS_PER_DAY
            available = max(basis_days - leave_days, 0.0)
            cells.append({"week": week, "label": wlabel(week), "days": days,
                          "available": available, "short": leave_days > 0.5})
            frees.append(available - days)
            for (n, w, pid), d in planned.items():
                if n == name and w == week and d:
                    targets.add(projects.get(pid, (f"#{pid}", False))[0])
        pool_idle = (name in cfg["pool_devs"]
                     and not any(c["days"] for c in cells))
        occ = occupancy.get(name)
        gap = GAP_FLAG in flags.get(name, [])
        rows.append({"name": name, "days": basis_days, "cells": cells,
                     "pool_idle": pool_idle,
                     "free_avg": sum(frees) / len(frees) if frees else 0.0,
                     "occupancy": occ, "badge": occ_badge(occ, gap), "gap": gap,
                     "targets": sorted(targets)})
    return rows


def runway_data(ctx):
    """D — per project: dev backlog, dev capacity planned per week ahead, and
    how many weeks the one takes to burn the other."""
    cfg, future = ctx["cfg"], ctx["future"]
    per_project = defaultdict(float)
    for (pid, week, role), days in ctx["dev_days"].items():
        if role in cfg["dev_role_names"] and week in future:
            per_project[pid] += days
    weeks_ahead = max(len(future), 1)
    rows = []
    for pid in set(ctx["backlog"]) | set(per_project):
        left = ctx["backlog"].get(pid, 0.0)
        per_week = per_project.get(pid, 0.0) / weeks_ahead * HOURS_PER_DAY
        if left < 0.05 and per_week < 0.05:
            continue
        if per_week < 0.05:
            state, weeks = ("none" if left >= 0.05 else "idle"), None
        elif left < 0.05:
            state, weeks = "done", None
        else:
            state, weeks = "ok", left / per_week
        rows.append({"project": ctx["projects"].get(pid, (f"#{pid}", False))[0],
                     "left": left, "per_week": per_week, "state": state,
                     "weeks": weeks})
    rows.sort(key=lambda r: -r["left"])
    return rows


def notes_data(ctx):
    """E — the data-quality lines, each starting ⚠️ (makes a figure less
    reliable) or ℹ️ (context needed to read one). Markdown."""
    cfg, today, weeks = ctx["cfg"], ctx["today"], ctx["weeks"]
    projects, planned, weekday = ctx["projects"], ctx["planned"], ctx["weekday"]
    lines = list(ctx["notes"])
    for _uid, _eid, name in ctx["devs"]:
        sched = ctx["schedules"][name]
        observed = detect_pattern({d: weekday[(name, d)] for d in range(7)}, sched)
        if observed is not None:
            lines.append(
                f"⚠️ **{name}** — le calendrier dit "
                f"{'/'.join(WEEKDAYS[d] for d in sorted(sched['hours']))} "
                f"({sched['source']}), les feuilles de temps disent "
                f"{'/'.join(WEEKDAYS[d] for d in sorted(observed))} sur les "
                f"{cfg['schedule_detect_weeks']} dernières semaines. Corriger le "
                "calendrier dans Odoo, ou ajouter le régime dans `dev_schedules` "
                "(digest_config.py) — d'ici là, l'occupation de ce développeur est "
                "calculée sur le mauvais dénominateur.")
        elif sched["overridden"]:
            lines.append(
                f"ℹ️ **{name}** — capacité issue de `dev_schedules` "
                f"({sched['source']}) ; la production le tient toujours sur "
                f"\"{sched['calendar']}\" "
                f"({'/'.join(WEEKDAYS[d] for d in sched['cal_days'])}). Retirer la "
                "surcharge dès que les RH encodent le vrai régime.")
        if not sched["hours"]:
            lines.append(f"⚠️ **{name}** — aucun régime de travail ; l'occupation "
                         "ne peut pas être calculée.")
        unattributed = sum(d for (n, w, pid), d in planned.items()
                           if n == name and pid == 0)
        if unattributed:
            lines.append(f"⚠️ **{name}** — {unattributed:.1f} jours d'allocation "
                         "**sans projet** sur la période : de la capacité engagée "
                         "qui ne pointe nulle part.")
        if name in cfg["pool_devs"]:
            lines.append(f"ℹ️ **{name}** — pool de renfort/support : aucune "
                         "allocation projet par conception, jugé sur l'occupation "
                         "seule, jamais sur l'adhérence.")
    lines.append(f"ℹ️ Absences lues sur le(s) projet(s) de congés "
                 f"{', '.join(projects.get(p, (str(p), False))[0] for p in cfg['leave_project_ids'])}"
                 ", et non sur `hr.leave`, dont un compte ordinaire ne voit que ses "
                 "propres enregistrements.")
    lines.append(f"ℹ️ Semaine en cours {wlabel(weeks['current'])} proratisée "
                 f"jusqu'au {WEEKDAYS[today.weekday()]} {today.isoformat()} — par "
                 "développeur, de sorte qu'une semaine de quatre jours est complète "
                 "là où une semaine de cinq jours en est à 80 %.")
    lines.append("ℹ️ Le `state` des allocations est ignoré : tous les créneaux de "
                 "cette base sont en `draft`, filtrer dessus ne renverrait rien.")
    return lines


# --- sections (Markdown) -----------------------------------------------------

def section_occupation(ctx, occ):
    """A — hours logged against net capacity, client and internal apart."""
    past, weeks, cfg = ctx["past"], ctx["weeks"], ctx["cfg"]
    print(f"## Occupation — heures pointées vs capacité nette "
          f"({wlabel(past[0])} → {wlabel(past[-1])})\n")
    print("_Occupation = heures pointées ÷ **capacité nette** (les heures du "
          "calendrier propre au développeur, moins les absences pointées sur "
          f"{', '.join(ctx['projects'][p][0] for p in cfg['leave_project_ids'] if p in ctx['projects']) or 'le projet de congés'})"
          ". La semaine en cours est marquée `*` et proratisée aux jours "
          "ouvrés déjà écoulés pour ce développeur : une semaine de quatre jours "
          "est donc déjà complète un jeudi, là où une semaine de cinq jours en "
          "est à 80 %. Une semaine entièrement en congé affiche `— congé`. "
          "**La moyenne ne porte que sur les semaines complètes** — la semaine "
          "en cours est affichée pour l'immédiateté mais exclue, car des "
          "feuilles de temps remplies en fin de semaine feraient passer une "
          "équipe pleine pour une équipe disponible._\n")
    leave_names = _leave_names(ctx)
    legend("Occupation", [
        ("`5 j · 40h`", "la semaine propre au développeur — jours travaillés × "
                        "heures hebdomadaires, lues sur son `resource.calendar` "
                        "ou sur une surcharge `dev_schedules`. Jamais supposée "
                        "égale à 5 × 8h."),
        ("`38.5/40h 96%`", "heures pointées / capacité nette de la semaine, et le "
                           "rapport des deux."),
        (f"`{wlabel(weeks['current'])}*`", "la semaine en cours : proratisée aux "
                                           "jours ouvrés écoulés du développeur, "
                                           "et **exclue de `Moy.`**."),
        ("`— congé`", f"semaine entièrement en absence (pointée sur {leave_names}) ; "
                      "la semaine sort de `Moy.`"),
        ("`—`", "aucune capacité cette semaine — pas de calendrier sur l'employé."),
        ("`96% ⚠️`", "plus de 120 % de la semaine : heures supplémentaires, ou "
                     "heures pointées un jour posé."),
        ("🟢", f"moyenne sous {ROOM_BELOW:.0f} % de la capacité nette — de la place "
               "pour du travail supplémentaire."),
        ("🟡", f"{ROOM_BELOW:.0f}–{FULL_ABOVE:.0f} % — chargé, sans être plein."),
        ("🔴", f"{FULL_ABOVE:.0f} % et plus — plein : toute nouvelle tâche en "
               "déplace une autre."),
        ("⚠️ (dans `Moy.`)", f"non mesurable — moins de {cfg['gap_ratio'] * 100:.0f} % "
                             "de la capacité pointée, sans absence qui l'explique. "
                             "Des feuilles de temps manquantes ne sont **jamais** "
                             "de la capacité libre : aucun badge vert n'est donc "
                             "affiché sur un pointage incomplet."),
        ("`(partiel)`", "la moyenne ne repose que sur la semaine en cours "
                        "(`--weeks-back 0`) — une lecture de milieu de semaine, "
                        "pas un verdict."),
        ("`Client` / `Interne`", "répartition sur `project.project.partner_id` : un "
                                 "projet avec un client est du travail client. Pas "
                                 "sur `allow_billable`, qui marque aussi `Interne`."),
        (f"`{POOL_FLAG}`", "développeur de renfort / support : aucune allocation "
                           "projet par conception, jugé sur l'occupation seule, "
                           "jamais sur l'adhérence."),
        (f"`{GAP_FLAG}`", "le pointage incomplet ci-dessus, repris en indicateur."),
        (f"`{MISMATCH_FLAG}`", "les jours réellement travaillés contredisent le "
                               "calendrier — voir **Qualité des données**."),
    ])
    header = (["Développeur", "Régime"] + [wlabel(w) + ("*" if w == weeks["current"] else "")
                                       for w in past]
              + ["Moy.", "Client", "Interne", "Remarques"])
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(["---"] * len(header)) + "|")
    for r in occ["rows"]:
        row = []
        for c in r["cells"]:
            if c["share"] is None:
                row.append("— congé" if c["leave"] else "—")
            else:
                over = " ⚠️" if c["over"] else ""
                row.append(f"{c['worked']:.1f}/{c['capacity']:.0f}h {c['share']:.0f}%{over}")
        avg = r["avg"]
        avg_txt = (f"{r['badge']} {avg:.0f}%{' (partiel)' if r['partial'] else ''}"
                   if avg is not None else r["badge"])
        print("| " + " | ".join(
            [f"**{cell(r['name'])}**", f"{r['days']} j · {r['hours']:.0f}h"] + row
            + [avg_txt, f"{r['client']:.1f}h", f"{r['internal']:.1f}h",
               " ".join(r["flags"]) or "—"]) + " |")


def section_adherence(ctx, rows, fold=False):
    """B — planned against actual per project, plus everything spent off-plan.

    No verdict and no threshold: the plan is indicative, and with ~40% of dev
    capacity planned an adherence alarm would fire permanently.

    `fold=True` folds each developer's block, as tab 1 folds theirs: the summary
    line already carries the three figures the section is read for — net
    capacity, hours planned, hours logged — and the project-by-project split
    below is what one opens once a line looks wrong. Section A above ranks the
    same people by occupation, so the choice of whom to open is made there."""
    past = ctx["past"]
    print(f"\n## Adhérence — planifié vs réel par projet ({wlabel(past[0])} → {wlabel(past[-1])})\n")
    print("_Les heures planifiées sont les jours d'allocation × 8. Les écarts sont "
          "constatés, pas jugés : le plan est indicatif, et le travail hors plan "
          "d'un développeur est le plus souvent du support ou de l'interne que le "
          "plan n'a jamais couvert, plutôt qu'un écart de conduite. "
          f"`{NO_PROJECT}` est un créneau d'allocation sans projet — de la "
          "capacité engagée qui ne pointe nulle part._\n")
    legend("Adhérence", [
        ("`Planifié`", "jours d'allocation × 8h. Un jour de plan est un jour-homme, "
                       "pas les heures journalières propres au développeur."),
        ("`Pointé`", "heures réellement pointées sur ce projet sur la période."),
        ("`Écart`", "pointé − planifié, sur un projet qui était planifié. Positif = "
                    "plus de temps que promis, négatif = moins. Constaté, jamais jugé."),
        ("`32h planifiées (4 j sur 2 des 5 semaines)`", "les heures planifiées, "
                                                        "puis les **jours** "
                                                        "d'allocation qui les "
                                                        "produisent et le nombre de "
                                                        "**semaines** où un plan "
                                                        "existait. Les deux comptes "
                                                        "ne sont pas dans la même "
                                                        "unité : 4 jours répartis "
                                                        "sur 2 semaines, soit 2 "
                                                        "jours par semaine, font "
                                                        "bien 32h. Un écart "
                                                        "important avec peu de "
                                                        "semaines couvertes, c'est "
                                                        "un plan absent, pas un "
                                                        "développeur qui a dévié."),
        (f"`{NO_PROJECT}` · `⚠️ créneau sans projet`", "un créneau d'allocation sans "
                                                       "projet : de la capacité "
                                                       "engagée qui ne pointe nulle "
                                                       "part, donc aucun écart "
                                                       "calculable."),
        ("`hors plan — client`", "heures sur un projet client que le plan n'a jamais "
                                 "couvert."),
        ("`hors plan — interne`", "idem, sur un projet sans client — support, "
                                  "`Interne`, R&D."),
        ("`_+N autres projets_`", f"la traîne au-delà des {UNPLANNED_ROWS} plus "
                                  "gros postes, sommée plutôt que supprimée — un "
                                  "développeur support touche légitimement plus de "
                                  "20 projets par mois."),
        ("`_renfort : pas d'allocation projet par conception_`", "ce développeur est "
                                                                 "un renfort : aucun "
                                                                 "plan n'est attendu, "
                                                                 "donc aucune "
                                                                 "adhérence n'est "
                                                                 "calculée."),
    ])
    for r in rows:
        pool = (" · _renfort : pas d'allocation projet par conception_"
                if r["pool"] else "")
        # The plan is younger than the timesheets: a week with no slot
        # contributes 0 planned, so a large positive delta usually means the
        # plan did not exist yet rather than that the developer overshot it.
        coverage = ("" if r["pool"]
                    else f" sur {r['planned_weeks']} des {r['n_weeks']} semaines")
        # The days are spelled out next to the hours on purpose: "32h · plan on
        # 2/5 weeks" reads as a contradiction, because 2 counts *weeks with a
        # plan*, not days.
        opening, closing = md_to_html.section_tags(
            f"{r['name']} — {r['capacity']:.0f}h de capacité nette · "
            f"{r['plan_total']:.0f}h planifiées "
            f"({r['plan_days']:.4g} j{coverage}) · "
            f"{r['act_total']:.1f}h pointées{pool}", fold)
        print(opening + "\n")
        if r["empty"]:
            print("_(rien de planifié, rien de pointé)_\n")
            if closing:
                print(closing + "\n")
            continue
        if r["planned_rows"]:
            print("| Projet planifié | Planifié | Pointé | Écart |")
            print("|---|---|---|---|")
            for pr in r["planned_rows"]:
                delta = ("⚠️ créneau sans projet" if pr["no_project"]
                         else f"{pr['delta']:+.1f}h")
                print(f"| {cell(pr['project'])} | {pr['planned']:.0f}h | "
                      f"{pr['logged']:.1f}h | {delta} |")
        first = not r["planned_rows"]
        for g in r["unplanned"]:
            print(f"| {g['title']} | Pointé |" if first else f"\n| {g['title']} | Pointé |")
            first = False
            print("|---|---|")
            for ur in g["rows"]:
                print(f"| {cell(ur['project'])} | {ur['hours']:.1f}h |")
            # Never truncate silently: a support developer legitimately touches
            # 20+ projects a month.
            if g["extra_n"]:
                print(f"| _+{g['extra_n']} autres projets_ | {g['extra_h']:.1f}h |")
        print(f"\n{closing}\n" if closing else "")


def section_forward(ctx, rows):
    """C — committed days ahead, against each developer's own week.

    Occupation travels with this table on purpose: read alone, the plan says
    Nathan has three free days while section A has him at 104%."""
    future = ctx["future"]
    if not future:
        return
    print(f"\n## Engagement à venir ({wlabel(future[0])} → {wlabel(future[-1])})\n")
    print("_Jours d'allocation déjà engagés, rapportés à la semaine propre de "
          "chaque développeur. Les jours fériés sont déduits, car ils sont "
          "pré-pointés en congé. **Les jours libres viennent du plan seul** — la "
          "colonne d'occupation est là pour que les deux ne soient jamais lus "
          "séparément : un développeur peut avoir 3 jours non planifiés et être "
          "occupé à plus de 100 %, ce qui veut dire que le plan est incomplet, "
          "pas qu'il est disponible._\n")
    legend("Engagement à venir", [
        ("`2.0`", "jours d'allocation engagés sur cette semaine."),
        ("`2.0 sur 4.0j`", "jours engagés sur les jours restants **après congés** — "
                           "une semaine raccourcie par un férié ne peut jamais "
                           "passer pour un jour libre."),
        ("`0.0`", "rien d'engagé cette semaine — ce qui est un trou dans le plan, "
                  "pas une semaine libre démontrée."),
        ("`Jours libres/sem.`", "moyenne des jours par semaine que le plan laisse "
                                "non engagés. `+` = de la marge **dans le plan** ; "
                                "à lire avec la colonne suivante."),
        ("`Occupation`", "le badge `Moy.` de la section Occupation, repris ici pour "
                         "que les jours libres ne soient jamais lus seuls : 3 jours "
                         "non planifiés chez un développeur 🔴 signifient que le "
                         "plan est incomplet."),
        ("`_renfort_`", "aucune allocation par conception — jugé sur l'occupation "
                        "seule."),
        ("`Engagé sur`", "tout projet portant au moins un jour d'allocation pour ce "
                         "développeur sur la période."),
    ])
    header = (["Développeur", "Régime"] + [wlabel(w) for w in future]
              + ["Jours libres/sem.", "Occupation", "Engagé sur"])
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(["---"] * len(header)) + "|")
    for r in rows:
        if r["pool_idle"]:
            cells, free_txt = ["—"] * len(future), "_renfort_"
        else:
            cells = [f"{c['days']:.1f}" + (f" sur {c['available']:.1f}j" if c["short"] else "")
                     for c in r["cells"]]
            free_txt = f"{r['free_avg']:+.1f}"
        occ_txt = (f"{r['badge']} {r['occupancy']:.0f}%" if r["occupancy"] is not None
                   else r["badge"])
        target_txt = ", ".join(cell(t) for t in r["targets"]) or "—"
        print("| " + " | ".join([f"**{cell(r['name'])}**", f"{r['days']} j"] + cells
                                + [free_txt, occ_txt, target_txt]) + " |")


def section_runway(ctx, rows):
    """D — the bridge between the two KPIs: a project's dev backlog over the dev
    capacity actually planned for it."""
    cfg, future = ctx["cfg"], ctx["future"]
    print(f"\n## Autonomie par projet — reste à faire dev ÷ capacité dev planifiée\n")
    print("_Le reste à faire est `dev_time` − les heures pointées **par les "
          "développeurs** sur les tickets en "
          f"{' · '.join(workload.DEV_STAGES)}, exactement la définition retenue "
          "par `/dev-workload`. La capacité dev planifiée est la moyenne de "
          f"l'allocation {', '.join(cfg['dev_role_names'])} sur "
          f"{wlabel(future[0]) if future else 'la période'}"
          f"{' → ' + wlabel(future[-1]) if len(future) > 1 else ''}. "
          "`⚠️ aucun dev planifié` est la case à regarder : du travail engagé "
          "chez un client sans personne pour le faire._\n")
    legend("Autonomie", [
        ("`Reste à faire dev`", "Σ (`dev_time` − heures pointées **par les "
                                "développeurs**) sur les tickets dev vivants du "
                                "projet — les étapes "
                                f"{' · '.join(workload.DEV_STAGES)}. Un ticket sans "
                                "`dev_time` ne contribue à rien : il ne peut pas "
                                "gonfler le reste à faire — ni être vu ici."),
        ("`Dev planifié/sem.`", "moyenne hebdomadaire de l'allocation "
                                f"{', '.join(cfg['dev_role_names'])} sur ce projet "
                                "pour la période à venir, en heures (jours × 8)."),
        ("`1.3 sem.`", "reste à faire ÷ capacité hebdomadaire planifiée : semaines "
                       "nécessaires pour absorber le reste à faire au rythme "
                       "actuellement planifié."),
        ("`∞ ⚠️ aucun dev planifié`", "du reste à faire avec **zéro** allocation dev "
                                      "devant — du travail engagé chez un client "
                                      "sans personne pour le faire. La case la plus "
                                      "parlante du rapport."),
        ("`✅ à jour`", "de la capacité planifiée et plus de reste à faire dessus."),
        ("`—`", "ni reste à faire ni allocation qui vaille l'affichage."),
    ])
    if not rows:
        print("_(aucun reste à faire dev et aucune allocation dev sur la période)_")
        return
    print("| Projet | Reste à faire dev | Dev planifié/sem. | Autonomie |")
    print("|---|---|---|---|")
    for r in rows:
        runway = {"none": "∞ ⚠️ aucun dev planifié", "idle": "—",
                  "done": "✅ à jour"}.get(r["state"]) or f"{r['weeks']:.1f} sem."
        print(f"| {cell(r['project'])} | {r['left']:.1f}h | {r['per_week']:.1f}h | {runway} |")


def section_notes(lines):
    """E — always printed, so the numbers can be trusted or discounted on sight."""
    print("\n## Qualité des données\n")
    legend("Qualité des données", [
        ("⚠️", "rend un chiffre ci-dessus moins fiable — un calendrier erroné, un "
               "développeur sans feuilles de temps, un créneau d'allocation sans "
               "projet."),
        ("ℹ️", "le contexte nécessaire pour lire un chiffre correctement, pas un "
               "problème : d'où viennent la capacité, les absences ou le prorata."),
    ])
    for line in lines:
        print(f"- {line}")


# --- main --------------------------------------------------------------------

def build(argv=None, kw=None, cfg=None, fold=False):
    """Print the report as Markdown.

    `kw`/`cfg` let a caller that has already authenticated — `dev_app`, which
    puts this report behind a tab — reuse its connection. Passing neither keeps
    the standalone behaviour, so `/dev-capacity` is unaffected.

    `fold=True` folds section B's per-developer blocks. Off by default: on its
    own page the report is read top to bottom, and only the application, where
    this is one tab of four, gains from folding them."""
    ap = argparse.ArgumentParser(
        description="Per-developer capacity: plan vs actual vs backlog.")
    ap.add_argument("dev", nargs="?", help="one developer (name or substring)")
    ap.add_argument("--weeks-back", type=int, default=4,
                    help="complete weeks of history, plus the running one (default 4)")
    ap.add_argument("--weeks-fwd", type=int, default=4,
                    help="weeks of committed plan ahead (default 4)")
    ap.add_argument("--today", help="pretend today is this date (YYYY-MM-DD)")
    args = ap.parse_args(argv)

    today = (datetime.date.fromisoformat(args.today) if args.today
             else datetime.date.today())
    cfg = cfg or digest_config.load()
    kw = kw or digest_config.connect(cfg)
    ctx = gather(kw, cfg, today, args.weeks_back, args.weeks_fwd, args.dev)

    print(f"# DEV TSC — Capacité · {today.isoformat()}\n")
    print(f"_{len(ctx['devs'])} développeur(s) · {ctx['n_lines']} lignes de feuille de temps · "
          f"{ctx['n_slots']} créneaux d'allocation · {ctx['n_tasks']} tickets dev vivants. "
          "Planifié vs réel vs reste à faire, joints sur (développeur, projet, "
          "semaine ISO)._\n")
    occ = occupation_data(ctx)
    section_occupation(ctx, occ)
    section_adherence(ctx, adherence_data(ctx), fold=fold)
    section_forward(ctx, forward_data(ctx, occ["occupancy"]))
    section_runway(ctx, runway_data(ctx))
    section_notes(notes_data(ctx))


def main(argv=None):
    """HTML is the default output.

    The report is five sections deep with a legend under each table: it reads as
    a page and scrolls badly as terminal Markdown. `--format md` (or `--md`)
    prints the Markdown alone. In HTML mode the Markdown still goes to stdout —
    a run whose only output was a file path would leave a terminal reader
    blind — and the page is opened unless `--no-open` says otherwise."""
    argv = sys.argv[1:] if argv is None else argv
    fmt = "html"
    open_it = True
    rest = []
    for arg in argv:
        if arg in ("--format=html", "--html"):
            fmt = "html"
        elif arg in ("--format=md", "--md", "--markdown"):
            fmt = "md"
        elif arg == "--open":
            open_it = True
        elif arg == "--no-open":
            open_it = False
        else:
            rest.append(arg)
    if "--format" in rest:
        i = rest.index("--format")
        fmt = rest[i + 1] if i + 1 < len(rest) else "html"
        del rest[i:i + 2]
    if fmt == "md":
        build(rest)
        return
    import io
    from contextlib import redirect_stdout
    import digest as digest_mod
    import md_to_html
    buf = io.StringIO()
    with redirect_stdout(buf):
        build(rest)
    markdown = buf.getvalue()
    out_dir = digest_mod.output_dir()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "capacity.html")
    with open(path, "w") as fh:
        fh.write(md_to_html.render(markdown, title="DEV TSC — Capacité"))
    print(markdown)
    print(path, file=sys.stderr)
    if open_it:
        # A plain window, not the digest's --app mode: this page is consulted
        # alongside Odoo tabs and wants an address bar and a tab strip.
        digest_mod.open_in_browser(path, app=False)


if __name__ == "__main__":
    main()
