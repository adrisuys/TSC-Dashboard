# DEV TSC web app on the home Mac: agent runbook

> **Which install is this?** Two different set-ups share this folder:
>
> - **The always-on home Mac**, reached from the iPhone over Tailscale:
>   follow sections 0–4 below.
> - **The human's work laptop**, to use the "Ma feuille de temps" tab (it
>   builds the timesheet from *this* machine's Claude Code sessions, Chrome
>   history and git commits, so it must run where the work happens): skip to
>   [Work laptop](#work-laptop-ma-feuille-de-temps) at the end. No Tailscale there.
>
> If the human just said "install via SETUP.md" on their work laptop (a Mac
> with `~/Desktop/TSC`), it is the second one. When in doubt, ask.

Goal: run the DEV TSC web app (FastAPI, four tabs read live from the
production Odoo) on this always-on Mac, and reach it from the iPhone over
Tailscale.

The app listens on `127.0.0.1:8734` with no login. **Tailscale Serve is the
only way in**: never bind it to `0.0.0.0` and never use `tailscale funnel`
(that would put it on the public internet).

This folder holds code only. No credentials ship with it.

## 0. Check before you start

```bash
python3 --version                      # 3.9+ is enough; Homebrew python is preferred if present
lsof -nP -iTCP:8734 -sTCP:LISTEN       # must print nothing
tailscale serve status                 # note what is already served (recipes, Strava…)
curl -sI https://www.tsc-experts.com | head -1   # the Mac must reach prod Odoo
```

If `tailscale` is not on the PATH, use
`/Applications/Tailscale.app/Contents/MacOS/Tailscale`.

**If port 8734 is taken**, pick a free port and set `RESYNC_PORT` in
`app/digest_config.py` to it before step 2. Use that port everywhere below.

## 1. Credentials (the human does this)

The Odoo API key is handled by the human, not by you: never read, print or
copy it. The human copies their key file (`api_key_dashboard.txt`) next to
this folder themselves, then runs:

```bash
python3 app/setup_credentials.py --login adrien@tsc-experts.com --api-key-stdin < api_key_dashboard.txt && rm api_key_dashboard.txt
```

(or plain `python3 app/setup_credentials.py` to type the key at a prompt).
It checks the key against Odoo, then writes
`~/.config/tsc-dev-digest/config.json` (mode 0600). Confirm with
`python3 app/setup_credentials.py --check`.

## 2. Install the web app

```bash
python3 app/install_schedule.py
```

This copies `app/` to `~/Library/Application Support/tsc-dev-digest/runner/`
(outside the TCC-protected Desktop/Documents/Downloads folders, so launchd can
run it), creates a venv next to it with fastapi, uvicorn and jinja2, and
installs the launchd job `com.tsc.dev-digest.resync-server` (RunAtLoad +
KeepAlive).

Check it:

```bash
python3 app/install_schedule.py --status     # state = running
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8734/travail   # 200
```

The first load reads Odoo, which takes a few seconds. The log is in
`~/Library/Application Support/tsc-dev-digest/launchd.log`.

## 3. Expose it on the tailnet

Serve it on its own HTTPS port so it doesn't clash with what is already
served on 443:

```bash
tailscale serve --bg --https=8734 http://127.0.0.1:8734
tailscale serve status
```

The URL is `https://<this-mac>.<tailnet>.ts.net:8734/`. `--bg` makes it
persist across reboots. If `tailscale serve` asks to enable HTTPS
certificates for the tailnet, follow the link it prints (the human approves it
in the Tailscale admin console).

## 4. Hand over

Give the human the URL. The app is installable (manifest + service worker at
`/manifest.webmanifest` and `/sw.js`):

- **iPhone**: open it in Safari, then Share → Add to Home Screen.
- **Mac, Chrome**: use the omnibox install icon, which Chrome shows once it
  has read the manifest. Chrome only offers it over the Tailscale HTTPS URL or
  `http://127.0.0.1:8734` — both are secure contexts.

Installed, it opens without browser chrome and keeps its own window. Offline
it shows a "hors ligne" card rather than stale figures: every tab is read live
from Odoo, so only the stylesheet and scripts are cached.

Icons live in `app/webapp/static/icons/`; regenerate them with
`python3 app/webapp/make_icons.py` after changing the colour or the mark.

Check from the phone (or another tailnet device) that:
- the four tabs load (Le travail · Capacité · Feuilles de temps · Délais);
- in Feuilles de temps, clicking an hours figure opens the side drawer with
  the timesheet lines. That's the patched relative link. If it fails, the
  copy wasn't built with `build_dev_app_portable.py`.

## Updating later

The human rebuilds the package on the work laptop
(`python3 utils/scripts/build_dev_app_portable.py`) and copies the new
`tsc-dev-app-portable/` here. Then:

```bash
python3 app/install_schedule.py      # recopies runner/, refreshes the venv, restarts the job
```

The credentials and the Tailscale Serve config are untouched by an update.

## Removing it

```bash
python3 app/install_schedule.py --uninstall
tailscale serve --https=8734 off
```

---

## Work laptop (Ma feuille de temps)

Goal: run the same app on the human's work laptop, at
`http://127.0.0.1:8734`, so the **Ma feuille de temps** tab can read this
machine's traces and push the ticked lines to Odoo. The other tabs work there
too. Nothing is exposed on the network: **do not run `tailscale serve` here.**

The tab's engine is `app/timesheet/` (the `tsc-timesheet` plugin's scripts,
unchanged); its config and day files are the plugin's own
(`~/.config/tsc-timesheet/`, `~/Library/Application Support/tsc-timesheet/`),
so `/timesheet` in Claude Code and the tab share the same days.

### L0. Check before you start

```bash
python3 --version
lsof -nP -iTCP:8734 -sTCP:LISTEN
launchctl print gui/$(id -u)/com.tsc.dev-digest.resync-server 2>/dev/null | grep -E 'state|program'
```

- **Something listens on 8734 and it is `com.tsc.dev-digest.resync-server`**:
  that is the dev app already installed from the `tsc-dev-digest` plugin
  (`/dev-app`). The install below **replaces it in place** (same job, same
  runner folder), with this version, which has one more tab. Tell the human
  before going on, and see L5: a later reinstall from the plugin would put the
  old version back.
- **Something else** listens on 8734: stop and ask the human.

### L1. Credentials (the human does this)

Check only that the file exists, **never read or print it**:

```bash
test -f ~/.config/tsc-dev-digest/config.json && echo present || echo missing
```

`present` → nothing to do (the timesheet reuses that key). `missing` → the
human runs, themselves:

```bash
python3 app/setup_credentials.py --login adrien@tsc-experts.com
```

### L2. Install

```bash
python3 app/install_schedule.py
python3 app/install_schedule.py --status                                   # running
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8734/feuille     # 200
```

### L3. Full Disk Access (the human does this, you prepare it)

The app runs under launchd, so macOS asks for its *own* permission to read
Chrome's history and the repos under `~/Desktop/TSC`. Without it the tab still
works from the Claude Code sessions alone; Chrome and git show as unavailable.

Find the exact binary to authorise (the running job's process, not the venv
symlink):

```bash
PID=$(launchctl print gui/$(id -u)/com.tsc.dev-digest.resync-server | awk '/pid = /{print $3}')
ps -o comm= -p "$PID"
```

Give the human that path and these steps: System Settings → Privacy &
Security → Full Disk Access → **+** → ⌘⇧G → paste the path → Open → make sure
the switch is on. Then restart the app:

```bash
launchctl kickstart -k gui/$(id -u)/com.tsc.dev-digest.resync-server
```

### L4. Hand over

The human opens **http://127.0.0.1:8734/feuille** in Chrome on this laptop
(installable as an app from Chrome's omnibox icon), picks yesterday, clicks
**Reconstruire depuis les traces**, and checks that the three badges
(claude · chrome · git) are green and that ticket lines appear. Do not open
the API yourself and do not push anything: the push is the human's act.

If the TSC workspace is not `~/Desktop/TSC`, or a repo lands in the wrong
project, the mapping lives in `~/.config/tsc-timesheet/config.json`: run
`/timesheet-setup` if the plugin is installed, otherwise ask the human.

### L5. Bring the changes back to the plugin source (ask the human first)

This folder is a package built from the `tsc-dev-digest` plugin source on this
laptop (`utils/scripts/build_dev_app_portable.py`). The changes below were made
in the package only, so the next rebuild or plugin reinstall loses them unless
they are ported to the source. Offer to do it; port the changes, don't copy
the files over blindly (the build patches some files).

- New: `app/dev_feuille.py`, `app/timesheet/` (engine + tests + README),
  `app/webapp/templates/feuille.html`, `app/webapp/static/feuille.js`.
- `app/dev_webapp.py`: `TABS` gets `("feuille", "Ma feuille de temps")`,
  `DESKTOP_ONLY`, `render_feuille`, `NATIVE` entry, `desktop_only` in
  `_page_ctx`, `app.include_router(dev_feuille.router)`.
- `app/install_schedule.py`: `sync_runner()` also copies `timesheet/`.
- `app/webapp/templates/_nav.html` (icon, short label, `desktop-only` class),
  `base.html` (loads `feuille.js`), `app.css` (section "Ma feuille de temps",
  plus the `.ov-card`/`.ov-ahead` fix of the overview's "Dispo à venir" grid),
  `apercu.html` (that grid: "renfort" as one cell spanning the weeks).

`git log` in this folder shows the same changes as commits.

### Removing it

```bash
python3 app/install_schedule.py --uninstall
```
