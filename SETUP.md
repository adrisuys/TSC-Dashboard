# DEV TSC web app on the home Mac: agent runbook

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
