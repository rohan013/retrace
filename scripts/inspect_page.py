#!/usr/bin/env python3
"""Dump a rendered page as structured text — no screenshot, ever.

Lets an agent (or a human on a headless box) check that a UI change actually
did what it should, without paying image tokens for a screenshot. Reads the
DOM instead: for each --select selector, the block/lane geometry
(getBoundingClientRect), the resolved --accent colour (how subject colour is
actually applied — see static/js/track.js), dataset attributes and trimmed
text. Also captures browser console output and JS errors, which a screenshot
would never show at all.

Point it at any running instance — a throwaway dev server on its own port and
DB is the usual choice so iterating doesn't disturb the live service:

    DB_PATH=data/dev.db PORT=8421 INGEST_TOKEN=dev-token \\
        .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8421 &
    INGEST_TOKEN=dev-token .venv/bin/python scripts/synth_day.py --url http://127.0.0.1:8421

    scripts/inspect_page.py --url http://127.0.0.1:8421/ \\
        --fill "#date=2026-06-03" --select ".block" --select ".place-band"
"""

import argparse
import json
import sys

from playwright.sync_api import sync_playwright

DEFAULT_SELECTORS = [".block"]
TEXT_LIMIT = 200


def dump_elements(page, selector):
    return page.eval_on_selector_all(
        selector,
        """(nodes, limit) => nodes.map((n) => {
            const rect = n.getBoundingClientRect();
            const style = getComputedStyle(n);
            return {
                tag: n.tagName.toLowerCase(),
                id: n.id || null,
                className: n.className || null,
                dataset: { ...n.dataset },
                text: (n.textContent || "").trim().slice(0, limit),
                rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                accent: style.getPropertyValue("--accent").trim() || null,
            };
        })""",
        TEXT_LIMIT,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://127.0.0.1:8421/")
    parser.add_argument(
        "--fill",
        action="append",
        default=[],
        metavar="SELECTOR=VALUE",
        help="fill a form field, e.g. an input#date, before inspecting (repeatable)",
    )
    parser.add_argument(
        "--click",
        action="append",
        default=[],
        metavar="SELECTOR",
        help="click an element before inspecting, e.g. a zoom preset button (repeatable)",
    )
    parser.add_argument(
        "--wait", metavar="SELECTOR", help="wait for a selector to appear before inspecting"
    )
    parser.add_argument(
        "--select",
        action="append",
        dest="selectors",
        default=[],
        metavar="SELECTOR",
        help=f"CSS selector to dump (repeatable, default {DEFAULT_SELECTORS})",
    )
    parser.add_argument("--timeout", type=int, default=10_000, help="navigation/wait timeout, ms")
    args = parser.parse_args()
    selectors = args.selectors or DEFAULT_SELECTORS

    console, errors = [], []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.on("console", lambda msg: console.append(f"{msg.type}: {msg.text}"))
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        page.goto(args.url, timeout=args.timeout, wait_until="networkidle")

        for action in args.fill:
            selector, _, value = action.partition("=")
            page.locator(selector).fill(value, timeout=args.timeout)
            page.wait_for_load_state("networkidle", timeout=args.timeout)

        for selector in args.click:
            page.locator(selector).click(timeout=args.timeout)
            page.wait_for_load_state("networkidle", timeout=args.timeout)

        if args.wait:
            page.wait_for_selector(args.wait, timeout=args.timeout)

        elements = {selector: dump_elements(page, selector) for selector in selectors}

        browser.close()

    json.dump({"console": console, "errors": errors, "elements": elements}, sys.stdout, indent=2)
    print()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
