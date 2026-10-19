#!/usr/bin/env python3
"""Runtime configuration + Odoo connection for the DEV TSC digest.

Credentials are never shipped with the plugin. They are resolved, in order:

  1. environment  TSC_DIGEST_URL / _DB / _LOGIN / _API_KEY
  2. ~/.config/tsc-dev-digest/config.json   (written by setup_credentials.py, mode 0600)
  3. the DEFAULTS below — connection endpoint and reporting scope only, never a key

Everything that identifies the team (tag, developer names, the two colliding
"En cours" stage ids) is configuration too, so a joiner or a leaver is a config
edit rather than a code change.
"""
import json
import os
import re
import sys
import xmlrpc.client

CONFIG_PATH = os.path.expanduser("~/.config/tsc-dev-digest/config.json")

# Loopback port for dev_resync_server.py, which backs the app's
# "Resynchroniser" button. No auth: fine for a single-user, single-machine tool.
RESYNC_PORT = 8734

DEFAULTS = {
    "url": "https://www.tsc-experts.com",
    "db": "tsc-be-main-3784485",
    "login": "",
    "api_key": "",
    "tag": "DEV TSC",
    "dev_names": [
        "Adrien Suys",
        "Thomas Dieuzeide",
        "Adam Tangiev",
        "Nathan Giacomello",
        "David Andrian",
    ],
    # Two distinct stages are both named "En cours"; match them by id.
    "stage_encours_ids": [420, 665],
    # --- capacity report (dev_capacity_report.py) ----------------------------
    # Developers who carry no project allocation on purpose: judged on
    # occupation, never on plan adherence.
    "pool_devs": ["Thomas Dieuzeide"],
    # Absences are timesheeted on this partner-less project, and public holidays
    # are pre-booked there in the future. hr.leave is NOT usable: a record rule
    # hides other people's leaves from an ordinary account.
    "leave_project_ids": [111],
    # tsc.role names that count as development capacity in the allocation plan.
    "dev_role_names": ["Dev"],
    # Work schedules that production gets wrong. {name: {"days": [weekday, ...],
    # "hours": per week}}, weekday 0 = Monday. Nathan works Mon-Thu but sits on
    # "Standard 40 hours/week" in Odoo; the 32h calendar that exists (id 4) is
    # Mon/Tue/Thu/Fri, so it is the wrong pattern and cannot just be assigned.
    # Drop the entry once HR encodes a 32h Mon-Thu calendar.
    "dev_schedules": {
        "Nathan Giacomello": {"days": [0, 1, 2, 3], "hours": 32.0},
    },
    # Below this share of their capacity, with no leave to explain it, a
    # developer's hours are a timesheet gap rather than free time.
    "gap_ratio": 0.5,
    # Window the schedule-mismatch detector looks back over.
    "schedule_detect_weeks": 10,
    # --- stats report (dev_stats_report.py) ----------------------------------
    # Days of ticket history the delay medians are computed over, on create_date.
    "stats_window_days": 90,
    # Calendar days in a stage past which a waiting ticket is flagged. Same
    # numbers the digest already uses for its stale markers.
    "stats_thresholds": {"spec_days": 1, "todo_days": 3},
    # --- planned vs quickstart (dev_plan_scope.py) ---------------------------
    # The split is derived from the capacity plan: a project is planned when it
    # carries at least one allocated day of one of our developers in the window.
    # These two lists are for what the query cannot see — a project planned
    # entirely outside the window, or one allocated for something other than
    # development. Matched on the project name.
    "planned_projects_extra": [],
    "quickstart_projects_extra": [],
}

ENV_VARS = {
    "url": "TSC_DIGEST_URL",
    "db": "TSC_DIGEST_DB",
    "login": "TSC_DIGEST_LOGIN",
    "api_key": "TSC_DIGEST_API_KEY",
}

REQUIRED = ("url", "db", "login", "api_key")

SETUP_HINT = """\
Missing digest credentials: {missing}

Run the one-time setup, then try again:

    python3 {setup}

It asks for your Odoo login and a personal API key, checks they work, and
stores them in {path} (readable only by you).

Create an API key in Odoo: avatar (top right) -> My Profile -> Account Security
-> New API Key. The key is shown once — copy it before closing the dialog.
"""


def _setup_script():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_credentials.py")


def load():
    """Merge defaults, the config file and the environment into one dict."""
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as fh:
            stored = json.load(fh)
    except FileNotFoundError:
        stored = {}
    except (OSError, ValueError) as exc:
        sys.exit(f"Cannot read {CONFIG_PATH}: {exc}")
    if not isinstance(stored, dict):
        sys.exit(f"{CONFIG_PATH} must contain a JSON object.")
    cfg.update(stored)
    for key, var in ENV_VARS.items():
        if os.environ.get(var):
            cfg[key] = os.environ[var]
    return cfg


def require_credentials(cfg):
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        sys.exit(SETUP_HINT.format(missing=", ".join(missing),
                                   path=CONFIG_PATH,
                                   setup=_setup_script()))
    return cfg


def save(cfg):
    """Write the config with owner-only permissions."""
    directory = os.path.dirname(CONFIG_PATH)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    # Create with 0600 up front so the key is never briefly world-readable.
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.chmod(CONFIG_PATH, 0o600)
    return CONFIG_PATH


def authenticate(cfg):
    """Return the uid, or None when the credentials are rejected."""
    with xmlrpc.client.ServerProxy(f"{cfg['url']}/xmlrpc/2/common") as common:
        return common.authenticate(cfg["db"], cfg["login"], cfg["api_key"], {})


def connect(cfg=None):
    """Return an `execute_kw` callable bound to the configured instance.

    `kw.close()` releases its HTTPS connection. One-shot scripts can ignore it,
    but the long-lived web app must call it: an unclosed connection stays open
    (CLOSE_WAIT) after Odoo hangs up, and once the process reaches its 256
    file-descriptor limit every new connection fails, starting with the DNS
    lookup (`gaierror: nodename nor servname provided`)."""
    cfg = require_credentials(cfg or load())
    uid = authenticate(cfg)
    if not uid:
        sys.exit(f"Authentication failed for {cfg['login']} on {cfg['db']}. "
                 f"The API key may have been revoked — re-run setup_credentials.py.")
    models = xmlrpc.client.ServerProxy(f"{cfg['url']}/xmlrpc/2/object")

    def kw(model, method, args, kwargs=None):
        return models.execute_kw(cfg["db"], uid, cfg["api_key"], model, method,
                                 args, kwargs or {})

    kw.close = models("close")
    return kw


# --- stage names -------------------------------------------------------------
# Production decorates its stage labels with emoji — "Spec validation / Estim ⚖️",
# "In Progress ⚙️", "Attente de tiers ⏳" — and that decoration is edited at will
# by whoever tidies the kanban. So nothing in these reports matches a stage on
# its full label: names are compared, and stages resolved to ids, on the *base*
# name — the label stripped of emoji, variation selectors and the space they
# leave behind. A future 🚦 costs nothing.
_DECORATION = re.compile(
    "[‍←-⇿⌀-⏿①-⓿■-➿"
    "⤀-⥿⬀-⯿︎️\U0001f000-\U0001faff]"
)

# {id(kw): (kw, rows)} — project.task.type is small and read by several reports
# in one dashboard run. The kw callable is kept in the value so its id cannot be
# recycled onto a different connection.
_STAGE_CACHE = {}


def base_stage_name(name):
    """'Spec validation / Estim ⚖️' -> 'Spec validation / Estim'."""
    return " ".join(_DECORATION.sub(" ", name or "").split())


def all_stages(kw):
    """[{id, name}] for every project.task.type, read once per connection."""
    cached = _STAGE_CACHE.get(id(kw))
    if cached is None or cached[0] is not kw:
        rows = kw("project.task.type", "search_read", [[]], {"fields": ["name"]})
        cached = (kw, rows)
        _STAGE_CACHE[id(kw)] = cached
    return cached[1]


def stage_ids(kw, names):
    """{name: {ids}} — the stages whose base name matches, so a stage renamed
    with an emoji still resolves. One name carries several ids in this database
    ("En cours" has three), hence a set per name. A name that matches nothing
    yields an empty set: an aspirational stage costs nothing."""
    wanted = {base_stage_name(n): n for n in names}
    out = {n: set() for n in names}
    for stage in all_stages(kw):
        name = wanted.get(base_stage_name(stage["name"]))
        if name:
            out[name].add(stage["id"])
    return out


def stage_id_list(kw, names):
    """The union of stage_ids(), as a list — ready for a `("stage_id", "in", …)`
    domain."""
    return sorted({i for ids in stage_ids(kw, names).values() for i in ids})


def task_link(cfg, task_id):
    """Markdown link to a task's form view."""
    url = f"{cfg['url']}/web#id={task_id}&model=project.task&view_type=form"
    return f"[{task_id}]({url})"
