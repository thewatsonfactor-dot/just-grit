# -*- coding: utf-8 -*-
"""Multi-location detection.

Daniel: "a lot of the corporate or multiple location sites ... show a bunch of
errors, but it's because they have multiple locations and they are not gonna
have that data on there."

He is right, and the failure is specific: a chain's homepage legitimately has
no single street address and no single LocalBusiness block, because those live
on the per-location pages. Telling a twelve-store chain to put its address in
the footer is advice that is wrong, and wrong in a way a prospect spots in two
seconds - which costs the meeting the report was supposed to buy.
"""
import sys, re
sys.path.insert(0, '.')
from bs4 import BeautifulSoup
from grit_analyzer import checks as C

def soup(h): return BeautifulSoup(h, "html.parser")
def text(s_): return s_.get_text(" ", strip=True)

CASES = [
    # ── chains: every one of these must suppress the address finding ──
    ("plural nav link",
     '<nav><a href="/locations">Locations</a></nav><p>Fast, friendly service.</p>', True),
    ("nested path",
     '<a href="/pages/our-locations">Find a Wash</a>', True),
    ("slug in path, no segment match",
     '<a href="/car-wash-locations/">See all</a>', True),
    ("store locator label",
     '<a href="/find">Store Locator</a>', True),
    ("near you",
     '<a href="/x">Find a wash near you</a>', True),
    ("count stated in prose only",
     '<p>Serving Texas from 12 convenient locations since 1968.</p>', True),
    ("per-location pages",
     '<a href="/locations/stone-oak">Stone Oak</a><a href="/locations/alamo-heights">AH</a>', True),
    ("three addresses on one page",
     '<p>101 Main St, San Antonio, TX 78205 · 202 Broadway Ave, San Antonio, TX 78209'
     ' · 303 Bandera Rd, San Antonio, TX 78228</p>', True),

    # ── not chains: the address finding must still fire ──
    ("singular Location nav is not enough",
     '<nav><a href="/location">Location</a></nav>', False),
    ("plain one-shop site",
     '<p>Open Mon-Fri. Call us.</p><a href="/about">About</a>', False),
    ("the word location in body copy",
     '<p>We are in a convenient location right off the highway.</p>', False),
    ("one location stated",
     '<p>1 locations</p>', False),
]

fails = 0
for name, html, want in CASES:
    s_ = soup(html)
    got = C.looks_like_chain(text(s_), s_)
    ok = (got == want)
    fails += (not ok)
    print("%-4s %-34s chain=%-5s (want %s)" % ("ok" if ok else "FAIL", name, got, want))

print("-" * 62)

# The finding-level consequence, which is what actually reaches the prospect.
CHAIN = soup('<nav><a href="/locations">Locations</a></nav><p>Fast service.</p>')
ONE   = soup('<nav><a href="/about">About</a></nav><p>Fast service.</p>')
for label, s_, want_addr in (("chain", CHAIN, False), ("single", ONE, True)):
    _, fs = C.check_local(None, s_)
    ids = {f.get("key") for f in fs}
    has = "no_address" in ids
    ok = (has == want_addr)
    fails += (not ok)
    print("%-4s %-34s no_address finding present=%s (want %s)"
          % ("ok" if ok else "FAIL", label + " homepage", has, want_addr))

# and the schema advice must change shape, not just disappear
_, fs = C.check_local(None, CHAIN)
sch = [f for f in fs if f.get("key") == "no_localbusiness"]
ok = bool(sch) and "PER LOCATION" in sch[0]["fix"]
fails += (not ok)
print("%-4s %-34s schema fix says per-location" % ("ok" if ok else "FAIL", "chain homepage"))

# stated count is used, not just detected
n = C.count_locations("Serving Texas from 12 convenient locations.", soup("<p></p>"))
ok = (n == 12); fails += (not ok)
print("%-4s %-34s count_locations=%s (want 12)" % ("ok" if ok else "FAIL", "stated count", n))

print("-" * 62)
print("FAILURES: %d" % fails)
sys.exit(1 if fails else 0)
