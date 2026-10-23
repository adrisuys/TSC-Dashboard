"""Tab "Ma feuille de temps" — the tsc-timesheet review page, inside the app.

The engine is the plugin's own (`timesheet/`, copied unchanged): it rebuilds a
day from local traces (Claude Code sessions, Chrome history, git commits),
proposes one line per Odoo task, and pushes the ticked ones. This module is
only the glue the plugin's `review_server.py` used to be: the tab's context
and a small JSON API under /feuille/api/.

Two differences with the rest of the app, both deliberate:

- **It writes to Odoo** (one `account.analytic.line` per pushed line, as the
  configured user) and **reads private traces** (prompts, browsing history).
  So the API answers only a browser on this Mac (`http://127.0.0.1:…`): a
  request that came through Tailscale Serve — Host of the tailnet, forwarded
  headers — is refused, and the tab says where to open it instead. Every call
  also carries a per-process token baked into the page, sent in a custom
  header, so another website open in the same browser can neither push nor
  read (the header forces a CORS preflight that is never answered; the Host
  check stops DNS rebinding).
- **It is desktop only**: the tab is hidden on phones and tablets (app.css).
"""
import datetime as dt
import json
import os
import secrets
import sys
import threading
import xmlrpc.client

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "timesheet"))
import dayfile  # noqa: E402
import odoo_sync  # noqa: E402
import ts_config  # noqa: E402

KEY = "feuille"
TOKEN = secrets.token_urlsafe(24)
LOCAL_NAMES = ("127.0.0.1", "localhost", "[::1]")
# Opened from another computer (over the tailnet), the tab embeds that
# computer's own review page instead: the tsc-timesheet plugin's server, which
# `/timesheet` starts on 127.0.0.1:8735. The iframe's address is resolved by
# the browser, so "127.0.0.1" there is the computer the browser runs on, its
# traces and its Odoo key; nothing goes through this Mac.
EMBED_URL = "http://127.0.0.1:8735/"
# Set by a reverse proxy (Tailscale Serve sets the first and the Tailscale-*
# identity headers): their presence means the request did not start here.
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded",
                 "tailscale-user-login")

# One lock for the day files and the Odoo connection: a build or a push
# rewrites the day file, and an XML-RPC ServerProxy is not thread-safe.
_lock = threading.RLock()
_odoo = {}


def is_local(request):
    """The browser runs on this Mac and talks to the app directly."""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in LOCAL_NAMES:
        return False
    if any(h in request.headers for h in PROXY_HEADERS):
        return False
    client = request.client.host if request.client else ""
    return client in ("127.0.0.1", "::1")


def context(request, port):
    """What the tab's template needs. The token is only handed to a local page."""
    local = is_local(request)
    try:
        odoo_url = ts_config.load()["url"]
    except SystemExit:
        odoo_url = ""
    return {"local": local, "token": TOKEN if local else "", "odoo_url": odoo_url,
            "local_url": f"http://127.0.0.1:{port}/{KEY}", "embed_url": EMBED_URL,
            "today": dt.date.today().isoformat()}


def _connect(cfg):
    if "kw" not in _odoo:
        _odoo["uid"], _odoo["kw"] = ts_config.connect(cfg)
    return _odoo["uid"], _odoo["kw"]


def _parse_date(value):
    return dt.date.fromisoformat(value) if value else dt.date.today()


def _guarded(request, fn):
    """Run `fn(cfg)` for an authorised local request; map failures to JSON."""
    if not is_local(request):
        return JSONResponse({"error": "Disponible uniquement sur le Mac qui héberge "
                                      "l'application (http://127.0.0.1)."}, 403)
    if not secrets.compare_digest(request.headers.get("x-ts-token", ""), TOKEN):
        return JSONResponse({"error": "Session expirée (l'application a redémarré) : "
                                      "rechargez la page."}, 403)
    try:
        with _lock:
            return JSONResponse(fn(ts_config.load()))
    except ts_config.ConfigError as exc:
        return JSONResponse({"error": str(exc)}, 401)
    except xmlrpc.client.Fault as exc:
        return JSONResponse({"error": exc.faultString.strip().splitlines()[-1]}, 502)
    except OSError as exc:
        _odoo.clear()
        return JSONResponse({"error": f"Odoo injoignable : {exc}"}, 502)
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, 400)
    except SystemExit as exc:          # ts_config.load() on an unreadable file
        return JSONResponse({"error": str(exc)}, 500)


def _build(cfg, day):
    uid, kw = _connect(cfg)
    return dayfile.build(cfg, day, kw, uid)


def _day(cfg, day):
    data = dayfile.load(str(day))
    return data if data is not None else _build(cfg, day)


def _save(cfg, body):
    data = dayfile.load(str(_parse_date(body.get("date"))))
    if data is None:
        raise ValueError("Journée pas encore construite.")
    edits = body.get("edits", [])
    dayfile.apply_edits(data, edits)
    if any("task_id" in e or "project_id" in e for e in edits):
        dayfile.refresh_tasks(data, _connect(cfg)[1])
    dayfile.save(data)
    return data


def _push(cfg, body):
    day = _parse_date(body.get("date"))
    uid, kw = _connect(cfg)
    data = dayfile.load(str(day))
    if data is None:
        raise ValueError("Journée pas encore construite.")
    dayfile.apply_edits(data, body.get("edits", []))
    lines = [line for line in data["lines"] if dayfile.pushable(line)]
    results = odoo_sync.push(kw, uid, day, lines) if lines else []
    dayfile.record_push(data, results)
    data["logged"] = odoo_sync.logged_lines(kw, uid, day)
    dayfile.set_logged(data["lines"], data["logged"])
    dayfile.save(data)
    return {"results": results, "day": data}


router = APIRouter(prefix=f"/{KEY}/api", include_in_schema=False)


@router.get("/day")
def api_day(request: Request, date: str = ""):
    return _guarded(request, lambda cfg: _day(cfg, _parse_date(date)))


@router.post("/build")
async def api_build(request: Request):
    return await _posted(request, lambda cfg, body: _build(cfg, _parse_date(body.get("date"))))


@router.post("/save")
async def api_save(request: Request):
    return await _posted(request, _save)


@router.post("/push")
async def api_push(request: Request):
    return await _posted(request, _push)


@router.get("/tasks")
def api_tasks(request: Request, q: str = "", project_id: str = ""):
    return _guarded(request, lambda cfg: odoo_sync.search_tasks(
        _connect(cfg)[1], q, project_id or None))


@router.get("/projects")
def api_projects(request: Request, q: str = ""):
    return _guarded(request, lambda cfg: odoo_sync.search_projects(_connect(cfg)[1], q))


async def _posted(request, fn):
    """A POST: the JSON body is read here (async), then the slow part — Odoo,
    the traces — runs off the event loop like the sync GET handlers. The body
    is parsed inside the guard, so a refused request never gets that far."""
    raw = await request.body()
    return await run_in_threadpool(
        _guarded, request, lambda cfg: fn(cfg, json.loads(raw or b"{}")))
