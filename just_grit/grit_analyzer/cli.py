"""Command line.

  python -m grit_analyzer.cli example.com                # single audit
  python -m grit_analyzer.cli example.com --report out.html
  python -m grit_analyzer.cli --batch list.txt           # one domain per line
  python -m grit_analyzer.cli --compare a.com b.com c.com
  python -m grit_analyzer.cli --suppress somedomain.com  # never scan again
  python -m grit_analyzer.cli --history example.com
"""

from __future__ import annotations

import argparse
import json
import sys

from . import db, guard
from .analyzer import analyze, analyze_batch
from .compare import compare
from .report import render_report


def main(argv=None):
    p = argparse.ArgumentParser(prog="grit-analyzer",
                                description="Just Grit Website Analyzer")
    p.add_argument("url", nargs="?", help="domain or URL to audit")
    p.add_argument("--report", metavar="FILE",
                   help="write the client-facing HTML report here")
    p.add_argument("--batch", metavar="FILE",
                   help="file with one domain per line; prints ranked JSON")
    p.add_argument("--compare", nargs="+", metavar="DOMAIN",
                   help="competitor mode: rank these sites against each other")
    p.add_argument("--suppress", metavar="DOMAIN",
                   help="add a domain to the do-not-scan list")
    p.add_argument("--history", metavar="DOMAIN",
                   help="show stored scan history for a domain")
    p.add_argument("--deep", action="store_true",
                   help="full audit in batch/compare (slower)")
    p.add_argument("--no-save", action="store_true",
                   help="don't persist this scan to the local database")
    args = p.parse_args(argv)

    if args.suppress:
        guard.suppress(args.suppress)
        print(f"Suppressed: {args.suppress} (file: {guard.SUPPRESSION_FILE})")
        return 0

    if args.history:
        print(json.dumps({"history": db.history(args.history),
                          "regression": db.regression(args.history)}, indent=2))
        return 0

    if args.batch:
        with open(args.batch, encoding="utf-8") as fh:
            urls = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
        urls = [u for u in urls if not guard.is_suppressed(u)]
        rows = analyze_batch(urls, deep=args.deep)
        print(json.dumps({"results": rows}, indent=2))
        return 0

    if args.compare:
        urls = [u for u in args.compare if not guard.is_suppressed(u)]
        print(json.dumps(compare(urls, deep=args.deep), indent=2))
        return 0

    if not args.url:
        p.print_help()
        return 1

    if guard.is_suppressed(args.url):
        print(guard.SUPPRESSED_MESSAGE, file=sys.stderr)
        return 2

    d = analyze(args.url, deep=True)
    if not args.no_save:
        db.save_scan(d)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(render_report(d))
        print(f"Report written: {args.report}")
    slim = {k: v for k, v in d.items() if k != "findings"}
    print(json.dumps(slim, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
