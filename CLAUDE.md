retrace is a self-hosted replacement for Google Maps Timeline: it keeps every raw location fix forever and derives stays, trips, and places from them as rebuildable layers rather than a lossy, opaque summary.

Read README.md before making any changes to this codebase. It explains the project's design and rationale (why fixes are kept forever, how derived layers are rebuilt, the layout of app/).

To visually verify a UI change, `scripts/inspect_page.py` drives headless Chromium (Python `playwright`, no Node) and dumps the rendered page as text — no screenshots. See README's "Browser inspection" section.
