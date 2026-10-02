"""JS-rendered sites (React/Vite/Wix/Squarespace/etc).

Some sites ship an empty shell (`<div id="root"></div>`) and build the page in
the browser. Grading the shell says "no phone, no H1, no form" about a site that
has all of them — a wrong report we'd hand a prospect. So when the raw HTML is
a shell we load it in headless Chrome and grade the rendered DOM instead.
If Chrome can't render it, we say so rather than invent findings.
"""

from __future__ import annotations

import subprocess

from bs4 import BeautifulSoup

from .pdfgen import find_chrome

SHELL_TEXT_MAX = 250       # visible chars in raw HTML below this = likely a shell
ROOT_IDS = ("root", "app", "__next", "__nuxt", "svelte", "q-app")


def looks_like_shell(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    body = soup.body
    text = (body.get_text(" ", strip=True) if body else "")
    if len(text) >= SHELL_TEXT_MAX:
        return False
    has_root = any(soup.find(id=i) for i in ROOT_IDS)
    raw_scripts = "<script" in html.lower()
    return has_root or (raw_scripts and len(text) < 80)


def render_html(url: str, timeout: int = 40) -> str | None:
    """Return the DOM after JavaScript has run, or None if Chrome can't."""
    chrome = find_chrome()
    if not chrome:
        return None
    cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--disable-crash-reporter", "--disable-extensions",
           "--virtual-time-budget=9000", "--dump-dom", url]
    try:
        run = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception:
        return None
    out = (run.stdout or b"").decode("utf-8", errors="replace")
    return out if len(out) > 200 else None
