#!/usr/bin/env python3
"""Everything that talks to Odoo: task lookup, what is already logged, push.

`kw` is the execute_kw callable from ts_config.connect().
"""
import xmlrpc.client

_CTX = {"context": {"active_test": False}}


def fetch_tasks(kw, ids):
    """{id: {name, project_id, project_name, company_id}} for the ids that exist
    and carry a project — a stray number in a prompt simply is not returned."""
    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return {}
    rows = kw("project.task", "search_read", [[("id", "in", ids)]],
              {"fields": ["name", "project_id", "company_id"], **_CTX})
    out = {}
    for r in rows:
        if not r["project_id"]:
            continue
        out[r["id"]] = {
            "name": r["name"],
            "project_id": r["project_id"][0],
            "project_name": r["project_id"][1],
            "company_id": r["company_id"][0] if r["company_id"] else None,
        }
    return out


def fetch_projects(kw, ids):
    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return {}
    rows = kw("project.project", "search_read", [[("id", "in", ids)]],
              {"fields": ["name", "company_id"], **_CTX})
    return {r["id"]: {"name": r["name"],
                      "company_id": r["company_id"][0] if r["company_id"] else None}
            for r in rows}


def logged_lines(kw, uid, day):
    """The user's timesheet lines already in Odoo for `day`."""
    rows = kw("account.analytic.line", "search_read",
              [[("user_id", "=", uid), ("date", "=", str(day)), ("project_id", "!=", False)]],
              {"fields": ["project_id", "task_id", "name", "unit_amount"], "order": "id"})
    return [{"id": r["id"],
             "project_id": r["project_id"][0],
             "project_name": r["project_id"][1],
             "task_id": r["task_id"][0] if r["task_id"] else None,
             "task_name": r["task_id"][1] if r["task_id"] else "",
             "description": r["name"],
             "hours": r["unit_amount"]} for r in rows]


def search_tasks(kw, query, project_id=None, limit=20):
    domain = []
    query = (query or "").strip()
    # A task id is looked up in every project: that is how a line moves to
    # a task of another project without picking the project first.
    if project_id and not query.isdigit():
        domain.append(("project_id", "=", int(project_id)))
    if query.isdigit():
        domain.append(("id", "=", int(query)))
    elif query:
        domain.append(("name", "ilike", query))
    rows = kw("project.task", "search_read", [domain],
              {"fields": ["name", "project_id", "stage_id"], "limit": limit,
               "order": "write_date desc"})
    return [{"id": r["id"], "name": r["name"],
             "project_id": r["project_id"][0] if r["project_id"] else None,
             "project_name": r["project_id"][1] if r["project_id"] else "",
             "stage": r["stage_id"][1] if r["stage_id"] else ""} for r in rows]


def search_projects(kw, query, limit=20):
    query = (query or "").strip()
    domain = [("id", "=", int(query))] if query.isdigit() else \
        [("name", "ilike", query)] if query else []
    rows = kw("project.project", "search_read", [domain],
              {"fields": ["name", "partner_id"], "limit": limit, "order": "write_date desc"})
    return [{"id": r["id"], "name": r["name"],
             "partner": r["partner_id"][1] if r["partner_id"] else ""} for r in rows]


def pick_employee(employees, company_id):
    """The user's employee record in the project's company, else the first one.
    A developer can carry one hr.employee per company (BE + FR)."""
    for emp in employees:
        if company_id and emp["company_id"] == company_id:
            return emp["id"]
    return employees[0]["id"] if employees else None


def user_employees(kw, uid):
    rows = kw("hr.employee", "search_read", [[("user_id", "=", uid)]],
              {"fields": ["company_id"], "order": "id"})
    return [{"id": r["id"], "company_id": r["company_id"][0] if r["company_id"] else None}
            for r in rows]


def line_values(line, day, employees, company_id):
    values = {
        "date": str(day),
        "project_id": int(line["project_id"]),
        "name": line["description"].strip() or "/",
        "unit_amount": float(line["hours"]),
    }
    if line.get("task_id"):
        values["task_id"] = int(line["task_id"])
    employee = pick_employee(employees, company_id)
    if employee:
        values["employee_id"] = employee
    return values


def push(kw, uid, day, lines):
    """Create one account.analytic.line per line, one call each so a refused
    line (locked period, archived task) does not take the others down.
    Returns [{"id": line id, "odoo_id": ..., "error": ...}]."""
    employees = user_employees(kw, uid)
    tasks = fetch_tasks(kw, [l["task_id"] for l in lines if l.get("task_id")])
    projects = fetch_projects(kw, [l["project_id"] for l in lines])
    results = []
    for line in lines:
        company = (tasks.get(line.get("task_id")) or {}).get("company_id") or \
            (projects.get(line["project_id"]) or {}).get("company_id")
        try:
            odoo_id = kw("account.analytic.line", "create",
                         [line_values(line, day, employees, company)])
            results.append({"id": line["id"], "odoo_id": odoo_id, "error": None})
        except xmlrpc.client.Fault as exc:
            msg = exc.faultString.strip().splitlines()[-1] if exc.faultString else str(exc)
            results.append({"id": line["id"], "odoo_id": None, "error": msg})
    return results
