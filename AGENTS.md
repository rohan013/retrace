retrace is a self-hosted replacement for Google Maps Timeline: it keeps every raw location fix forever and derives stays, trips, and places from them as rebuildable layers rather than a lossy, opaque summary.

Read README.md before making reading/writing any other file on this codebase. It explains the project's design and rationale (why fixes are kept forever, how derived layers are rebuilt, the layout of app/).

To visually verify a UI change, `scripts/inspect_page.py` drives headless Chromium (Python `playwright`, no Node) and dumps the rendered page as text — no screenshots. See README's "Browser inspection" section.

The live instance on this machine runs as the systemd unit `retrace.service`, with `OnFailure=` wired to send an alert (see README's "Alerts" section). To pick up a code change, restart it with `sudo systemctl restart retrace` — passwordless sudo is scoped to exactly that command. Never kill the process by PID and relaunch it manually: systemd's own auto-restart will then fight over the port with the orphaned process, failing repeatedly and firing the failure alert on every attempt.
