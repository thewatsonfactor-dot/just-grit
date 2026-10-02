# Ops

- `install.sh` — once. Installs three launchd jobs under your user: the app (restarts itself if it dies, starts at login), a 3:15 am backup, a 5-minute watchdog. Re-run any time.
- `backup.sh` — every workspace database → `backups/nightly/*.db.gz`, 30 days kept. Run by hand any time.
- `release.sh <bundle.tgz>` — install a release: snapshots the files it replaces (and the databases) into `backups/releases/<stamp>/`, runs the tests in the bundle plus login and start, restarts, checks `/health`. Rolls itself back if either fails. `release.sh rollback` puts the previous release back; `release.sh list` shows them.
- `watchdog.py` — texts the owner once when `/health` isn't 200 (then every two hours until it is), and once when it's back. Uses the Telnyx key and number from Setup; the owner's cell is `alert_phone` in settings, falling back to the rep number, then the business phone.
- Restart by hand: `launchctl kickstart -k gui/$(id -u)/com.justgrit.app`. Logs: `uvicorn.log`, `backups/backup.log`, `backups/watchdog.log`.
