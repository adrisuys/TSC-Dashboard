#!/usr/bin/env python3
"""Build the DEV TSC digest: TODO + per-developer workload.

Two sections, deliberately: TODO is the work nobody owns yet, WORKLOAD is the
work that is owned and how much of it is left. The old IN PROGRESS section sat
between them and repeated what WORKLOAD already says per developer.

    python3 digest.py                        # Markdown on stdout (what /dev-digest uses)
    python3 digest.py --format html --open   # also render an HTML page and open it
    python3 digest.py --notify               # what the scheduled job runs

Every run overwrites `latest.md` in the output directory, whatever `--format`
asks for on stdout; HTML is only ever an extra rendering of that same digest,
saved as `latest.html` beside it. Pass `--no-save` for a throwaway run.

Network failures are retried (Wi-Fi may still be reconnecting right after a
wake); a wrong login or a missing tag is a configuration error and fails at
once rather than burning the retries. Either way the digest is still written,
with the error in place of the tables, so a scheduled run never leaves a stale
file looking current.
"""
import argparse
import io
import os
import subprocess
import sys
import time
import traceback
import urllib.request
import webbrowser
from contextlib import redirect_stdout
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_tasks_report  # noqa: E402
import dev_workload_report  # noqa: E402
import md_to_html  # noqa: E402


def output_dir():
    """Where scheduled runs keep their digests — deliberately NOT under
    ~/Desktop or ~/Documents, which macOS gates behind Full Disk Access and
    which a launchd job therefore cannot write to."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/tsc-dev-digest")
    return os.path.expanduser("~/.local/share/tsc-dev-digest")


class ConfigError(Exception):
    """Non-retryable: bad credentials, missing tag, unknown developer."""


def run_reports():
    """Return the report sections as one Markdown string."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            dev_tasks_report.main(["todo"])
            print()
            dev_workload_report.main([])
    except SystemExit as exc:
        # The reports call sys.exit(<message>) for anything a retry cannot fix.
        raise ConfigError(str(exc.code)) from exc
    return buf.getvalue()


def build(retries=3, delay=30):
    """(status, body) — the digest sections, or the error explaining their absence.

    status is "ok", "config" (retrying cannot help) or "network"."""
    last = ""
    for attempt in range(1, retries + 1):
        try:
            return "ok", run_reports()
        except ConfigError as exc:
            return "config", str(exc)
        except Exception:  # noqa: BLE001 — network/XML-RPC noise, worth a retry
            last = traceback.format_exc()
            if attempt < retries:
                time.sleep(delay)
    return "network", last


def failure_block(status, body, stamp, retries):
    if status == "config":
        lead = [
            f"> ⚠️ **The digest is not configured yet** (checked {stamp}).",
            "> Odoo answered, but the digest could not run. This will not fix "
            "itself — see the detail below.",
        ]
    else:
        lead = [
            f"> ⚠️ **Could not reach Odoo** after {retries} attempt(s) (checked {stamp}).",
            "> Most likely no internet connection. The next scheduled run will try again.",
        ]
    return "\n".join(lead + [
        "",
        "<details><summary>Error detail</summary>",
        "",
        "```",
        body.rstrip(),
        "```",
        "",
        "</details>",
    ])


def notify(status, what="digest"):
    """`what` names the report in the notification — the dashboard shares this
    code path and a Mac saying "digest ready" for the dashboard is a small lie
    that costs a click to resolve."""
    if sys.platform != "darwin":
        return
    if status == "ok":
        text, title, sound = f"DEV TSC {what} ready", f"Dev {what}", "Glass"
    elif status == "config":
        text, title, sound = ("Not configured — run /dev-digest-setup",
                              f"Dev {what} failed", "Basso")
    else:
        text, title, sound = ("Could not reach Odoo — will retry next run",
                              f"Dev {what} failed", "Basso")
    os.system("osascript -e 'display notification \"%s\" with title \"%s\" "
              "sound name \"%s\"' >/dev/null 2>&1" % (text, title, sound))


def notify_failure_once(status, what="digest"):
    """Notify when the job ENTERS a failed state, not on every failed run.

    A run every half hour turns "Odoo is unreachable" into nineteen identical
    Basso alerts for one outage, which trains you to dismiss the one that
    mattered. Only a change of state is worth a sound, so the last status is
    kept in a dotfile beside the saved pages. A different kind of failure
    (config after network) does notify again — it is new information.

    Recovery is deliberately silent: the ask was to hear about failures only,
    and the page's own timestamp already says it is current again.

    If the state file cannot be read or written the run notifies anyway —
    over-notifying is a nuisance, going quiet about an outage is a bug.
    Returns True when a notification was posted."""
    path = os.path.join(output_dir(), f".last_status_{what}")
    try:
        with open(path, encoding="utf-8") as fh:
            previous = fh.read().strip()
    except OSError:
        previous = ""
    try:
        os.makedirs(output_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(status)
    except OSError:
        pass
    if status == "ok" or previous == status:
        return False
    notify(status, what)
    return True


CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def chrome_reuse_window(urls, focus=True, goto=None):
    """True if a Chrome window already shows the digest — reloaded in place.

    Chrome opens a NEW window for every `--app=` invocation and the digest is
    rebuilt many times a day, so without this the desk fills up with stale
    copies of the same page. AppleScript is asked first whether Chrome is
    already running: `is running` does not launch it, whereas `activate` would.
    Any failure (Automation permission refused, Chrome busy) returns False and
    the caller just opens a fresh window.

    `focus=False` reloads the tab and stops there — no `set index`, no
    `activate`. That is what a page rebuilt every half hour needs: raising the
    window is welcome once in the morning and an interruption by 14:30, and a
    background reload leaves the page fresh for whenever it is next looked at.
    Chrome does reload a tab in an unfocused window, so nothing is deferred.

    `goto` sends the tab to that URL instead of reloading it where it stands —
    the web app opened on purpose starts on its home page (the overview), not
    on whichever tab the window was left on. Only with `focus`: a background
    refresh must never pull the reader off the tab they are on."""
    action = (f'set URL of t to "{goto}"' if goto and focus else "reload")
    tests = " or ".join('(u starts with "%s")' % u for u in sorted(urls))
    raise_window = """
                    try
                        set index of w to 1
                    end try
                    activate""" if focus else ""
    script = f"""
    if application "Google Chrome" is not running then return "no"
    tell application "Google Chrome"
        repeat with w in windows
            repeat with t in tabs of w
                set u to URL of t
                if {tests} then
                    tell t to {action}{raise_window}
                    return "yes"
                end if
            end repeat
        end repeat
    end tell
    return "no"
    """
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.stdout.strip() == "yes"


def open_url_app(url, focus=True):
    """Show an http page (the local web app) in a Chrome app window: the one
    already showing it — any page of it, matched on the URL prefix — is sent
    back to `url` (reloaded in place when `focus=False`), or a new one opens.
    Same contract as `open_in_browser` for `focus=False`."""
    if sys.platform == "darwin" and os.path.exists(CHROME):
        if chrome_reuse_window({url}, focus=focus, goto=url):
            return True
        if not focus:
            return False
        subprocess.Popen([CHROME, f"--app={url}"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    if focus:
        webbrowser.open(url)
    return focus


def chrome_open_tab(url):
    """Open `url` as a new tab in a Chrome window that is already on screen.

    Only a window holding at least two tabs qualifies. That is the heuristic
    that keeps the digest's own `--app=` window safe: an app window is
    chrome-less and always reports exactly one tab, and pushing a tab into it
    would produce a tab strip the window was created not to have. When no such
    window exists the caller opens a fresh one instead — a new window is a
    harmless outcome, hijacking the digest window is not."""
    script = f"""
    if application "Google Chrome" is not running then return "no"
    tell application "Google Chrome"
        repeat with w in windows
            if (count of tabs of w) > 1 then
                tell w to make new tab with properties {{URL:"{url}"}}
                try
                    set index of w to 1
                end try
                activate
                return "yes"
            end if
        end repeat
    end tell
    return "no"
    """
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.stdout.strip() == "yes"


def open_in_browser(path, app=True, focus=True):
    """Show a report in Chrome, on the normal profile.

    `app=True` (the digest) gives a chrome-less window — no tabs, no address
    bar — which is what a morning digest wants to be. `app=False` (the capacity
    report) lands as an ordinary tab: in a Chrome window that is already open
    when there is one, in a new normal window otherwise, so the page keeps its
    tab strip and address bar and sits alongside the Odoo tabs it is read next
    to. Either way links clicked inside it open as ordinary Chrome tabs
    and the system default browser is left alone — unlike `open -a <some other
    browser>`, which hijacks the links too. Falls back to the default browser
    when Chrome is not installed.

    `focus=False` is refresh-only: reload the window if it is already on
    screen, otherwise do nothing at all. Opening a window is itself a focus
    steal, so a job that runs unattended every half hour must not do it — the
    window is opened once, by hand or by a foreground run, and from then on the
    schedule only keeps it current. Returns True when a window was reloaded or
    opened.

    Two URL spellings are matched because Chrome reports tab URLs
    percent-encoded ("Application%20Support") while `file://` + a raw path is
    what `webbrowser` and the printed path use."""
    encoded = "file://" + urllib.request.pathname2url(path)
    plain = f"file://{path}"
    if sys.platform == "darwin" and os.path.exists(CHROME):
        if chrome_reuse_window({encoded, plain}, focus=focus):
            return True
        if not focus:
            return False
        # A browsing page belongs in the browsing window the user already has
        # open; only fall through to a window of its own when there is none.
        if not app and chrome_open_tab(encoded):
            return True
        # Detached: Chrome must outlive this script, including under launchd.
        flag = f"--app={encoded}" if app else "--new-window"
        argv = [CHROME, flag] if app else [CHROME, flag, encoded]
        subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    webbrowser.open(plain)
    return True


def main():
    ap = argparse.ArgumentParser(description="Build the DEV TSC digest.")
    ap.add_argument("--format", choices=["md", "html"], default="md",
                    help="output format (default: md)")
    ap.add_argument("--save", action="store_true",
                    help=argparse.SUPPRESS)  # kept for older callers; saving is the default
    ap.add_argument("--no-save", dest="save_files", action="store_false",
                    help="do not write the digest to the output directory")
    ap.add_argument("--out-dir", default=output_dir(),
                    help="directory for saved digests")
    ap.add_argument("--open", dest="open_it", action="store_true",
                    help="open the saved digest in its own Chrome window")
    ap.add_argument("--notify", action="store_true",
                    help="post a macOS notification when done")
    ap.add_argument("--quiet", action="store_true",
                    help="do not echo the digest on stdout")
    ap.add_argument("--retries", type=int, default=3,
                    help="attempts before giving up (default: 3)")
    ap.add_argument("--retry-delay", type=int, default=30,
                    help="seconds between attempts (default: 30)")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%A %d %B %Y, %H:%M")
    status, body = build(args.retries, args.retry_delay)

    markdown = f"# DEV TSC — Digest — {stamp}\n\n"
    markdown += body if status == "ok" else failure_block(status, body, stamp,
                                                          args.retries)

    text = markdown if args.format == "md" else md_to_html.render(
        markdown, title=f"DEV TSC — Digest — {stamp}")

    if not args.quiet:
        print(text)

    if args.save_files:
        os.makedirs(args.out_dir, exist_ok=True)
        # One file per format, overwritten every run. No dated copies: the digest
        # is regenerated several times a day, so a dated file only ever held the
        # last run of that day — not a snapshot worth keeping. If per-day history
        # is ever wanted, take it from one scheduled run rather than from here.
        renderings = {"md": markdown}
        if args.format != "md":
            renderings[args.format] = text
        saved = {}
        for ext, content in renderings.items():
            saved[ext] = os.path.join(args.out_dir, f"latest.{ext}")
            with open(saved[ext], "w", encoding="utf-8") as fh:
                fh.write(content)
        path = saved[args.format]
        if args.quiet:
            print(path)
        else:
            # stderr, so the Markdown on stdout stays pasteable as-is.
            print(f"Saved: {' and '.join(sorted(saved.values()))}", file=sys.stderr)
        if args.open_it:
            open_in_browser(path)

    if args.notify:
        notify(status)
    # Distinct codes so a caller can tell "fix your setup" from "try later".
    return {"ok": 0, "network": 1, "config": 2}[status]


if __name__ == "__main__":
    sys.exit(main())
