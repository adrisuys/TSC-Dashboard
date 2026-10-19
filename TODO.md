# DEV TSC on the home Mac: your checklist

1. [x] Dedicated Odoo API key for this Mac: `utils/api_key_dashboard.txt`.
2. [ ] AirDrop `tsc-dev-app-portable.zip` AND `api_key_dashboard.txt` to the
       home Mac; unzip, and put the key file inside the unzipped folder.
3. [ ] Open Claude Code in the unzipped folder: "follow SETUP.md".
4. [ ] When asked, run the `setup_credentials.py … < api_key_dashboard.txt && rm …`
       line from SETUP.md step 1 yourself. It stores the key and deletes the file.
5. [ ] If Tailscale asks to enable HTTPS certificates, approve it in the admin console.
6. [ ] On the iPhone: open `https://<home-mac>.<tailnet>.ts.net:8734/` in
       Safari → Share → Add to Home Screen.
7. [ ] Tap an hours figure in "Feuilles de temps": the lines drawer must open.

Anyone on your tailnet can see the whole dashboard (colleagues' timesheets
included), so keep the tailnet limited to your own devices.
