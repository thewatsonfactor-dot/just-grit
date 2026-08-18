"""Just Grit Website Analyzer — Scout's site-audit engine.

Rebuilt 2026-08-04 from the v1 design notes, with the v1 "known limits"
addressed: rate limiting, domain suppression, configurable CORS, scan
persistence + score history, contact-page crawl, and competitor mode.

Ground rules (non-negotiable, see project notes):
- Honors robots.txt, identifies itself in the User-Agent.
- No auth, no form submission, no vulnerability probing, no personal data.
- Every finding answers: what we found (a number), why it matters (dollars,
  never jargon), and the fix (hand-to-a-developer instruction).
- False positives are the bug class that matters. When in doubt, stay quiet.
"""

__version__ = "2.0.0"
USER_AGENT = (
    "JustGritAnalyzer/2.0 (+https://thewatsonfactor.dev/analyzer; "
    "site health check; contact: bot@thewatsonfactor.dev)"
)
