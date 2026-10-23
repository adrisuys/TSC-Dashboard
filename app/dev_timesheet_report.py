#!/usr/bin/env python3
"""Les feuilles de temps de l'équipe DEV : tenues à jour, et facturables ?

Usage:
    python3 dev_timesheet_report.py                   # page HTML + Markdown
    python3 dev_timesheet_report.py "Nathan"          # un développeur
    python3 dev_timesheet_report.py --days 180
    python3 dev_timesheet_report.py --format md       # Markdown seul

Two questions, and they have to be answered in this order:

  A  Les feuilles sont-elles tenues ?   jours ouvrés pointés vs régime réel
  B  Facturable vs non facturable ?     part facturable de ce qui est pointé

B is meaningless without A. A ratio computed on half-filled timesheets is not a
measure of how the team sells its time, it is a measure of who remembers to
click. So the fill rate comes first on the page, and every developer under the
completeness threshold is flagged in section B too — their percentage is printed
but it does not enter the team figure.

*Le facturable se lit sur `timesheet_invoice_type`*, not on the project's
`partner_id` and not on `allow_billable`. `dev_capacity_report` splits
client/internal on `partner_id` because it only needs to know whether the hour
was spent for someone outside; that is a coarser question. `allow_billable` is
useless here — it is set on `Interne`. `timesheet_invoice_type` is filled on
100 % of this database's timesheet lines and carries the distinction the team
actually steers on, including a `non_billable_invest` value for deliberate
internal investment.

*Two ratios are printed, never one.* Strict counts internal investment against
the team; `hors invest` sets it aside. Investment in TSC's own product is a
decision, not slack, and a single number would either hide it or punish it. The
gap between the two ratios is the price of that decision, which is the figure
worth discussing.

*Les congés sortent du dénominateur.* Leave is booked on a partner-less "Congés"
project (`cfg["leave_project_ids"]`) and typed `non_billable`. Counting it as
non-billable work would make a fortnight off read as a collapse in billability.

Read-only: search, search_read and read only.
"""
import argparse
import datetime
import os
import re
import statistics
import sys
from collections import defaultdict
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_capacity_report as cap  # noqa: E402
import dev_periods as periods  # noqa: E402
import digest  # noqa: E402
import digest_config  # noqa: E402
import md_to_html  # noqa: E402

TITLE = "DEV TSC — Feuilles de temps"

# The team's own target, and the bands around it. 85 % is a management target,
# not a derived figure: it lives here rather than in the config because changing
# it changes what the page means, and that deserves a commit.
#
# It applies to `cfg["target_devs"]` only — the Belgian employees, on whom the
# company bonus is computed. Everyone else is shown for information, never
# coloured against the target and never counted in the figure that is: the
# "Objectif" row, the verdict, the watch list, the trend and the projection.
TARGET = 85.0
AMBER_BELOW = 75.0

# The billable window defaults to the calendar year to date, because the 85 %
# target is a yearly one: a rolling ninety days answers "how are we doing lately",
# which is a different question and one nobody is held to. `--days N` gives the
# rolling window back for a spot check.
# The second window, shown beside the year by default. The year says whether the
# target will be met; ninety days says who to sit down with this month, and the
# two are genuinely different questions — a developer at 80 % for the year but
# 60 % lately needs attention the yearly figure has not noticed yet.
RECENT_WINDOW = 90
GRID_WEEKS = 4         # semaines affichées dans la grille de tenue
RECENT_DAYS = 5        # jours ouvrés qui décident du badge de tenue
# Past this many weeks the trend switches from weekly to monthly buckets. Thirty
# four weekly rows is not a trend, it is a log; a year reads as twelve months.
TREND_MAX_WEEKS = 16

# A day counts as filled at half its expected hours (`cfg["gap_ratio"]`), the
# same threshold `dev_capacity_report` uses for its ⚠️ gap flag — so the two
# pages can never designate two different laggards.

BILLABLE_TYPES = {"billable_time", "billable_fixed", "billable_milestones",
                  "billable_manual", "timesheet_revenues", "service_revenues",
                  "other_revenues"}
INVEST_TYPES = {"non_billable_invest"}
# Everything Odoo can put in the field today. A type outside this set is a value
# added to the instance after this page was written: it is counted as internal
# (the conservative side of the ratio) and reported under **Notes**, rather than
# silently inflating the percentage.
KNOWN_TYPES = BILLABLE_TYPES | INVEST_TYPES | {"non_billable", "other_costs"}

# Leave booked somewhere other than the Congés project. Deliberately narrow, and
# deliberately *reported* rather than acted on: it matches free text a human
# typed, so it can only ever be a hint. `récup` was in an earlier version and had
# to go — it matches "récupération", which is in half the dev lines on the
# instance. `absence` went the same way ("le support en l'absence de Matthieu").
# What survives still catches work: "Clean retour de vacances" is a real morning
# of work, and "meetup avant ses congés" is a meeting. That is exactly why the
# hours below are shown as a correction the reader can apply, never subtracted
# from the ratio behind their back.
LEAVE_RX = re.compile(r"cong[ée]s?\b|vacances?\b|maladie|malade\b|f[ée]ri[ée]|\brtt\b",
                      re.IGNORECASE)
# Below this share of hours worked, the correction is noise and the section is
# not printed at all.
MISFILED_FLOOR = 0.01

# Grid glyphs. Monospace inside backticks, like the load bars of the workload
# report — they line up in the rendered table and in a terminal alike.
#
# There is deliberately no "partially filled" glyph. An earlier version drew ▄
# for a day between half and 90 % of the expected hours, which encoded four
# states for a two-state question — and it read as an accusation against the
# people who were fine: Thomas books 6 to 8 hours a day and came out
# `▄██▄█▄▄▄▄▄██▄▄█`, a broken barcode next to a "Trous" column reading "—".
# Booking 6 h instead of 8 is ordinary variance, not a timesheet that is not
# being kept, and this grid answers only the second question.
FULL, EMPTY, LEAVE, OFF = "█", "·", "▒", " "

# A launchd job runs under the C locale, where %a renders "Mon 24/08" in the
# middle of a French page. Same reason `dev_dashboard` carries its own names.
JOURS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août",
        "sept.", "oct.", "nov.", "déc."]


def date_fr(date):
    return f"{JOURS[date.weekday()]} {date:%d/%m}"


def window_bounds(today, days=None):
    """(date_from, label, label_de) — the calendar year to date unless `days` says
    otherwise. The third item is the same label after a preposition: French needs
    "de l'année 2026" but "des 90 derniers jours", and no rule derives one from
    the other.

    Year to date, not a rolling twelve months: the target is set per calendar
    year, so the figure has to reset when the year does. A rolling window would
    keep dragging last year's November into this year's verdict, and no one is
    accountable for that."""
    if days:
        return (today - datetime.timedelta(days=days),
                f"{days} derniers jours", f"des {days} derniers jours")
    return (datetime.date(today.year, 1, 1), f"année {today.year}",
            f"de l'année {today.year}")


def link_text(text):
    """Text safe inside a Markdown `[...]` — brackets would end the label."""
    return cap.cell(text).replace("[", "(").replace("]", ")")


def hd(hours):
    return f"{hours:.1f}h"


def pct_txt(value):
    return "—" if value is None else f"{value:.0f}%"


def ratio(part, whole):
    return None if whole <= 0.05 else part / whole * 100.0


def target_names(cfg):
    """The developers the target applies to (see TARGET)."""
    return set(cfg.get("target_devs") or [])


def target_badge(value):
    """🟢 at or above target · 🟡 within ten points · 🔴 below."""
    if value is None:
        return "—"
    if value >= TARGET:
        return "🟢"
    if value >= AMBER_BELOW:
        return "🟡"
    return "🔴"


def fill_badge(missing_recent):
    if missing_recent >= 3:
        return "🔴"
    return "🟡" if missing_recent else "🟢"


def bar(value, width=20):
    """A 0–100 % block bar, with the target marked by a `|` inside it."""
    if value is None:
        return "`" + " " * width + "`"
    filled = max(0, min(width, int(round(value / 100.0 * width))))
    cells = [FULL if i < filled else "░" for i in range(width)]
    mark = min(width - 1, int(round(TARGET / 100.0 * width)))
    if cells[mark] == "░":
        cells[mark] = "▏"
    return "`%s`" % "".join(cells)


# --- scope -------------------------------------------------------------------

def resolve_devs(kw, cfg, needle=None):
    """[{name, user_id, emp_ids}] — *every* employee record of each developer.

    Resolving a developer by `hr.employee.name` alone loses one who carries an
    employee per company. Production holds both `David Andrian` (TSC France) and
    `David Andrian (BE)` (The Service Company) against the same user 1219, with
    92 % of the hours on the second; matching the configured name picks the
    first and reads the developer as very nearly idle. `Thomas Dieuzeide (FR)`
    is the same shape. The *user* is the identity that survives the split, so
    the employees are gathered through `user_id` and their lines summed."""
    names = list(cfg["dev_names"])
    if needle:
        matches = [n for n in names if needle.lower() in n.lower()]
        if not matches:
            sys.exit(f"Aucun développeur ne correspond à {needle!r}. "
                     f"Connus : {', '.join(names)}")
        if len(matches) > 1:
            sys.exit(f"{needle!r} est ambigu : {', '.join(matches)}")
        names = matches
    users = kw("res.users", "search_read", [[["name", "in", names]]],
               {"fields": ["name"]})
    by_uid = {u["id"]: u["name"] for u in users}
    if not by_uid:
        sys.exit(f"Aucun utilisateur Odoo ne porte l'un de ces noms : "
                 f"{', '.join(names)}. Corrigez `dev_names` dans "
                 f"{digest_config.CONFIG_PATH}.")
    found = kw("hr.employee", "search_read",
               [[["user_id", "in", sorted(by_uid)]]],
               {"fields": ["name", "user_id", "resource_calendar_id",
                           "company_id"]})
    per_uid = defaultdict(list)
    for emp in found:
        per_uid[emp["user_id"][0]].append(emp)
    devs, missing = [], []
    for name in names:                       # configured order: stable runs
        uid = next((u for u, n in by_uid.items() if n == name), None)
        if uid is None or not per_uid.get(uid):
            missing.append(name)
            continue
        devs.append({"name": name, "user_id": uid,
                     "employees": per_uid[uid],
                     "emp_ids": [e["id"] for e in per_uid[uid]]})
    return devs, missing


def primary_employee(dev, hours_by_emp):
    """The employee record the developer's schedule is read from.

    The one they actually book their hours on, not the first row Odoo returns:
    the France-side David Andrian holds 44 h against the Belgian record's 505 h,
    and it is the busy record whose calendar describes his week. Falls back to
    the first record carrying a calendar, then to the first record at all."""
    ranked = sorted(dev["employees"],
                    key=lambda e: (-hours_by_emp.get(e["id"], 0.0),
                                   0 if e["resource_calendar_id"] else 1,
                                   e["id"]))
    return ranked[0]


# --- data --------------------------------------------------------------------

def fetch_lines(kw, emp_ids, date_from, date_to):
    """Raw timesheet lines.

    `create_date` rides along: the delay between the day worked and the day it
    was booked is the only honest measure of "up to date" here, since not a
    single line in this database is `validated`. `name` rides along for
    `LEAVE_RX`, which reads it to spot leave booked outside the Congés project —
    to report it, never to reclassify on it."""
    if not emp_ids:
        return []
    return kw("account.analytic.line", "search_read",
              [[["employee_id", "in", emp_ids],
                ["date", ">=", date_from.isoformat()],
                ["date", "<=", date_to.isoformat()]]],
              {"fields": ["employee_id", "date", "unit_amount", "project_id",
                          "task_id", "timesheet_invoice_type", "create_date",
                          "name"]})


def classify(line, leave_ids):
    """'leave' | 'billable' | 'invest' | 'internal' for one timesheet line.

    Leave wins over the invoice type: the "Congés" project's lines are typed
    `non_billable`, and leaving them in would make a fortnight off read as a
    billability collapse. Anything else falls back to internal, so an invoice
    type added to Odoo tomorrow lands on the conservative side of the ratio
    rather than silently inflating it; unknown types are counted and reported
    under **Notes**."""
    pid = line["project_id"][0] if line["project_id"] else 0
    if pid in leave_ids:
        return "leave"
    kind = line["timesheet_invoice_type"] or ""
    if kind in BILLABLE_TYPES:
        return "billable"
    if kind in INVEST_TYPES:
        return "invest"
    return "internal"


def grid_dates(schedules, today, weeks=GRID_WEEKS):
    """The calendar columns of the grid — the *same dates for every developer*.

    This is the whole point of the rewrite. The grid used to be "each
    developer's own last fifteen working days", which quietly gave every row a
    different date range: a four-day week reaches further back for the same
    count, so Nathan's leftmost cell was 29/07 while Adrien's was 04/08. The
    rows looked like a calendar, invited being read down the columns, and column
    three meant a different day on every line.

    Now the columns are calendar dates. A day the developer does not work is
    blank rather than absent, so the rows stay aligned and Nathan's Fridays read
    as "not his day" instead of silently shifting his week.

    Weekdays come from the union of the team's schedules rather than a hardcoded
    Monday–Friday, so a Saturday worker would get a Saturday column."""
    weekdays = {d for sched in schedules.values() for d in sched["hours"]} \
        or {0, 1, 2, 3, 4}
    # Today is excluded: nobody has finished booking the day they are in, and
    # counting it makes the whole team look one day behind every morning.
    end = today - datetime.timedelta(days=1)
    start = cap.monday(end) - datetime.timedelta(weeks=weeks - 1)
    out, cursor = [], start
    while cursor <= end:
        if cursor.weekday() in weekdays:
            out.append(cursor)
        cursor += datetime.timedelta(days=1)
    return out


def gather(kw, cfg, today, days=None, recent_days=RECENT_WINDOW, needle=None,
           period=None):
    """`period` — a `dev_periods` key running to today ("month", "quarter",
    "year", "90d"…) — replaces `days` when given; the web app's picker
    passes it. "year" is the default year-to-date window, recent one included."""
    devs, missing = resolve_devs(kw, cfg, needle)
    leave_ids = set(cfg.get("leave_project_ids") or [])
    if period == "year":
        period = None
    if period:
        window_from = periods.period_range(period, today)[0]
        window_label, window_de = periods.window_text(period, today)
    else:
        window_from, window_label, window_de = window_bounds(today, days)
    # Only the default year to date carries the recent window and the
    # year-end projection. (Not `not days`: "Ce mois" on the 1st is 0 days.)
    is_year = not (days or period)
    # The recent window rides alongside the year, never alongside an explicit
    # `--days`: two rolling windows on one table is a puzzle, not a report.
    # It is deliberately not clamped to 1 January — in February the current
    # picture legitimately reaches back into December.
    recent_from = (None if not is_year else
                   today - datetime.timedelta(days=recent_days))
    # One query covers whichever reaches furthest back — the billable window,
    # the recent one, or the fill grid. Early in January the grid is the widest.
    grid_from = cap.monday(today) - datetime.timedelta(weeks=GRID_WEEKS)
    date_from = min([window_from, grid_from] + ([recent_from] if recent_from else []))
    emp_ids = [eid for d in devs for eid in d["emp_ids"]]
    lines = fetch_lines(kw, emp_ids, date_from, today)

    owner = {eid: d["name"] for d in devs for eid in d["emp_ids"]}
    hours_by_emp = defaultdict(float)
    for line in lines:
        if line["employee_id"]:
            hours_by_emp[line["employee_id"][0]] += line["unit_amount"]

    employees = {d["name"]: primary_employee(d, hours_by_emp) for d in devs}
    schedules = cap.fetch_schedules(kw, cfg, employees)

    # Daily hours for the fill grid, and the booking delay per line.
    per_day = defaultdict(float)          # (name, date) -> h
    leave_day = defaultdict(float)        # (name, date) -> h on leave
    lag = defaultdict(list)               # name -> [days]
    kinds = defaultdict(float)            # (name, kind) -> h, window only
    buckets = defaultdict(float)          # (bucket, kind, name) -> h
    by_project = defaultdict(float)       # (project, kind) -> h
    by_dev_task = defaultdict(float)      # (name, project, task, kind) -> h, window
    by_dev_task_recent = defaultdict(float)  # same key, recent window
    task_labels = defaultdict(float)      # (name, project, task, label) -> h, window
    # Names are not unique — "Interne" exists once per company — so a row
    # keyed on labels can merge several projects; keep every id behind it.
    nb_ids = defaultdict(set)             # (name, project, task) -> {(pid, tid)}, 0 = none
    unknown = defaultdict(float)          # raw invoice type -> h
    internal_pids = set()                 # projects carrying non-billable hours
    misfiled = defaultdict(float)         # (name, project) -> h looking like leave
    misfiled_ids = defaultdict(set)       # (name, project) -> {project id}, 0 = none
    recent = defaultdict(float)           # (name, kind) -> h, recent window
    first_seen = {}                       # name -> earliest date with any line
    monthly = (today - window_from).days > TREND_MAX_WEEKS * 7

    for line in lines:
        if not line["employee_id"]:
            continue
        name = owner.get(line["employee_id"][0])
        if name is None:
            continue
        date = datetime.date.fromisoformat(line["date"])
        hours = line["unit_amount"]
        kind = classify(line, leave_ids)
        per_day[(name, date)] += hours
        if name not in first_seen or date < first_seen[name]:
            first_seen[name] = date
        if kind == "leave":
            leave_day[(name, date)] += hours
        if date <= today:
            booked = datetime.datetime.fromisoformat(line["create_date"]).date()
            lag[name].append((booked - date).days)
        if recent_from and date >= recent_from:
            recent[(name, kind)] += hours
        if kind in ("internal", "invest"):
            nb_key = (name,
                      line["project_id"][1] if line["project_id"] else "(sans projet)",
                      line["task_id"][1] if line["task_id"] else "(sans tâche)",
                      kind)
            if recent_from and date >= recent_from:
                by_dev_task_recent[nb_key] += hours
            nb_ids[nb_key[:3]].add((line["project_id"][0] if line["project_id"] else 0,
                                    line["task_id"][0] if line["task_id"] else 0))
            if date >= window_from:
                by_dev_task[nb_key] += hours
                label = (line["name"] or "").strip().lower() or "/"
                task_labels[nb_key[:3] + (label,)] += hours
        if date >= window_from:
            kinds[(name, kind)] += hours
            buckets[(bucket_of(date, monthly), kind, name)] += hours
            if kind == "internal" and LEAVE_RX.search(line["name"] or ""):
                label = line["project_id"][1] if line["project_id"] else "(sans projet)"
                misfiled[(name, label)] += hours
                misfiled_ids[(name, label)].add(line["project_id"][0]
                                                if line["project_id"] else 0)
            if kind in ("internal", "invest"):
                label = line["project_id"][1] if line["project_id"] else "(sans projet)"
                by_project[(label, kind)] += hours
                if line["project_id"]:
                    internal_pids.add(line["project_id"][0])
            raw = line["timesheet_invoice_type"] or "(vide)"
            if kind != "leave" and raw not in KNOWN_TYPES:
                unknown[raw] += hours

    # A developer's own first timesheet line, not their `hr.employee` record's
    # creation date: HR can (and does) set the record up ahead of the actual
    # start, and a record created early would then clear a joiner of weeks
    # they were genuinely expected to timesheet but had not. The absence of a
    # line before this date is not evidence either way — it is simply outside
    # what a read-only query can see — so `date_from` (which already reaches
    # back to `window_from`, 1 January by default) has to cover the whole
    # window for this to find a real first day rather than an artifact of
    # where the query happened to start.
    joins = {d["name"]: first_seen.get(d["name"]) for d in devs}

    # A client project whose hours are typed non-billable is a setup mistake,
    # not a business decision: someone forgot the sales order line, and the hours
    # are lost quietly. Only projects with a partner are worth flagging — the
    # "Interne" projects are non-billable by design.
    leaking = {}
    if internal_pids:
        for proj in kw("project.project", "read", [sorted(internal_pids)],
                       {"fields": ["name", "partner_id"]}):
            if proj["partner_id"] and proj["id"] not in leave_ids:
                leaking[proj["name"]] = proj["partner_id"][1]

    return {"devs": devs, "missing": missing, "schedules": schedules,
            "grid_dates": grid_dates(schedules, today), "leaking": leaking,
            "employees": employees, "joins": joins,
            "per_day": per_day, "leave_day": leave_day,
            "lag": lag, "kinds": kinds, "buckets": buckets, "monthly": monthly,
            "by_project": by_project, "unknown": unknown, "today": today,
            "by_dev_task": by_dev_task, "by_dev_task_recent": by_dev_task_recent,
            "task_labels": task_labels, "nb_ids": nb_ids,
            "odoo_url": cfg["url"],
            "misfiled": misfiled, "misfiled_ids": misfiled_ids, "recent": recent, "recent_from": recent_from,
            "recent_days": recent_days, "has_recent": bool(recent_from),
            "window_label": window_label, "window_de": window_de,
            "window_from": window_from,
            "is_year": is_year,
            "n_lines": len(lines), "hours_by_emp": hours_by_emp}


def bucket_of(date, monthly):
    return f"{date:%Y-%m}" if monthly else cap.week_of(date)


def bucket_label(key, monthly):
    if not monthly:
        return cap.wlabel(key)
    year, month = key.split("-")
    return f"{MOIS[int(month) - 1]} {year[2:]}"


def year_days(ctx, name):
    """Weekdays this developer is expected to have timesheeted over the
    report's whole window (the calendar year to date by default, or whatever
    `--days` asked for) — same idea as `grid_dates`, but not clamped to
    `GRID_WEEKS`, and starting at this developer's own first timesheet line
    (`ctx["joins"]`, built in `gather()`) rather than at `window_from` when
    that first line falls inside the window. That is what keeps a mid-year
    hire from reading as months of missing timesheets: before their first
    line, nothing here is counted as a hole, because there was nothing this
    report could have expected of them yet."""
    join = ctx["joins"].get(name)
    start = max(ctx["window_from"], join) if join else ctx["window_from"]
    end = ctx["today"] - datetime.timedelta(days=1)
    weekdays = set(ctx["schedules"][name]["hours"])
    out, cursor = [], start
    while cursor <= end:
        if cursor.weekday() in weekdays:
            out.append(cursor)
        cursor += datetime.timedelta(days=1)
    return out


def fill_state(ctx, name, cfg, days=None):
    """Per-developer completeness: the grid, the gaps, and how late they are.

    `days` defaults to the recent glance grid (`ctx["grid_dates"]`, four
    weeks); passing `year_days(ctx, name)` instead runs the exact same
    day-by-day rule — half the calendar hours counts as filled, leave netted
    out of both sides — over the full reporting window instead, which is what
    `section_tenue` uses for the year-to-date coverage figure."""
    schedule = ctx["schedules"][name]
    gap = cfg.get("gap_ratio", 0.5)
    days = ctx["grid_dates"] if days is None else days
    expected_map = schedule["hours"]
    # A blank week separator every Monday: three groups of five read as three
    # weeks at a glance ("the last two days of last week are missing"), where an
    # unbroken run of fifteen has to be counted on a finger.
    glyphs, missing_days, last_filled, mine = [], [], None, []
    net_worked = net_expected = 0.0
    for i, day in enumerate(days):
        if i and day.weekday() == min(d.weekday() for d in days):
            glyphs.append(" ")
        expected = expected_map.get(day.weekday(), 0.0)
        worked = ctx["per_day"].get((name, day), 0.0)
        on_leave = ctx["leave_day"].get((name, day), 0.0)
        if expected <= 0.05:
            glyphs.append(OFF)          # not this developer's working day
            continue
        mine.append(day)
        # Coverage nets the leave out of *both* sides, hour for hour rather than
        # day for day. A fortnight off must not read as a coverage collapse, and
        # a half-day off followed by a half-day worked is a full day covered —
        # dropping the whole day would throw away the half that was worked.
        net_expected += max(expected - on_leave, 0.0)
        net_worked += max(worked - on_leave, 0.0)
        if on_leave >= gap * expected:
            glyphs.append(LEAVE)
            last_filled = day
        elif worked >= gap * expected:
            glyphs.append(FULL)
            last_filled = day
        else:
            glyphs.append(EMPTY)
            missing_days.append(day)
    recent = set(mine[-RECENT_DAYS:])
    missing_recent = sum(1 for d in missing_days if d in recent)
    late = sum(1 for d in mine if last_filled and d > last_filled)
    lags = ctx["lag"].get(name) or []
    return {"days": days, "mine": mine, "grid": "".join(glyphs),
            "missing": missing_days,
            "worked": net_worked, "expected": net_expected,
            "coverage": ratio(net_worked, net_expected),
            "missing_recent": missing_recent, "last_filled": last_filled,
            "late": late, "lag": statistics.median(lags) if lags else None,
            # Only a red timesheet voids the billable ratio. A single missing
            # day out of fifteen says someone was travelling on Monday; it does
            # not make ninety days of history unreadable, and gating on it threw
            # four developers out of six and left the team figure resting on two
            # people. Three gaps in the last five days is a different animal:
            # Michael's August is 12h logged out of a month, and no percentage
            # computed on it means anything.
            "trusted": missing_recent < 3}


# --- sections ----------------------------------------------------------------

def section_tenue(ctx, cfg, states):
    """A — is the timesheet actually being kept?"""
    days = ctx["grid_dates"]
    span = (f"{date_fr(days[0])} → {date_fr(days[-1])}" if days else "—")
    weeks = sorted({cap.week_of(d) for d in days})
    year_days_by = {d["name"]: year_days(ctx, d["name"]) for d in ctx["devs"]}
    year_states = {d["name"]: fill_state(ctx, d["name"], cfg,
                                         days=year_days_by[d["name"]])
                  for d in ctx["devs"]}
    print(f"## Tenue des feuilles — {ctx['window_label']} · grille des "
          f"{GRID_WEEKS} dernières semaines ({span})\n")
    print("_**Une colonne = un jour, et la même date sur toutes les lignes.** "
          "Les blocs sont les semaines, du lundi au vendredi : `██·██` en "
          f"troisième position, c'est le mercredi de {cap.wlabel(weeks[2]) if len(weeks) > 2 else 'la 3e semaine'} "
          "qui manque. Un jour compte comme pointé dès la moitié des heures "
          f"attendues ce jour-là ({cfg.get('gap_ratio', 0.5) * 100:.0f} %), le "
          "seuil que `/dev-capacity` utilise déjà pour son drapeau « pointage "
          "incomplet » — les deux pages ne peuvent donc pas désigner deux "
          "retardataires différents._\n")
    print("_**`Trous` et `Couverture` ne mesurent pas la même chose, et c'est "
          "voulu.** Un trou est une journée qui n'est pas renseignée du tout : "
          "c'est une question de **tenue**. La couverture est la part des heures "
          "attendues qui est effectivement pointée : c'est une question de "
          "**volume**. Quelqu'un qui pointe 6 h chaque jour sur un calendrier de "
          "8 h n'a aucun trou — il tient sa feuille, tous les jours, le jour "
          "même — et pourtant un quart de son temps n'est nulle part. Mettre ces "
          "6 h dans la colonne des trous en ferait un retardataire, ce qu'il "
          "n'est pas ; les taire laisserait 2 h par jour disparaître sans "
          "trace._\n")
    print("_**Les jours de repos du développeur sont vides, pas manquants** : "
          "le vendredi de Nathan est un blanc dans sa ligne, jamais un trou, et "
          "sa semaine reste alignée sur celle des autres. **Aujourd'hui est "
          "exclu** — personne n'a fini de pointer sa journée en cours, et l'y "
          "inclure mettrait toute l'équipe en retard tous les matins._\n")
    print(f"_**La grille et ses `Trous`/`Couverture` ne portent que sur les "
          f"{GRID_WEEKS} dernières semaines** — au-delà, un pavé de blocs ne se "
          f"lit plus, c'est un journal, pas un coup d'œil. Les colonnes "
          f"`Trous ({ctx['window_label']})` et `Couverture ({ctx['window_label']})` "
          f"répondent à la vraie question — l'objectif est-il tenu depuis le "
          f"début de l'année — avec la même règle (moitié des heures "
          f"attendues, congés nets des deux côtés), juste sur toute la "
          f"fenêtre. **La fenêtre de chacun démarre à sa toute première ligne "
          f"de pointage si elle est postérieure au début de l'année** — un "
          f"développeur arrivé en mai n'est pas jugé sur janvier-avril ; voir "
          f"**Notes** pour qui est concerné._\n")
    # The key sits above the table, unfolded. The same key is in the legend
    # below, but a reader meeting `███·█ █████` for the first time should not
    # have to open a <details> to find out whether the dots are good or bad.
    print(f"_Lecture : `{FULL}` jour pointé · `{LEAVE}` congé ou férié · "
          f"`{EMPTY}` **trou** · blanc = jour non travaillé._\n")
    cap.legend("Tenue", [
        (f"`{FULL}`", "journée pointée — au moins la moitié des heures attendues."),
        (f"`{LEAVE}`", "congé ou férié, pointé sur le projet de congés. Compte comme "
                       "tenu : la journée est renseignée."),
        (f"`{EMPTY}`", "**trou** — moins de la moitié des heures attendues, sans "
                       "congé qui l'explique. Le seuil est à la moitié, pas à la "
                       "journée entière : à 90 % on transformerait en "
                       "retardataire quelqu'un qui pointe 6 h tous les jours, "
                       "sans rien manquer."),
        ("blanc", "jour que ce développeur ne travaille pas (son régime), ou jour "
                  "à venir. Ni tenu ni manquant : sans objet."),
        ("espace entre les blocs", "changement de semaine. Un bloc = une semaine, "
                                   "du lundi au vendredi."),
        ("🟢", f"à jour — aucun trou sur les {RECENT_DAYS} derniers jours **ouvrés "
               "pour lui**."),
        ("🟡", f"un ou deux trous sur ces {RECENT_DAYS} jours."),
        ("🔴", f"trois trous ou plus sur ces {RECENT_DAYS} jours — les chiffres de "
               "facturabilité de ce développeur ne veulent plus dire grand-chose, "
               "et il est sorti du total d'équipe plus bas."),
        ("`Trous`", "nombre de trous sur toute la grille, pas seulement sur les "
                    "derniers jours. Une question de **tenue**."),
        ("`Couverture`", "heures pointées ÷ heures attendues sur la grille, congés "
                         "retirés des deux côtés. Une question de **volume** : "
                         "`105.0/136h 77%` = un quart des heures attendues n'est "
                         "pointé nulle part. C'est la même mesure que "
                         "l'`Occupation` de l'onglet Capacité, sur la fenêtre de "
                         "cette grille plutôt que semaine par semaine."),
        ("`> 100%`", "plus d'heures pointées que d'heures au calendrier : des "
                     "heures supplémentaires, ou un calendrier qui ne décrit pas "
                     "le vrai régime — voir **Qualité des données** dans l'onglet "
                     "Capacité."),
        ("`Retard`", "jours ouvrés écoulés depuis le dernier jour pointé."),
        ("`Délai méd.`", "médiane de l'écart entre le jour travaillé et le jour où la "
                         "ligne a été créée. `0 j` = pointage le jour même ; un délai "
                         "qui grandit annonce les trous avant qu'ils n'apparaissent."),
        (f"`Trous ({ctx['window_label']})`", "même définition que `Trous`, sur toute la "
                                             "fenêtre plutôt que sur la grille des "
                                             f"{GRID_WEEKS} dernières semaines — et sur la "
                                             "fenêtre propre à ce développeur s'il a "
                                             "rejoint TSC après le début de la période."),
        (f"`Couverture ({ctx['window_label']})`", "même définition que `Couverture`, sur "
                                                   "cette fenêtre plus large : c'est le "
                                                   "chiffre qui répond « l'objectif de "
                                                   "8h/jour est-il tenu depuis le début de "
                                                   "l'année », pas seulement ces quatre "
                                                   "dernières semaines."),
    ])
    # The week labels head the column, so each block of the grid is named.
    grid_head = "Grille — " + " · ".join(cap.wlabel(w) for w in weeks)
    header = ["Développeur", "Régime", grid_head, "Trous", "Couverture",
              f"Trous ({ctx['window_label']})", f"Couverture ({ctx['window_label']})",
              "Dernier jour", "Retard", "Délai méd.", "État"]
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(["---"] * len(header)) + "|")
    for dev in ctx["devs"]:
        name = dev["name"]
        state, sched = states[name], ctx["schedules"][name]
        ystate = year_states[name]
        days_n = len(sched["hours"])
        regime = (f"{days_n} j · {cap.gross(sched):.0f}h" if days_n
                  else "régime inconnu")
        last = f"{state['last_filled']:%d/%m}" if state["last_filled"] else "—"
        lag = "—" if state["lag"] is None else f"{state['lag']:.0f} j"
        cover = ("—" if state["coverage"] is None else
                 f"{state['worked']:.1f}/{state['expected']:.0f}h "
                 f"{state['coverage']:.1f}%")
        ycover = ("—" if ystate["coverage"] is None else
                  f"{ystate['worked']:.1f}/{ystate['expected']:.0f}h "
                  f"{ystate['coverage']:.1f}%")
        print(f"| {cap.cell(name)} | {regime} | `{state['grid']}` | "
              f"{len(state['missing']) or '—'} | {cover} | "
              f"{len(ystate['missing']) or '—'} | {ycover} | {last} | "
              f"{state['late'] or '—'} | {lag} | "
              f"{fill_badge(state['missing_recent'])} |")
    print()
    trusted_year = [d["name"] for d in ctx["devs"] if states[d["name"]]["trusted"]]
    tw = sum(year_states[n]["worked"] for n in trusted_year)
    te = sum(year_states[n]["expected"] for n in trusted_year)
    tcov = ratio(tw, te)
    if tcov is not None:
        verdict = ("au-dessus du régime attendu" if tcov >= 100
                   else f"il manque **{hd(te - tw)}**")
        print(f"> **Couverture — {ctx['window_label']} : {tcov:.1f} %** sur "
              f"{hd(te)} attendues ({hd(tw)} pointées) — {verdict}. Même "
              f"population que le total d'équipe de la section suivante : "
              f"les feuilles non fiables sur les derniers jours sont "
              f"affichées ci-dessus mais sorties de ce total.\n")
    holes = [(d["name"], states[d["name"]]) for d in ctx["devs"]
             if states[d["name"]]["missing"]]
    if holes:
        print("**Jours à rattraper**\n")
        for name, state in holes:
            dates = ", ".join(date_fr(d) for d in state["missing"])
            print(f"- **{cap.cell(name)}** — {dates}")
        print()


# Below this many hours a recent-window percentage is arithmetic on noise — one
# invoiced afternoon moves it ten points — and it is printed as "—" instead.
RECENT_MIN_HOURS = 20.0
# A movement smaller than this is not a movement, it is the same quarter seen
# twice. Only past it does an arrow appear.
DELTA_FLOOR = 3.0


def movement(year, recent):
    """↘ / ↗ / → for the gap between the year and the recent window.

    This column, not either percentage, is what answers "who do I sit down
    with". A developer at 80 % for the year and 60 % lately has a problem the
    yearly figure has not caught up with; one at 60 % for the year and 85 %
    lately has already fixed it, and coaching them would be reading a stale
    number back to someone who moved on."""
    if year is None or recent is None:
        return "—", None
    delta = recent - year
    if abs(delta) < DELTA_FLOOR:
        return "→", delta
    return ("↗ +%.0f" % delta if delta > 0 else "↘ %.0f" % delta), delta


def section_facturable(ctx, cfg, states):
    """B — billable share: the year for the target, the recent window for today."""
    dual = ctx["has_recent"]
    rd = ctx["recent_days"]
    main_col = "% année" if ctx["is_year"] else "% période"
    print(f"## Facturable vs non facturable — {ctx['window_label']} "
          f"({ctx['window_from']:%d/%m/%Y} → {ctx['today']:%d/%m/%Y})"
          + (f" · {rd} derniers jours" if dual else "") + "\n")
    if dual:
        print(f"_**Deux fenêtres, parce qu'il y a deux questions.** L'année dit "
              f"si l'objectif de **{TARGET:.0f} %** sera tenu — c'est une cible "
              f"annuelle, le chiffre repart donc de zéro au 1er janvier. Les "
              f"**{rd} derniers jours** disent où on en est *maintenant*, et "
              f"c'est ce qui permet de décider avec qui travailler de plus "
              f"près. **La colonne qui décide est `Mouv.`** : un développeur à "
              f"80 % sur l'année mais 60 % ces {rd} jours a un problème que la "
              f"moyenne annuelle n'a pas encore vu ; l'inverse a déjà redressé "
              f"la barre, et lui parler reviendrait à lui relire un chiffre "
              f"périmé. `--days N` remplace les deux par une seule fenêtre "
              f"glissante._\n")
    else:
        print(f"_Objectif d'équipe : **{TARGET:.0f} % facturable sur l'année**. "
              "Une seule fenêtre est affichée : `--days` a été demandé "
              "explicitement. Sans lui, la page montre l'année civile et les "
              f"{RECENT_WINDOW} derniers jours côte à côte._\n")
    print("_Le facturable se lit sur `timesheet_invoice_type`, rempli sur 100 % "
          "des lignes de cette base — ni sur `allow_billable`, qui marque aussi "
          "`Interne`, ni sur le client du projet, qui ne distingue pas "
          "l'investissement interne du temps perdu. **Les congés sortent du "
          "dénominateur** : ils ne sont pas du temps non facturé, ils ne sont "
          "pas du temps de travail._\n")
    print("_**Deux ratios aussi, jamais un seul.** Le ratio strict compte "
          "l'investissement interne (produit TSC, R&D) contre l'équipe ; « hors "
          "invest » le met de côté. Investir dans notre propre produit est une "
          "décision, pas du mou — un chiffre unique la cacherait ou la "
          "punirait. **L'écart entre les deux colonnes est le prix de cette "
          "décision**, et c'est lui qui mérite la discussion._\n")
    legend_rows = [
        ("`Facturable`", "`billable_time` (au temps passé), `billable_fixed` (forfait) "
                         "et `billable_manual` — l'heure est vendue."),
        ("`Interne`", "`non_billable` hors congés : réunions internes, "
                      "administratif, avant-vente."),
        ("`Invest`", "`non_billable_invest` — investissement délibéré dans nos propres "
                     "produits (TSC - Odoo, TSC - Store)."),
        (f"`{main_col}`", "facturable ÷ (facturable + interne + invest). Les "
                          "colonnes d'heures portent sur cette fenêtre-là."),
        ("`% hors inv.`", "facturable ÷ (facturable + interne) — l'invest sort des "
                          "deux côtés. Sur l'année."),
    ]
    if dual:
        legend_rows += [
            (f"`% {rd} j`", f"le même ratio strict, sur les {rd} derniers jours "
                            "seulement — la situation du moment, pas la note de "
                            "l'année."),
            ("`Mouv.`", f"`% {rd} j` moins `% année`, en points. `↘` recule, `↗` "
                        f"progresse, `→` stable (moins de {DELTA_FLOOR:.0f} points). "
                        "**C'est la colonne qui dit avec qui travailler de plus "
                        "près.**"),
            ("`—` (dans les colonnes récentes)", f"moins de {RECENT_MIN_HOURS:.0f} h "
                                                 "pointées sur la fenêtre : un "
                                                 "pourcentage y serait du bruit "
                                                 "arrondi, une après-midi facturée "
                                                 "le déplacerait de dix points."),
        ]
    legend_rows += [
        ("🟢", f"à l'objectif de {TARGET:.0f} % ou au-dessus."),
        ("🟡", f"entre {AMBER_BELOW:.0f} % et {TARGET:.0f} %."),
        ("🔴", f"sous {AMBER_BELOW:.0f} %."),
        ("`▏` (dans la barre)", f"la marque de l'objectif, à {TARGET:.0f} %."),
        ("⚠️", "feuille de temps incomplète (voir la section précédente) : le "
               "pourcentage est affiché mais **exclu du total d'équipe** — un ratio "
               "calculé sur des jours manquants mesure qui pense à cliquer, pas "
               "comment l'équipe vend son temps."),
    ]
    cap.legend("Facturable", legend_rows)

    header = ["Développeur", "Facturable", "Interne", "Invest", "Total travaillé",
              "Congés", main_col, "% hors inv."]
    if dual:
        header += [f"% {rd} j", "Mouv."]
    header += ["Barre (%s)" % (f"{rd} j" if dual else main_col[2:]), ""]
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(["---"] * len(header)) + "|")

    goal_set = target_names(cfg)
    totals, rtotals = defaultdict(float), defaultdict(float)
    gtotals, grtotals = defaultdict(float), defaultdict(float)
    watch = []
    for dev in ctx["devs"]:
        name = dev["name"]
        bil = ctx["kinds"].get((name, "billable"), 0.0)
        inv = ctx["kinds"].get((name, "invest"), 0.0)
        internal = ctx["kinds"].get((name, "internal"), 0.0)
        leave = ctx["kinds"].get((name, "leave"), 0.0)
        worked = bil + inv + internal
        strict, ex_inv = ratio(bil, worked), ratio(bil, bil + internal)
        trusted = states[name]["trusted"]
        goal = name in goal_set

        rbil = ctx["recent"].get((name, "billable"), 0.0)
        rworked = rbil + ctx["recent"].get((name, "invest"), 0.0) \
            + ctx["recent"].get((name, "internal"), 0.0)
        rstrict = ratio(rbil, rworked) if rworked >= RECENT_MIN_HOURS else None
        arrow, delta = movement(strict, rstrict)

        if trusted:
            for key, value in (("billable", bil), ("invest", inv),
                               ("internal", internal), ("leave", leave)):
                totals[key] += value
            rtotals["billable"] += rbil
            rtotals["worked"] += rworked
        if trusted and goal:
            for key, value in (("billable", bil), ("invest", inv),
                               ("internal", internal), ("leave", leave)):
                gtotals[key] += value
            grtotals["billable"] += rbil
            grtotals["worked"] += rworked
            # Who to work with more closely: below target *now*, and going the
            # wrong way. Someone below target but climbing is already on it.
            if rstrict is not None and rstrict < TARGET and (delta or 0) <= -DELTA_FLOOR:
                watch.append((delta, name, strict, rstrict))

        flag = ("" if trusted else " ⚠️") + (" 🎯" if goal else "")
        cells = [f"{cap.cell(name)}{flag}", hd(bil), hd(internal), hd(inv),
                 hd(worked), hd(leave), pct_txt(strict), pct_txt(ex_inv)]
        if dual:
            cells += [pct_txt(rstrict), arrow]
        shown = rstrict if dual else strict
        cells += [bar(shown), target_badge(shown) if goal else "hors objectif"]
        print("| " + " | ".join(cells) + " |")

    def total_row(label, tot, rtot, judged):
        b, i, n = tot["billable"], tot["invest"], tot["internal"]
        w = b + i + n
        strict, ex = ratio(b, w), ratio(b, b + n)
        recent = (ratio(rtot["billable"], rtot["worked"])
                  if rtot["worked"] >= RECENT_MIN_HOURS else None)
        arrow, _d = movement(strict, recent)
        cells = [f"**{label}**", f"**{hd(b)}**", f"**{hd(n)}**", f"**{hd(i)}**",
                 f"**{hd(w)}**", f"**{hd(tot['leave'])}**",
                 f"**{pct_txt(strict)}**", f"**{pct_txt(ex)}**"]
        if dual:
            cells += [f"**{pct_txt(recent)}**", f"**{arrow}**"]
        shown = recent if dual else strict
        cells += [bar(shown), target_badge(shown) if judged else "info"]
        print("| " + " | ".join(cells) + " |")
        return b, i, n, w, strict, ex, recent

    goal_devs = [d["name"] for d in ctx["devs"] if d["name"] in goal_set]
    has_goal = bool(goal_devs)
    if has_goal:
        tb, ti, tn, tworked, t_strict, t_ex, r_strict = total_row(
            "🎯 Objectif (" + ", ".join(n.split()[0] for n in goal_devs) + ")",
            gtotals, grtotals, True)
    if len(ctx["devs"]) > len(goal_devs) or not has_goal:
        team = total_row("Équipe entière", totals, rtotals, False)
        if not has_goal:
            tb, ti, tn, tworked, t_strict, t_ex, r_strict = team
    print()
    if goal_set:
        others = [d["name"] for d in ctx["devs"] if d["name"] not in goal_set]
        print(f"_🎯 L'objectif de {TARGET:.0f} % ne s'applique qu'aux employés "
              f"belges ({', '.join(sorted(goal_set))}) — c'est sur lui que se "
              f"calcule le bonus de société. "
              + (f"{', '.join(others)} : chiffres affichés pour information, "
                 f"hors objectif et hors de la ligne 🎯._\n" if others else "_\n"))

    excluded = [d["name"] for d in ctx["devs"] if not states[d["name"]]["trusted"]]
    if t_strict is not None and has_goal:
        delta = t_strict - TARGET
        verdict = ("au-dessus de l'objectif" if delta >= 0
                   else f"il manque **{-delta:.1f} points**")
        # "l'équipe" is a lie when the run is filtered to one developer.
        subject = (cap.cell(goal_devs[0]) if len(goal_devs) == 1
                   else "le périmètre de l'objectif")
        cost = (f" L'investissement interne pèse **{t_ex - t_strict:.1f} points** "
                f"({hd(ti)}) : hors invest, {subject} est à **{t_ex:.0f} %**."
                if t_ex is not None and ti > 0.05 else "")
        mis = sum(h for (n, _p), h in ctx["misfiled"].items()
                  if states.get(n, {}).get("trusted") and n in goal_set)
        hint = ""
        if tworked > 0.05 and mis / tworked >= MISFILED_FLOOR:
            fixed = ratio(tb, tworked - mis)
            hint = (f" ⚠️ **{hd(mis)} de ce dénominateur ressemblent à des "
                    f"congés pointés hors du projet Congés** : corrigés, le "
                    f"ratio serait de **{fixed:.1f} %** — voir plus bas.")
        print(f"> **Objectif — {ctx['window_label']} : {t_strict:.1f} % "
              f"facturable** sur {hd(tworked)} travaillées, pour un objectif de "
              f"{TARGET:.0f} % — {verdict}.{cost}{hint}\n")
    if dual and r_strict is not None and has_goal:
        way = ("mieux que" if r_strict > (t_strict or 0) + DELTA_FLOOR else
               "moins bien que" if r_strict < (t_strict or 0) - DELTA_FLOOR
               else "dans la ligne de")
        who = (cap.cell(goal_devs[0]) if len(goal_devs) == 1
               else "le périmètre de l'objectif")
        print(f"> **Maintenant — {rd} derniers jours : {r_strict:.1f} %**, soit "
              f"{way} la moyenne {ctx['window_de']}. C'est ce chiffre-là "
              f"qui décrit {who} aujourd'hui ; celui du dessus décrit "
              f"l'année qu'on ne peut plus refaire.\n")
    if watch:
        watch.sort()
        who = ", ".join(f"**{cap.cell(n)}** ({r:.0f} % sur {rd} j, {d:+.0f} pts)"
                        for d, n, _y, r in watch)
        print(f"> 🎯 **À suivre de près** — sous l'objectif sur les {rd} derniers "
              f"jours *et* en recul par rapport à leur propre moyenne "
              f"{ctx['window_de']} : {who}. Un développeur sous l'objectif "
              f"mais en progression n'est pas dans cette liste : il a déjà "
              f"corrigé.\n")
    if excluded:
        print(f"> ⚠️ Exclus du total d'équipe, feuille incomplète : "
              f"{', '.join(excluded)}. Leurs lignes ci-dessus sont affichées, "
              f"pas comptées.\n")


def section_tendance(ctx, cfg, states, fold=False):
    """B bis — the shape of the year, and whether it can still be recovered.

    The cumulative column is the one that answers the yearly target. A month at
    60 % in February is not the same fact as a month at 60 % in November: the
    first can still be averaged away, the second cannot. A row-by-row trend
    alone never says which of the two you are looking at."""
    monthly = ctx["monthly"]
    # The same population as the 🎯 row above it. A trend that quietly counts
    # a developer the target does not apply to prints a second, different
    # answer to the one question the page exists to answer. Only when the run
    # holds nobody the target applies to does it fall back to everyone shown —
    # and then it says so.
    goal_set = target_names(cfg)
    counted = {d["name"] for d in ctx["devs"]
               if states[d["name"]]["trusted"] and d["name"] in goal_set}
    if not counted:
        return
    keys = sorted({b for (b, _k, n) in ctx["buckets"] if n in counted})
    if not monthly:
        keys = keys[-TREND_MAX_WEEKS:]
    if len(keys) < 2:
        return
    unit = "mois" if monthly else "semaines"
    with md_to_html.folded(f"Tendance — {len(keys)} {unit}", fold):
        print("_Le ratio strict du périmètre de l'objectif (employés belges : "
              f"{', '.join(sorted(counted))}), congés exclus. **`Cumulé` est "
              "la colonne qui compte** : c'est le ratio depuis le début de la "
              "fenêtre, donc la note de l'année à ce jour — un mois faible en "
              "février se rattrape encore, le même en novembre ne se rattrape plus. "
              f"La dernière ligne est en cours et incomplète par construction. Une "
              "période très légère (moins de 40 h pour toute l'équipe) est affichée "
              "sans jugement : c'est une période de congés collectifs, pas une "
              "chute de la facturabilité._\n")
        print(f"| {'Mois' if monthly else 'Semaine'} | Facturable | Travaillé | "
              f"% période | | Cumulé | | Barre (cumulé) |")
        print("|---|---|---|---|---|---|---|---|")
        run_bil = run_worked = 0.0
        for i, key in enumerate(keys):
            bil = sum(ctx["buckets"].get((key, "billable", n), 0.0) for n in counted)
            worked = bil + sum(ctx["buckets"].get((key, k, n), 0.0)
                               for k in ("invest", "internal") for n in counted)
            run_bil += bil
            run_worked += worked
            period = ratio(bil, worked)
            cumul = ratio(run_bil, run_worked)
            light = " ·" if worked < 40.0 else ""
            star = "*" if i == len(keys) - 1 else ""
            print(f"| {bucket_label(key, monthly)}{star} | {hd(bil)} | "
                  f"{hd(worked)}{light} | {pct_txt(period)} | "
                  f"{target_badge(period) if not light else '—'} | "
                  f"**{pct_txt(cumul)}** | {target_badge(cumul)} | {bar(cumul)} |")
        print()
        if run_worked > 0.05:
            print(projection(ctx, run_bil, run_worked))


def projection_numbers(ctx, run_bil, run_worked):
    """The arithmetic behind `projection`, for a reader that formats it
    itself (the web app). Keys: cumul, debt (billable hours short of target,
    0 when met), ahead (hours left to work until 31/12 at the same daily
    volume, None outside a calendar-year window) and needed (the billable rate
    those hours would need, None when not computable, > 100 when out of reach)."""
    cumul = ratio(run_bil, run_worked)
    out = {"cumul": cumul, "debt": max(TARGET / 100.0 * run_worked - run_bil, 0.0),
           "ahead": None, "needed": None}
    if cumul is None or cumul >= TARGET or not ctx["is_year"]:
        return out
    today, year_end = ctx["today"], datetime.date(ctx["today"].year, 12, 31)
    elapsed = (today - ctx["window_from"]).days + 1
    left = (year_end - today).days
    if left <= 0 or elapsed <= 0:
        return out
    out["ahead"] = run_worked / elapsed * left
    out["needed"] = ratio(TARGET / 100.0 * (run_worked + out["ahead"]) - run_bil,
                          out["ahead"])
    return out


def projection(ctx, run_bil, run_worked):
    """Where the year lands, and what the rest of it would have to run at.

    The one line a yearly target actually needs. It projects the remaining
    months at the volume of hours worked so far — stated, not hidden — and
    solves for the billable rate that would still land on target. When that rate
    comes out above 100 % the target is arithmetically gone, and saying so is
    worth more than printing a number nobody can hit. Only for the calendar-year
    window: projecting a rolling ninety days to December means nothing."""
    cumul = ratio(run_bil, run_worked)
    head = f"> Cumul — {ctx['window_label']} : **{cumul:.1f} %**."
    if cumul >= TARGET:
        return f"{head} Objectif atteint — il reste à le tenir.\n"
    debt = TARGET / 100.0 * run_worked - run_bil
    head += (f" Le retard est de **{hd(debt)} facturables** : c'est ce qu'il "
             f"aurait fallu vendre en plus sur la période pour être à "
             f"{TARGET:.0f} %.")
    if not ctx["is_year"]:
        return head + "\n"
    today, year_end = ctx["today"], datetime.date(ctx["today"].year, 12, 31)
    elapsed = (today - ctx["window_from"]).days + 1
    left = (year_end - today).days
    if left <= 0 or elapsed <= 0:
        return head + "\n"
    # Same daily volume as the year so far: not a forecast of the business, an
    # arithmetic extension of it. Any other assumption would need a capacity
    # model this page has no business carrying — tab 2 has one.
    ahead = run_worked / elapsed * left
    needed = ratio(TARGET / 100.0 * (run_worked + ahead) - run_bil, ahead)
    tail = (f" À volume d'heures constant, il reste ~{hd(ahead)} à travailler "
            f"d'ici au 31/12")
    if needed is None or needed > 100.0:
        tail += (f" : **{TARGET:.0f} % sur l'année est hors d'atteinte** — il "
                 f"faudrait facturer {pct_txt(needed)} de tout ce qui reste. "
                 f"La question n'est plus d'atteindre la cible cette année, "
                 f"mais de savoir où le ratio se stabilise et ce qu'on vise "
                 f"pour la suivante.")
    else:
        tail += (f", à facturer à **{needed:.0f} %** pour finir l'année à "
                 f"{TARGET:.0f} %.")
    return head + tail + "\n"


def section_non_facturable(ctx, fold=False):
    """B ter — where the non-billable hours actually go."""
    rows = sorted(ctx["by_project"].items(), key=lambda kv: -kv[1])
    if not rows:
        return
    total = sum(ctx["by_project"].values())
    with md_to_html.folded("Où part le non facturable", fold):
        print("_Les heures internes et d'investissement, par projet, sur la même "
              "fenêtre. Fermer l'écart avec l'objectif passe par cette liste : "
              "c'est la seule section de la page qui dise **quoi** arrêter, et non "
              "**de combien** on est loin._\n")
        print("| Projet | Nature | Heures | Part du non facturable |")
        print("|---|---|---|---|")
        for (project, kind), hours in rows[:15]:
            nature = "Invest" if kind == "invest" else "Interne"
            share = hours / total * 100.0 if total else 0.0
            print(f"| {cap.cell(project)} | {nature} | {hd(hours)} | {share:.0f}% |")
        if len(rows) > 15:
            rest = sum(h for _k, h in rows[15:])
            print(f"| _{len(rows) - 15} autres_ | | {hd(rest)} | "
                  f"{rest / total * 100.0 if total else 0:.0f}% |")
        print()
        section_fuites(ctx)
        section_non_facturable_par_dev(ctx)


# Rows per developer before the rest folds into "N autres". Twelve covers the
# whole non-billable picture of every developer but one, and that one's tail is
# half-hours of point meetings.
NB_DEV_ROWS = 12
NB_LABELS = 3


def lines_url(ctx, dev, kind="nb", ids=None, pairs=None, project=None,
              leave=False, name=None):
    """The local `/lines` page listing every line behind a row (see
    `dev_timesheet_lines`). `dev` is a developer, or a list of them for a
    team-wide page (`name` then titles it).

    `ids` = (project_id, task_id), 0 meaning "none". `pairs` = several such
    tuples — a row whose label covers several projects, or an "N autres" row.
    `project` = one id or several, whatever the task. `leave=True` keeps only the lines whose label looks like
    leave (`LEAVE_RX`) — the "congés pointés hors du projet Congés" rows. With
    none of them, every non-billable line of the developer(s)."""
    devs = dev if isinstance(dev, list) else [dev]
    query = {"emp": ",".join(str(e) for d in devs for e in d["emp_ids"]),
             "name": name or devs[0]["name"],
             "from": ctx["window_from"].isoformat(),
             "to": ctx["today"].isoformat(), "kind": kind}
    if ids is not None:
        query["project"], query["task"] = ids
    elif project is not None:
        query["project"] = (",".join(str(p) for p in sorted(project))
                            if isinstance(project, (set, list, tuple)) else project)
    if pairs:
        query["pairs"] = ",".join(f"{p}:{t}" for p, t in pairs)
    if leave:
        query["leave"] = "1"
    return ("/lines?"
            + urlencode(query))


def section_non_facturable_par_dev(ctx):
    """The same question, one developer at a time, down to the task.

    The project table above says what the *team* should stop doing; it cannot
    say what to talk about with one person. A developer at 44 % billable whose
    non-billable hours sit on `Interne - FR` tells you nothing until the task
    and the labels are read: inter-contract, waiting for feedback and leave
    booked in the wrong place are three different conversations. Lines with no
    task are the usual case on the internal projects, so the most frequent
    labels ride along — they are the only thing that says what the hour was."""
    rd = ctx["recent_days"] if ctx["has_recent"] else None
    devs = [d["name"] for d in ctx["devs"]
            if any(k[0] == d["name"] for k in ctx["by_dev_task"])]
    if not devs:
        return
    print("**Par développeur — projet et tâche**\n")
    print("_Les heures internes et d'investissement de chacun, regroupées par "
          "projet et tâche. Sans tâche, la colonne `Libellés` est la seule "
          "trace de ce qui a été fait : ce sont les libellés les plus pointés "
          f"sur la ligne ({NB_LABELS} au plus, puis le reste regroupé), "
          f"sur {ctx['window_label']} — pas sur les {ctx['recent_days']} "
          "derniers jours. **Cliquer sur les heures ouvre toutes les lignes "
          "de la ligne du tableau** (libellé, date, heures), lues en direct "
          "dans Odoo par le serveur local ; le nom de la tâche ouvre la "
          "tâche dans Odoo. Les congés pointés hors du "
          "projet Congés apparaissent ici tels quels — voir la section "
          "suivante._\n")
    dev_by_name = {d["name"]: d for d in ctx["devs"]}
    for name in devs:
        dev = dev_by_name[name]
        rows = sorted(((k[1], k[2], k[3], h) for k, h in ctx["by_dev_task"].items()
                       if k[0] == name), key=lambda r: -r[3])
        total = sum(r[3] for r in rows)
        bil = ctx["kinds"].get((name, "billable"), 0.0)
        worked = total + bil
        share = ratio(total, worked)
        title = (f"{cap.cell(name)} — {hd(total)} non facturables "
                 f"({pct_txt(share)} du travaillé, {ctx['window_label']})")
        with md_to_html.folded(title, True, level=4):
            header = ["Projet", "Tâche", "Nature", "Heures", "Part"]
            if rd:
                header.append(f"{rd} j")
            header.append("Libellés")
            print("| " + " | ".join(header) + " |")
            print("|" + "|".join(["---"] * len(header)) + "|")
            for project, task, kind, hours in rows[:NB_DEV_ROWS]:
                labels = sorted(((lab, h) for (n, p, t, lab), h
                                 in ctx["task_labels"].items()
                                 if n == name and p == project and t == task),
                                key=lambda x: -x[1])
                lab_txt = " · ".join(f"{cap.cell(lab[:50])} ({hd(h)})"
                                     for lab, h in labels[:NB_LABELS])
                # The top labels alone read as a breakdown that does not add
                # up to `Heures` — say what is left, so nobody sums them.
                pairs = sorted(ctx["nb_ids"].get((name, project, task), ()))
                url = lines_url(ctx, dev, kind, pairs=pairs)
                tids = {t for _p, t in pairs}
                rest = labels[NB_LABELS:]
                if rest:
                    # No italics around it: `md_to_html` does not apply them
                    # across a link, and the underscores print literally.
                    lab_txt += (f" · [+ {len(rest)} autres "
                                f"({hd(sum(h for _l, h in rest))})]({url})")
                task_txt = (f"[{link_text(task)}]({ctx['odoo_url']}/odoo/"
                            f"project.task/{min(tids)})"
                            if len(tids) == 1 and min(tids) else cap.cell(task))
                cells = [cap.cell(project.strip()), task_txt,
                         "Invest" if kind == "invest" else "Interne",
                         f"[{hd(hours)}]({url})",
                         f"{hours / total * 100.0:.0f}%" if total else "—"]
                if rd:
                    recent = ctx["by_dev_task_recent"].get(
                        (name, project, task, kind), 0.0)
                    cells.append(hd(recent) if recent else "—")
                cells.append(lab_txt)
                print("| " + " | ".join(cells) + " |")
            if len(rows) > NB_DEV_ROWS:
                rest = rows[NB_DEV_ROWS:]
                rest_h = sum(r[3] for r in rest)
                rest_url = lines_url(ctx, dev, pairs=sorted(set().union(*(
                    ctx["nb_ids"].get((name, p, t), set()) for p, t, _k, _h in rest))))
                cells = [f"_{len(rest)} autres_", "", "",
                         f"[{hd(rest_h)}]({rest_url})",
                         f"{rest_h / total * 100.0:.0f}%" if total else "—"]
                if rd:
                    cells.append("")
                cells.append("")
                print("| " + " | ".join(cells) + " |")
            print()


# A project has to lose more than this before it is worth a line. Below it the
# list is twenty rows of half-hours that bury the two that matter.
LEAK_FLOOR = 1.0


def section_fuites(ctx):
    """The non-billable hours booked on projects that *have* a client.

    Internal projects are non-billable by design and say nothing. A client
    project whose hours come out `non_billable` is almost always a missing sales
    order line rather than a decision — the hour was worked for a paying
    customer and quietly given away. That is the one part of the gap to target
    that costs nobody any working time to close."""
    leaks = []
    for name, partner in ctx["leaking"].items():
        hours = sum(h for (p, _k), h in ctx["by_project"].items() if p == name)
        if hours >= LEAK_FLOOR:
            leaks.append((hours, name, partner))
    if not leaks:
        return
    leaks.sort(reverse=True)
    total = sum(h for h, _n, _p in leaks)
    print(f"**Fuites — {hd(total)} non facturées sur des projets qui ont un "
          f"client**\n")
    print("_Ces heures ne sont pas de l'interne : elles ont été travaillées pour "
          "un client payant et typées `non_billable`, presque toujours faute "
          "d'une ligne de commande. **C'est la seule part de l'écart qui se "
          "rattrape sans changer la façon dont l'équipe travaille** — d'où sa "
          f"place à part. Projets sous {LEAK_FLOOR:.0f}h non listés._\n")
    print("| Projet | Client | Heures |")
    print("|---|---|---|")
    for hours, name, partner in leaks[:10]:
        print(f"| {cap.cell(name.strip())} | {cap.cell(partner)} | {hd(hours)} |")
    if len(leaks) > 10:
        rest = sum(h for h, _n, _p in leaks[10:])
        print(f"| _{len(leaks) - 10} autres_ | | {hd(rest)} |")
    print()


def section_conges_mal_pointes(ctx, states, fold=False):
    """Leave booked on an internal project instead of Congés.

    It is not a rounding detail: leave counted as internal *work* is pure
    denominator, and it pushes the ratio down without anyone having worked a
    non-billable hour. It is also not something to fix silently — the match is
    on free text, so the hours are presented as a correction the reader applies
    themselves, with the false positives named. The real fix is upstream anyway:
    book the day on Congés."""
    rows = sorted(ctx["misfiled"].items(), key=lambda kv: -kv[1])
    total = sum(ctx["misfiled"].values())
    counted = sum(h for (n, _p), h in ctx["misfiled"].items()
                  if states.get(n, {}).get("trusted"))
    worked = sum(h for (n, k), h in ctx["kinds"].items()
                 if k != "leave" and states.get(n, {}).get("trusted"))
    if not rows or worked <= 0.05 or counted / worked < MISFILED_FLOOR:
        return
    with md_to_html.folded(f"Congés pointés hors du projet Congés — {hd(total)}", fold):
        print("_Une journée de congé pointée sur `Interne` est comptée comme du "
              "**travail** non facturable : elle entre au dénominateur et fait "
              "baisser le ratio sans que personne ait passé une heure non "
              "facturable. Les congés pointés sur le projet Congés, eux, sortent du "
              "calcul — c'est le même congé, noté à deux endroits, avec deux effets "
              "opposés sur le chiffre._\n")
        print("_**Ces heures ne sont pas retirées du ratio ci-dessus.** Elles sont "
              "repérées sur le libellé saisi à la main, donc à titre indicatif : "
              "« Clean retour de vacances » est une vraie matinée de travail et "
              "« meetup avant ses congés » est une réunion. Corriger un chiffre "
              "d'équipe sur une recherche de texte serait pire que l'erreur qu'on "
              "corrige. Le vrai correctif est en amont : poser la journée sur le "
              "projet Congés._\n")
        by_name = {d["name"]: d for d in ctx["devs"]}
        print("_Les heures ouvrent les lignes repérées, libellé par libellé._\n")
        print("| Développeur | Projet | Heures |")
        print("|---|---|---|")
        for (name, project), hours in rows[:12]:
            flag = "" if states.get(name, {}).get("trusted") else " ⚠️"
            url = lines_url(ctx, by_name[name], kind="internal", leave=True,
                            project=ctx["misfiled_ids"].get((name, project), {0}))
            print(f"| {cap.cell(name)}{flag} | {cap.cell(project)} | [{hd(hours)}]({url}) |")
        if len(rows) > 12:
            print(f"| _{len(rows) - 12} autres_ | | {hd(sum(h for _k, h in rows[12:]))} |")
        print()


def section_notes(ctx, cfg, fold=False):
    """What the page could not read, and what it silently assumed."""
    notes = []
    for dev in ctx["devs"]:
        extra = [e for e in dev["employees"] if len(dev["employees"]) > 1]
        if len(extra) > 1:
            detail = ", ".join(
                f"{cap.cell(e['name'])} ({e['company_id'][1] if e['company_id'] else '—'}, "
                f"{hd(ctx['hours_by_emp'].get(e['id'], 0.0))})" for e in extra)
            notes.append(f"**{cap.cell(dev['name'])}** porte "
                         f"{len(extra)} fiches employé — {detail}. Leurs heures "
                         f"sont additionnées, et le régime est lu sur "
                         f"`{cap.cell(ctx['employees'][dev['name']]['name'])}`, "
                         f"la fiche la plus pointée.")
    for name in ctx["missing"]:
        notes.append(f"**{cap.cell(name)}** : aucune fiche employé rattachée à "
                     f"cet utilisateur — développeur absent de toute la page. "
                     f"Vérifiez `dev_names` dans `{digest_config.CONFIG_PATH}`.")
    for dev in ctx["devs"]:
        join = ctx["joins"].get(dev["name"])
        if join and join > ctx["window_from"]:
            notes.append(f"**{cap.cell(dev['name'])}** : fenêtre "
                         f"{ctx['window_de']} recalée au {join:%d/%m/%Y} — sa "
                         f"toute première ligne de pointage lue par ce rapport. "
                         f"Les colonnes `Trous`/`Couverture` sur cette fenêtre "
                         f"ne comptent rien avant cette date, donc rien avant "
                         f"ce qui a été pointé.")
    # A leaver is not detected the symmetric way — see the comment on `joins`
    # in `gather()` — so an archived employee just fails to resolve rather
    # than skewing a window. Nothing to compute here; this note only explains
    # why nothing appears.
    for dev in ctx["devs"]:
        sched = ctx["schedules"][dev["name"]]
        if not sched["hours"]:
            notes.append(f"**{cap.cell(dev['name'])}** n'a pas de calendrier de "
                         f"travail : sa grille retombe sur lundi–vendredi et "
                         f"{cap.HOURS_PER_DAY:.0f} h par jour, ce qui peut "
                         f"inventer des trous.")
    for raw, hours in sorted(ctx["unknown"].items(), key=lambda kv: -kv[1]):
        notes.append(f"Type de facturation `{raw}` inconnu de ce rapport "
                     f"({hd(hours)}) — compté comme **interne**, donc contre "
                     f"l'équipe. Ajoutez-le à `BILLABLE_TYPES` ou "
                     f"`INVEST_TYPES` s'il ne l'est pas.")
    if not notes:
        return
    with md_to_html.folded("Notes", fold):
        print("_Ce que la page n'a pas pu lire, et ce qu'elle a supposé sans le "
              "dire ailleurs._\n")
        for note in notes:
            print(f"- {note}")
        print()


def render(ctx, cfg, fold=False):
    """`fold=True` folds everything below the two lead sections.

    Tenue and Facturable are the page's two questions and stay open; the trend,
    the non-billable breakdown, the misfiled leave and the notes are what one
    opens after reading the answer. On its own page nothing is folded — there
    the report *is* the page, and a page of closed boxes answers nothing."""
    states = {d["name"]: fill_state(ctx, d["name"], cfg) for d in ctx["devs"]}
    section_tenue(ctx, cfg, states)
    section_facturable(ctx, cfg, states)
    section_tendance(ctx, cfg, states, fold=fold)
    section_non_facturable(ctx, fold=fold)
    section_conges_mal_pointes(ctx, states, fold=fold)
    section_notes(ctx, cfg, fold=fold)
    return states


# --- CLI ---------------------------------------------------------------------

def build(argv=None):
    ap = argparse.ArgumentParser(description="Feuilles de temps DEV TSC.")
    ap.add_argument("developer", nargs="?", help="filtrer sur un développeur")
    ap.add_argument("--days", type=int, default=None,
                    help="remplace les deux fenêtres par une seule, glissante, "
                         "de N jours (défaut : année civile + "
                         f"{RECENT_WINDOW} jours côte à côte)")
    ap.add_argument("--recent", type=int, default=RECENT_WINDOW,
                    help="fenêtre « situation actuelle », en jours "
                         f"(défaut : {RECENT_WINDOW})")
    ap.add_argument("--today", help="date de référence (YYYY-MM-DD)")
    ap.add_argument("--format", choices=["md", "html"], default="html")
    ap.add_argument("--no-open", dest="open_it", action="store_false")
    ap.add_argument("--out-dir", default=digest.output_dir())
    args = ap.parse_args(argv)

    cfg = digest_config.load()
    kw = digest_config.connect(cfg)
    today = (datetime.date.fromisoformat(args.today) if args.today
             else datetime.date.today())
    ctx = gather(kw, cfg, today, args.days, args.recent, args.developer)
    print(f"# {TITLE} — {today:%d/%m/%Y}\n")
    print(f"_{len(ctx['devs'])} développeur(s) · {ctx['n_lines']} lignes de "
          f"pointage lues._\n")
    render(ctx, cfg)
    return args


def main(argv=None):
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        args = build(argv)
    markdown = buf.getvalue()
    print(markdown)
    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, f"timesheets.{args.format}")
    content = (md_to_html.render(markdown, title=TITLE)
               if args.format == "html" else markdown)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"Enregistré : {path}", file=sys.stderr)
    if args.open_it:
        digest.open_in_browser(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
