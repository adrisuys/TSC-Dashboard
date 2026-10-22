#!/usr/bin/env python3
"""Runtime configuration + Odoo connection for the timesheet autofill.

Resolved, in order (later wins):

  1. DEFAULTS below — reporting rules only, never a key
  2. the Odoo credentials of ~/.config/tsc-dev-digest/config.json, when present,
     so a developer who already ran /dev-digest-setup has nothing to configure
  3. ~/.config/tsc-timesheet/config.json   (written by setup.py, mode 0600)
  4. environment  TSC_TIMESHEET_URL / _DB / _LOGIN / _API_KEY

Every rule that maps a trace onto a project (repo, worktree, browser domain,
non-ticket bucket) is configuration, so a new client repo is a config edit.
"""
import copy
import json
import os
import sys
import xmlrpc.client

CONFIG_PATH = os.path.expanduser("~/.config/tsc-timesheet/config.json")
DIGEST_CONFIG_PATH = os.path.expanduser("~/.config/tsc-dev-digest/config.json")
DATA_DIR = os.path.expanduser("~/Library/Application Support/tsc-timesheet")

# The four non-ticket buckets. Shown on the review page for reference, never
# pushed: their time is booked by hand on whatever project fits that day.
BUCKETS = ("meeting", "support", "internal", "admin")

DEFAULTS = {
    "url": "https://www.tsc-experts.com",
    "db": "tsc-be-main-3784485",
    "login": "",
    "api_key": "",
    "port": 8735,
    # --- sources ---------------------------------------------------------------
    "claude_projects_dir": "~/.claude/projects",
    "chrome_dir": "~/Library/Application Support/Google/Chrome",
    # Profile picked by the account signed into it; empty = the Odoo login.
    "chrome_profile_email": "",
    # Root under which `custom/<version>/<repo>/<worktree>` lives, for git.
    "workspace_root": "~/Desktop/TSC",
    # Empty = the Odoo login.
    "git_author": "",
    # --- timeline --------------------------------------------------------------
    # A silence longer than this closes an activity block.
    "idle_gap_minutes": 15,
    # Reading/testing time credited after the last event of a block.
    "tail_minutes": 5,
    # A browser visit lasts until the next one, at most this long.
    "chrome_visit_cap_minutes": 5,
    # A commit stands for this much work before it.
    "commit_lead_minutes": 10,
    # Two things at once (parallel Claude sessions on two tickets): "split"
    # shares the moment between them, "full" credits it to each.
    "overlap": "split",
    # Every line is rounded UP to this — 5 minutes of work is a 15-minute line.
    "rounding_minutes": 15,
    # Below this much real time a line is not proposed at all: a 2-minute
    # glance at a task page is not work.
    "min_minutes": 5,
    # --- attribution -----------------------------------------------------------
    # `repo` or `repo/worktree-prefix` -> project.project id. The longest
    # matching key wins, so an `inspector` worktree can point elsewhere than its
    # repo. Used only when no task id is found.
    "repo_projects": {
        "european-schools": 617,
        "european-schools/inspector": 813,
        "capinvest": 752,
        "chantdeole": 163,
        "multitel": 526,
        "EMC": 554,
        "hem-france": 638,
        "codaclean": 576,
        "Alaize": 766,
        "app-archi2000": 768,
        "uwa-web": 677,
        "Fortis-Blade": 637,
        "lesfunerailles": 627,
        "Dilaco": 549,
        "main7": 106,
        "swissharvest-franchise": 232,
        "senteursdailleurs": 863,
        "moismont": 384,
        "nespart": 98,
        "air-glaciers-v18-new": 651,
    },
    # Folder -> project.task id, for internal work outside custom/: an edit in
    # that folder books the session on the task until something else moves it.
    # The longest folder wins. Edits only — running a skill's script is using
    # the skill, not working on it.
    "path_tasks": {
        "~/Desktop/TSC/skills": 27678,
        "~/Desktop/TSC/.claude/skills": 27678,
        "~/.claude/skills": 27678,
        "~/Desktop/TSC/skills/tsc-claude-plugins/plugins/tsc-dev-digest": 29425,
    },
    # Browser host (suffix match) -> project.project id, for client instances.
    "domain_projects": {},
    # GitHub organisation whose repositories map through repo_projects.
    "github_orgs": ["TSC-BE"],
    # Host of our own Odoo — task and helpdesk URLs are read from it.
    "odoo_host": "tsc-experts.com",
    # Non-ticket buckets, matched on the browser host (suffix match).
    "bucket_hosts": {
        "meeting": ["meet.google.com", "teams.microsoft.com", "teams.live.com",
                    "zoom.us", "whereby.com"],
        "admin": ["mail.google.com", "calendar.google.com", "outlook.office.com",
                  "outlook.live.com", "app.qonto.com"],
        "support": [],
        "internal": [],
    },
    # Claude sessions that touch no client repo land here (tooling, skills,
    # analyses outside a ticket).
    "claude_default_bucket": "internal",
}

ENV_VARS = {
    "url": "TSC_TIMESHEET_URL",
    "db": "TSC_TIMESHEET_DB",
    "login": "TSC_TIMESHEET_LOGIN",
    "api_key": "TSC_TIMESHEET_API_KEY",
}
REQUIRED = ("url", "db", "login", "api_key")


def _read_json(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        sys.exit(f"Cannot read {path}: {exc}")
    if not isinstance(data, dict):
        sys.exit(f"{path} must contain a JSON object.")
    return data


def load(path=CONFIG_PATH, digest_path=DIGEST_CONFIG_PATH, environ=None):
    environ = os.environ if environ is None else environ
    cfg = copy.deepcopy(DEFAULTS)
    digest = _read_json(digest_path) if digest_path else {}
    for key in REQUIRED:
        if digest.get(key):
            cfg[key] = digest[key]
    stored = _read_json(path) if path else {}
    for key, value in stored.items():
        # Dicts merge one level deep so a user adds a repo without restating all.
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    for key, var in ENV_VARS.items():
        if environ.get(var):
            cfg[key] = environ[var]
    cfg["chrome_profile_email"] = cfg["chrome_profile_email"] or cfg["login"]
    cfg["git_author"] = cfg["git_author"] or cfg["login"]
    for key in ("claude_projects_dir", "chrome_dir", "workspace_root"):
        cfg[key] = os.path.expanduser(cfg[key])
    return cfg


def save(values, path=CONFIG_PATH):
    """Merge `values` into the stored config, owner-only permissions."""
    stored = _read_json(path)
    stored.update(values)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(stored, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.chmod(path, 0o600)
    return path


class ConfigError(Exception):
    pass


def require_credentials(cfg):
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        raise ConfigError(
            f"Missing Odoo credentials: {', '.join(missing)}. Run /timesheet-setup "
            f"(or /dev-digest-setup, whose key is reused).")
    return cfg


def connect(cfg):
    """Return (uid, kw) where kw(model, method, args, kwargs) runs execute_kw."""
    require_credentials(cfg)
    common = xmlrpc.client.ServerProxy(f"{cfg['url']}/xmlrpc/2/common", allow_none=True)
    uid = common.authenticate(cfg["db"], cfg["login"], cfg["api_key"], {})
    if not uid:
        raise ConfigError(f"Authentication failed for {cfg['login']} on {cfg['db']}.")
    models = xmlrpc.client.ServerProxy(f"{cfg['url']}/xmlrpc/2/object", allow_none=True)

    def kw(model, method, args, kwargs=None):
        return models.execute_kw(cfg["db"], uid, cfg["api_key"], model, method,
                                 args, kwargs or {})

    return uid, kw


def day_path(day):
    return os.path.join(DATA_DIR, "days", f"{day}.json")
