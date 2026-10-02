"""HTML → PDF via headless Chrome.

No Python PDF library can render the report's real CSS the way a browser
does, and every machine this runs on (the Mac Studio, a customer demo
laptop) already has Chrome. So we print through Chrome headless: identical
pixels to the browser, zero new dependencies.

Set GRIT_CHROME to a browser binary to override auto-detection.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

CHROME_CANDIDATES = [
    os.environ.get("GRIT_CHROME", ""),
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    # Linux (dev container / server)
    "/opt/pw-browsers/chromium",
    "google-chrome", "chromium", "chromium-browser",
]


def find_chrome() -> str | None:
    for c in CHROME_CANDIDATES:
        if not c:
            continue
        if os.path.sep in c:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
            # playwright-style folder: <dir>/chrome-linux/chrome
            nested = os.path.join(c, "chrome-linux", "chrome")
            if os.path.isfile(nested) and os.access(nested, os.X_OK):
                return nested
        else:
            hit = shutil.which(c)
            if hit:
                return hit
    return None


class PdfUnavailable(RuntimeError):
    pass


def html_to_pdf(html: str, timeout: int = 45) -> bytes:
    """Render an HTML string to PDF bytes with headless Chrome."""
    chrome = find_chrome()
    if not chrome:
        raise PdfUnavailable(
            "No Chrome/Chromium found. Install Google Chrome, or set the "
            "GRIT_CHROME environment variable to a browser binary.")
    with tempfile.TemporaryDirectory(prefix="grit_pdf_") as td:
        src = os.path.join(td, "report.html")
        out = os.path.join(td, "report.pdf")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(html)
        cmd = [
            chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--disable-crash-reporter", "--disable-extensions",
            "--no-pdf-header-footer", "--no-margins",
            f"--print-to-pdf={out}", f"file://{src}",
        ]
        try:
            run = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise PdfUnavailable("Chrome took too long rendering the PDF.")
        if not os.path.isfile(out):
            # older Chrome doesn't know --headless=new / --no-pdf-header-footer
            cmd_legacy = [
                chrome, "--headless", "--disable-gpu", "--no-sandbox",
                "--print-to-pdf-no-header",
                f"--print-to-pdf={out}", f"file://{src}",
            ]
            run = subprocess.run(cmd_legacy, capture_output=True,
                                 timeout=timeout)
        if not os.path.isfile(out):
            err = (run.stderr or b"")[-300:].decode(errors="replace")
            raise PdfUnavailable(f"Chrome could not render the PDF. {err}")
        with open(out, "rb") as fh:
            return fh.read()
