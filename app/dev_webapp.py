#!/usr/bin/env python3
"""L'application DEV TSC, servie en local : FastAPI + Jinja + htmx.

    venv/bin/python dev_webapp.py            # http://127.0.0.1:8734

Replaces the static `app.html` (rebuilt by launchd, opened as a `file://`
page) and the stdlib resync server behind its button. One long-lived process
now owns the whole page:

- **Every tab is native**: each reads its data with the report modules' own
  `gather()` (in-process — none of them prints), lays it out with a view
  module (`dev_*_view.py`) and a Jinja template. The Markdown reports are no
  longer involved; they still run on their own (`/dev-app`, `/dev-capacity`…).
- **The data is cached in memory** per tab and filter set (`NativeCache`), and
  refreshed every `REFRESH_MINUTES` by a background thread, and on demand by
  the "Resynchroniser" button. A refresh loads the default view of all four
  tabs *before* swapping it in, so a failed one keeps the last good figures on
  screen and says so in the top bar.
- **A tab is a URL** (`/apercu`, `/travail`, `/capacite`, `/temps`). htmx swaps
  the pane and pushes the URL, so a reload, a bookmark or the back button lands
  on the tab you were reading.
- **`/lines` is the drill-down** behind the hours of "Par développeur — projet
  et tâche": read live from Odoo on each click, shown in a side drawer inside
  the app, or as a page of its own when opened directly.

Listens on 127.0.0.1 only, no auth: a single-user, single-machine tool. Every
Odoo call is read-only.
"""
import datetime
import os
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import digest_config  # noqa: E402
import dev_capacite_view  # noqa: E402
import dev_feuille  # noqa: E402
import dev_apercu_view  # noqa: E402
import dev_capacity_report  # noqa: E402
import dev_periods  # noqa: E402
import dev_quickstart_report  # noqa: E402
import dev_repartition  # noqa: E402
import dev_tasks_report  # noqa: E402
import dev_timesheet_lines  # noqa: E402
import dev_travail_view  # noqa: E402
import dev_timesheet_report  # noqa: E402
import dev_timesheet_view  # noqa: E402
import md_to_html  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "webapp")
HOST = "127.0.0.1"
REFRESH_MINUTES = 30
# A refresh that fails (Odoo unreachable, a timeout) is retried this many
# times, this far apart, before the error is shown.
REFRESH_ATTEMPTS = 2
RETRY_SECONDS = 10

# The tab order on screen.
# The overview first: one card per question, each linking to the tab below
# that details it. (The delays tab is gone: the overview flags the tickets
# stuck in their stage; `dev_stats_report` still runs on its own.)
TABS = [("apercu", "Vue d'ensemble"), ("travail", "Tickets"),
        ("capacite", "Disponibilité"), ("temps", "Facturable & feuilles"),
        (dev_feuille.KEY, "Ma feuille de temps")]
TAB_KEYS = [k for k, _l in TABS]
# Tabs left out of the phone/tablet tab bar (app.css hides them there): the
# timesheet review is a wide editing table, and it only works on the Mac
# that runs the app anyway (dev_feuille).
DESKTOP_ONLY = {dev_feuille.KEY}

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]


def date_fr(date):
    return f"{JOURS[date.weekday()]} {date.day} {MOIS[date.month - 1]} {date.year}"


class NativeCache:
    """The data behind the tabs, one entry per key — e.g. ("temps", days,
    recent) or ("travail",) — so a filter combination is read from Odoo once
    and then served from memory.

    Loaders run in-process, each with its own XML-RPC connection — a
    ServerProxy holds a single HTTP connection and is not safe to share between
    threads. An entry older than two refresh periods is reloaded on the next
    request (a filter nobody has used since the last refresh); if that reload
    fails, the stale value is served rather than an error."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}          # key -> (monotonic time, value)

    def get(self, key, loader):
        """`loader(kw, cfg, today)` computes the value on a miss."""
        with self._lock:
            hit = self._items.get(key)
            if hit and time.monotonic() - hit[0] < 2 * REFRESH_MINUTES * 60:
                return hit[1]
            try:
                value = _run(loader)
            except Exception:  # noqa: BLE001
                if hit:
                    return hit[1]
                raise
            self._items[key] = (time.monotonic(), value)
            return value

    def replace(self, items):
        """Swap in a fresh set of entries at once — the refresh's result. Any
        other key (a filter combination) is dropped and reloads on demand."""
        now = time.monotonic()
        with self._lock:
            self._items = {key: (now, value) for key, value in items.items()}


def _run(loader):
    cfg = digest_config.load()
    kw = digest_config.connect(cfg)
    try:
        return loader(kw, cfg, datetime.date.today())
    finally:
        kw.close()


native = NativeCache()


# The periods each tab's picker offers (`dev_periods` keys): calendar ones
# first, then rolling ones. Tab 3 only takes periods that run to today — the
# billable rate and the sheet coverage are measured up to now.
TEMPS_PERIODS = ["month", "quarter", "year", "90d", "180d", "365d"]
RECENTS = {"30": 30, "60": 60, "90": 90}


def render_temps(request):
    """Tab 3, native: filters from the query string, data from the cache."""
    recent_default = str(dev_timesheet_report.RECENT_WINDOW)
    q = {"window": dev_periods.from_legacy(request.query_params.get("window"),
                                          TEMPS_PERIODS, "year"),
         "recent": request.query_params.get("recent", recent_default),
         "dev": request.query_params.get("dev", "")}
    if q["recent"] not in RECENTS:
        q["recent"] = recent_default
    recent = RECENTS[q["recent"]]
    ctx = native.get(("temps", q["window"], recent), _load_temps(q["window"], recent))
    vm = dev_timesheet_view.build(ctx, digest_config.load(),
                                  [q["dev"]] if q["dev"] else None)
    return templates.get_template("temps.html").render(
        q=q, periods=dev_periods.choices(TEMPS_PERIODS),
        recents=[(k, f"{k} jours") for k in RECENTS], **vm)


def _load_temps(period, recent):
    return lambda kw, cfg, today: dev_timesheet_report.gather(
        kw, cfg, today, None, recent, period=period)


def _load_capacite(back, fwd):
    return lambda kw, cfg, today: (
        dev_quickstart_report.gather(kw, cfg, today, fwd),
        dev_capacity_report.gather(kw, cfg, today, back, fwd))


def _load_travail(kw, cfg, today):
    """Tab 1's inputs — the same two calls the static tab makes."""
    return (dev_quickstart_report.gather(kw, cfg, today),
            dev_tasks_report.fetch(kw, cfg, "todo"))


def render_travail(request):
    q = {"kind": request.query_params.get("kind", "all"),
         "dev": request.query_params.get("dev", ""),
         "parked": request.query_params.get("parked") == "1"}
    qctx, todo = native.get(("travail",), _load_travail)
    vm = dev_travail_view.build(qctx, todo, digest_config.load(),
                                [q["dev"]] if q["dev"] else None, q["kind"])
    return templates.get_template("travail.html").render(q=q, **vm)


BACKS = {"4": 4, "8": 8, "12": 12}
FWDS = {"4": 4, "5": 5, "8": 8}


def render_capacite(request):
    """Tab 2 — the quickstart week and the capacity report, from the same
    gather() calls as the static tab (same 4 weeks back, same horizon)."""
    q = {"back": request.query_params.get("back", "4"),
         "fwd": request.query_params.get("fwd", str(dev_quickstart_report.WEEKS_FWD)),
         "dev": request.query_params.get("dev", "")}
    q["back"] = q["back"] if q["back"] in BACKS else "4"
    q["fwd"] = q["fwd"] if q["fwd"] in FWDS else str(dev_quickstart_report.WEEKS_FWD)
    back, fwd = BACKS[q["back"]], FWDS[q["fwd"]]
    qctx, cctx = native.get(("capacite", back, fwd), _load_capacite(back, fwd))
    vm = dev_capacite_view.build(qctx, cctx, digest_config.load(),
                                 [q["dev"]] if q["dev"] else None)
    return templates.get_template("capacite.html").render(
        q=q, fwds=[(k, f"{k} sem.") for k in FWDS], **vm)


def _load_repartition(period):
    return lambda kw, cfg, today: dev_repartition.gather(kw, cfg, today, period)


def render_repartition(request):
    """The "Répartition du temps" block of tab 2, swapped on its own when its
    period changes: its period is independent of the tab's weeks filter."""
    period = request.query_params.get("period", dev_repartition.DEFAULT_PERIOD)
    period = period if period in dev_repartition.PERIOD_KEYS else dev_repartition.DEFAULT_PERIOD
    dev = request.query_params.get("dev", "")
    ctx = native.get(("repartition", period), _load_repartition(period))
    vm = dev_repartition.view(ctx, [dev] if dev else None)
    return templates.get_template("_repartition.html").render(
        period=period, periods=dev_repartition.PERIODS, dev=dev,
        span=f"{date_fr(vm['start'])} → {date_fr(vm['end'])}", **vm)


# Tab key -> renderer.
def render_apercu(request):
    """Tab 0 — the overview, from the default view of the three other tabs
    (whole team, current year, default horizon): the same cache entries, so
    it costs no Odoo call once they are loaded."""
    cfg = digest_config.load()
    qctx, todo = native.get(("travail",), _load_travail)
    fwd, recent = dev_quickstart_report.WEEKS_FWD, dev_timesheet_report.RECENT_WINDOW
    cq, cc = native.get(("capacite", 4, fwd), _load_capacite(4, fwd))
    tctx = native.get(("temps", "year", recent), _load_temps("year", recent))
    vm = dev_apercu_view.build(dev_travail_view.build(qctx, todo, cfg),
                               dev_capacite_view.build(cq, cc, cfg),
                               dev_timesheet_view.build(tctx, cfg))
    return templates.get_template("apercu.html").render(**vm)


def render_feuille(request):
    """Tab 4 — my timesheet. Nothing cached: the page loads the day from its
    own API (dev_feuille), which reads local traces and Odoo on demand."""
    return templates.get_template("feuille.html").render(
        **dev_feuille.context(request, digest_config.RESYNC_PORT))


NATIVE = {"apercu": render_apercu, "travail": render_travail,
          "capacite": render_capacite, "temps": render_temps,
          dev_feuille.KEY: render_feuille}


def default_items():
    """The default view of every tab, loaded fresh — what a refresh swaps in.
    The overview has no entry of its own: it reads the three below."""
    travail = _run(_load_travail)
    fwd = dev_quickstart_report.WEEKS_FWD
    return {
        ("travail",): travail,
        ("capacite", 4, fwd): _run(_load_capacite(4, fwd)),
        ("temps", "year", dev_timesheet_report.RECENT_WINDOW):
            _run(_load_temps("year", dev_timesheet_report.RECENT_WINDOW)),
        ("repartition", dev_repartition.DEFAULT_PERIOD):
            _run(_load_repartition(dev_repartition.DEFAULT_PERIOD)),
    }


class Store:
    """When the data was last refreshed, and whether the last attempt failed.

    `_lock` serialises refreshes: a click on "Resynchroniser" during the
    timer's refresh waits for it and gets its result instead of reading Odoo a
    second time."""

    def __init__(self):
        self._lock = threading.Lock()
        self.built_at = None      # datetime of the last good refresh
        self.error = None         # str — last refresh's failure, None if fine
        self.building = False

    def build(self):
        """Refresh now, or wait for the refresh already running. True when the
        cache holds fresh data afterwards."""
        if not self._lock.acquire(blocking=False):
            with self._lock:
                return self.error is None
        try:
            self.building = True
            for attempt in range(1, REFRESH_ATTEMPTS + 1):
                try:
                    native.replace(default_items())
                except (Exception, SystemExit):  # noqa: BLE001 — connect() exits
                    self.error = traceback.format_exc()[-2000:]
                    if attempt < REFRESH_ATTEMPTS:
                        time.sleep(RETRY_SECONDS)
                    continue
                self.built_at = datetime.datetime.now()
                self.error = None
                return True
            _restart_if_resolver_stuck(self.error)
            return False
        finally:
            self.building = False
            self._lock.release()

    def build_async(self):
        threading.Thread(target=self.build, name="refresh", daemon=True).start()


def _restart_if_resolver_stuck(error):
    """Exit (launchd's KeepAlive restarts us) when this long-lived process can
    no longer resolve Odoo's host name but a fresh process can.

    Seen on the always-on home Mac: after a night-time network change the
    process kept failing with `gaierror` for hours while DNS worked fine
    everywhere else, and only a restart cured it. A genuine outage (no
    network) fails in the fresh process too, so it does not cause a restart
    loop — the last good figures stay on screen as before."""
    if not error or "gaierror" not in error:
        return
    import subprocess
    import urllib.parse
    try:
        host = urllib.parse.urlparse(digest_config.load()["url"]).hostname
        probe = subprocess.run(
            [sys.executable, "-c",
             f"import socket; socket.getaddrinfo({host!r}, 443)"],
            capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001
        return
    if probe.returncode == 0:
        print(f"{datetime.datetime.now():%Y-%m-%d %H:%M} résolution DNS bloquée "
              f"dans ce processus ({host}) mais OK ailleurs : redémarrage.",
              file=sys.stderr, flush=True)
        os._exit(75)


store = Store()


def refresher():
    """Refresh now, then every REFRESH_MINUTES. Never raises: a thread that dies
    silently leaves a page that stops updating with nothing to say why."""
    while True:
        try:
            store.build()
        except Exception:  # noqa: BLE001
            store.error = traceback.format_exc()[-2000:]
        time.sleep(REFRESH_MINUTES * 60)


_odoo = {"kw": None, "cfg": None}
_odoo_lock = threading.Lock()


def odoo():
    """One authenticated XML-RPC callable for the drill-down, made on first
    use. `reset_odoo()` drops it after a failure so the next click
    re-authenticates (a revoked key, a dropped connection)."""
    with _odoo_lock:
        if _odoo["kw"] is None:
            cfg = digest_config.load()
            _odoo["kw"], _odoo["cfg"] = digest_config.connect(cfg), cfg
        return _odoo["kw"], _odoo["cfg"]


def reset_odoo():
    with _odoo_lock:
        if _odoo["kw"] is not None:
            _odoo["kw"].close()
        _odoo["kw"] = None


@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=refresher, name="refresher", daemon=True).start()
    yield


app = FastAPI(title="DEV TSC", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(WEB, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(WEB, "templates"))
app.include_router(dev_feuille.router)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _asset_version():
    """Newest mtime under static/, appended to the asset URLs so an edited
    stylesheet is never served from the browser's cache."""
    folder = os.path.join(WEB, "static")
    return int(max(os.path.getmtime(os.path.join(folder, f)) for f in os.listdir(folder)))


def _page_ctx(request, active):
    return {"request": request, "tabs": TABS, "desktop_only": DESKTOP_ONLY,
            "active": active, "store": store,
            "today": date_fr(datetime.date.today()), "content_css": md_to_html.CSS,
            "asset_v": _asset_version()}


@app.get("/")
def index():
    return RedirectResponse(f"/{TAB_KEYS[0]}")


# Both of these have to sit above the `/{key}` catch-all at the bottom of the
# file, which would otherwise answer "Page inconnue" for them.
@app.api_route("/manifest.webmanifest", methods=["GET", "HEAD"], include_in_schema=False)
def manifest():
    """The PWA manifest — what makes Chrome and Safari offer to install the
    app. Served from the root so the URL survives a change of asset layout."""
    return FileResponse(os.path.join(WEB, "static", "manifest.webmanifest"),
                        media_type="application/manifest+json")


@app.api_route("/sw.js", methods=["GET", "HEAD"], include_in_schema=False)
def service_worker():
    """The service worker. It *must* be served from the root: a worker's scope
    cannot reach above its own URL, and this one controls every tab. Chrome
    also wants a registered worker with a fetch handler before it shows the
    install button on the desktop."""
    return FileResponse(os.path.join(WEB, "static", "sw.js"),
                        media_type="text/javascript",
                        headers={"Cache-Control": "no-cache",
                                 "Service-Worker-Allowed": "/"})


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    """The freshness badge in the top bar, polled by htmx."""
    return templates.TemplateResponse(request, "_status.html", _page_ctx(request, None))


@app.post("/resync", response_class=HTMLResponse)
def resync(request: Request):
    """The button: refresh, then let htmx reload the page on the same tab.
    A failure answers 200 with the status badge — htmx does not swap a 5xx,
    and the badge is where the error is shown."""
    if store.build():
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return templates.TemplateResponse(request, "_status.html", _page_ctx(request, None))


@app.post("/refresh", response_class=PlainTextResponse)
def refresh_async():
    """A foreground `/dev-app` run asks for fresh data: start a refresh in the
    background and answer at once, so its window opens without waiting."""
    store.build_async()
    return PlainTextResponse("started")


@app.get("/resync", response_class=PlainTextResponse)
def resync_legacy():
    """The button of an `app.html` saved before the web app — still answers."""
    return PlainTextResponse("ok" if store.build() else (store.error or "échec"),
                             status_code=200 if store.error is None else 502,
                             headers={"Access-Control-Allow-Origin": "*"})


@app.get("/repartition", response_class=HTMLResponse)
def repartition(request: Request):
    try:
        return HTMLResponse(render_repartition(request))
    except (Exception, SystemExit):  # noqa: BLE001 — connect() exits
        traceback.print_exc()
        return HTMLResponse('<section class="block" id="repartition"><div class="callout err-box">'
                            "<b>La répartition du temps n'a pas pu lire Odoo.</b></div></section>")


@app.get("/lines", response_class=HTMLResponse)
def lines(request: Request):
    try:
        kw, cfg = odoo()
        data = dev_timesheet_lines.context(kw, cfg, request.url.query)
    except Exception as exc:  # noqa: BLE001
        reset_odoo()
        traceback.print_exc()
        data = {"error": str(exc)}
    template = "_lines.html" if _is_htmx(request) else "lines.html"
    return templates.TemplateResponse(request, template,
                                      {**_page_ctx(request, None), **data})


@app.get("/delais", include_in_schema=False)
def delais_gone():
    """The delays tab was folded into the overview — old bookmarks land there."""
    return RedirectResponse("/apercu")


@app.get("/{key}", response_class=HTMLResponse)
def tab(request: Request, key: str):
    if key not in TAB_KEYS:
        return HTMLResponse("Page inconnue", status_code=404)
    ctx = _page_ctx(request, key)
    try:
        ctx["content"] = NATIVE[key](request)
    except (Exception, SystemExit):  # noqa: BLE001 — connect() exits
        traceback.print_exc()
        ctx["tab_error"] = traceback.format_exc()[-1500:]
    template = "_pane.html" if _is_htmx(request) else "index.html"
    return templates.TemplateResponse(request, template, ctx)


if __name__ == "__main__":
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser(description="L'application DEV TSC, en local.")
    ap.add_argument("--port", type=int, default=digest_config.RESYNC_PORT)
    uvicorn.run(app, host=HOST, port=ap.parse_args().port, log_level="warning")
