#!/usr/bin/env python3
"""Which projects the capacity plan commits developer days to — and which are
left for anyone to pick up.

The dev team works two ways at once. Some projects sit in the capacity plan
(`project.allocation.slot`, the grid behind /odoo/action-2407) with named
developers and days per week; everything else is **quickstart** — no allocation,
any developer, whatever comes in. Telling the two apart is what makes a free
hour readable: an allocated day is already spoken for, a quickstart hour is not.

The split is *derived from the plan*, never hardcoded. A project is planned when
the plan gives it at least one day of one of our developers inside the window.
That is why European Schools stops being a special case in the reports: it
becomes one planned project among several, and a project entering the plan next
month is picked up without an edit. `planned_projects_extra` and
`quickstart_projects_extra` in the config exist for the edge a query cannot see
(a project planned entirely outside the window, say), and are matched on the
project name.

Slots of 0 days are ignored: this database carries one per week for Adrien with
no project at all, which is plan noise rather than a commitment.
"""
import datetime

PLANNED = "📌"
QUICKSTART = "⚡"

# Grey and italic mark a **parked** row — a ticket sitting downstream, waiting on
# someone else (see dev_workload_report.PARKED_STAGES). Everything a developer can
# act on today stays in normal black type, whatever its project: 📌 / ⚡ says who
# pays for the work, not whether it is live, and dimming half the live tickets on
# that basis made the plan look like a lower priority than the quickstart queue.
# An inline style rather than a CSS class: these tables are Markdown, read as
# often in a preview or a terminal as in the generated page.
DIM = '<span style="color:gray;font-style:italic">%s</span>'


def dim_row(row):
    """The row, greyed and italicised — for a parked ticket.

    Empty cells are left alone: a span around a blank is invisible and would
    only widen the Markdown. Callers add the ⏸️ themselves, after this:
    a marker inside the span would be dimmed too."""
    return [DIM % c if str(c).strip() else c for c in row]

# Wide enough that a project between two engagements still reads as planned,
# short enough that one finished last spring does not.
WEEKS_BACK = 4
WEEKS_FWD = 8


def monday(date):
    return date - datetime.timedelta(days=date.weekday())


def window(today=None, weeks_back=WEEKS_BACK, weeks_fwd=WEEKS_FWD):
    mon = monday(today or datetime.date.today())
    return (mon - datetime.timedelta(weeks=weeks_back),
            mon + datetime.timedelta(weeks=weeks_fwd))


def fetch_slots(kw, employee_ids, date_from, date_to):
    """Raw allocation slots for those employees over the window."""
    if not employee_ids:
        return []
    return kw("project.allocation.slot", "search_read",
              [[["employee_id", "in", employee_ids],
                ["date", ">=", date_from.isoformat()],
                ["date", "<=", date_to.isoformat()]]],
              {"fields": ["employee_id", "project_id", "role_id", "date", "days"]})


class Scope:
    """Planned / quickstart, resolved once and asked per project."""

    def __init__(self, planned_ids, cfg):
        self.planned_ids = set(planned_ids)
        self.planned_extra = set(cfg.get("planned_projects_extra") or [])
        self.quickstart_extra = set(cfg.get("quickstart_projects_extra") or [])

    def is_planned(self, project):
        """`project` is a many2one value — [id, name] — or a falsy no-project."""
        if not project:
            return False
        pid, name = project[0], project[1]
        if name in self.quickstart_extra:
            return False
        return pid in self.planned_ids or name in self.planned_extra

    def marker(self, project):
        return PLANNED if self.is_planned(project) else QUICKSTART

    def label(self, project):
        """The project name, prefixed by its marker. A task with no project at
        all gets neither: there is nothing to classify."""
        if not project:
            return "-"
        return f"{self.marker(project)} {project[1]}"


def build(kw, cfg, employee_ids=None, today=None):
    """(Scope, slots) — the classification plus the slots it was built from, so
    a caller that also needs the plan does not query it twice."""
    date_from, date_to = window(today)
    if employee_ids is None:
        employee_ids = employee_ids_of(kw, cfg)
    slots = fetch_slots(kw, employee_ids, date_from, date_to)
    planned = {s["project_id"][0] for s in slots
               if s["project_id"] and s["days"] > 0}
    return Scope(planned, cfg), slots


def employee_ids_of(kw, cfg):
    """Every employee id of the configured developers — all their records,
    found through their user as in the capacity report: a slot booked on
    `David Andrian (BE)` makes its project planned as surely as one booked on
    `David Andrian`."""
    users = kw("res.users", "search_read", [[["name", "in", cfg["dev_names"]]]],
               {"fields": ["name"]})
    return kw("hr.employee", "search",
              [["|", ["user_id", "in", [u["id"] for u in users]],
                ["name", "in", cfg["dev_names"]]]])
