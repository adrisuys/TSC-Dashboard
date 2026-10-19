#!/usr/bin/env python3
"""One-time credential setup for the DEV TSC digest.

    python3 setup_credentials.py                       # prompts (key is not echoed)
    python3 setup_credentials.py --login a@b.com --api-key-stdin < key.txt
    python3 setup_credentials.py --check               # test the stored credentials

The key is verified against Odoo before anything is written, so a typo fails
here rather than at 08:00 on a Monday. It is stored in
~/.config/tsc-dev-digest/config.json with mode 0600 — never in the plugin, and
never as a command-line argument unless you ask for that explicitly.
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import digest_config  # noqa: E402

HOW_TO_GET_A_KEY = """\
To create an Odoo API key:
  1. Open {url} and log in.
  2. Click your avatar (top right) -> My Profile -> Account Security tab.
  3. New API Key -> name it "dev digest" -> confirm with your password.
  4. Copy the key immediately — Odoo shows it only once.
"""


def main():
    ap = argparse.ArgumentParser(description="Configure the DEV TSC digest credentials.")
    ap.add_argument("--login", help="your Odoo login (email)")
    ap.add_argument("--api-key", help="API key (avoid: lands in shell history)")
    ap.add_argument("--api-key-stdin", action="store_true",
                    help="read the API key from stdin")
    ap.add_argument("--url", help="override the Odoo URL")
    ap.add_argument("--db", help="override the database name")
    ap.add_argument("--check", action="store_true",
                    help="only verify the stored credentials")
    args = ap.parse_args()

    cfg = digest_config.load()

    if args.check:
        digest_config.require_credentials(cfg)
        uid = digest_config.authenticate(cfg)
        if not uid:
            sys.exit(f"✗ Rejected: {cfg['login']} on {cfg['db']} ({cfg['url']}).")
        print(f"✓ {cfg['login']} authenticates on {cfg['db']} (uid {uid}).")
        return

    if args.url:
        cfg["url"] = args.url
    if args.db:
        cfg["db"] = args.db

    print(f"Odoo instance: {cfg['url']}  (database {cfg['db']})\n")

    login = args.login or cfg.get("login")
    if not args.login:
        prompt = f"Odoo login [{login}]: " if login else "Odoo login (email): "
        entered = input(prompt).strip() if sys.stdin.isatty() else ""
        login = entered or login
    if not login:
        sys.exit("A login is required.")
    cfg["login"] = login

    if args.api_key_stdin:
        key = sys.stdin.read().strip()
    elif args.api_key:
        key = args.api_key.strip()
    elif sys.stdin.isatty():
        print()
        print(HOW_TO_GET_A_KEY.format(url=cfg["url"]))
        key = getpass.getpass("API key (not echoed): ").strip()
    else:
        sys.exit("No API key supplied. Use --api-key-stdin, or run this in a terminal.")
    if not key:
        sys.exit("An API key is required.")
    cfg["api_key"] = key

    print("\nVerifying…")
    try:
        uid = digest_config.authenticate(cfg)
    except Exception as exc:  # noqa: BLE001 — surface the network error plainly
        sys.exit(f"✗ Could not reach {cfg['url']}: {exc}")
    if not uid:
        sys.exit(f"✗ Odoo rejected {cfg['login']}. Check the login and that the key "
                 f"was copied in full, then run this again.")

    path = digest_config.save(cfg)
    print(f"✓ Authenticated as {cfg['login']} (uid {uid}).")
    print(f"✓ Saved to {path} (owner-only).")
    print("\nNext: run the digest with  /dev-digest")


if __name__ == "__main__":
    main()
