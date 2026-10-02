# -*- coding: utf-8 -*-
"""A vertical, packaged — and the things that must never ride along.

GoHighLevel's snapshot is a copy of a sub-account's configuration that loads
into an empty one. The half that makes it a product rather than a database
dump is what it refuses to copy: contacts, credentials, connections. These
tests are mostly about that half.

The central claim under test: what travels is an ALLOW-list. A deny-list
fails open — add a setting tomorrow and every snapshot silently carries it.
So the strongest test here is not "the key is filtered out", it is "a setting
nobody allow-listed is absent from the file", which stays true for settings
that do not exist yet.

Temp data dir, no network, never touches the live databases.
"""
import json, os, pathlib, sqlite3, sys, tempfile
sys.path.insert(0, '.')

TMP = tempfile.mkdtemp(prefix="jg-snap-")
os.environ["JUST_GRIT_DATA"] = TMP
import webapp as W
import workspaces as ws
import snapshots as S
W.DATA = pathlib.Path(TMP)
for _slug in list(ws.WORKSPACES):
    _t = ws.CURRENT.set(_slug)
    try:
        W.init_db()
    finally:
        ws.CURRENT.reset(_t)

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want)
    fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

class Req:
    headers = {}
    cookies = {}
W.require_auth = lambda r: "test@example.com"

OFFERS = {"offers": {
    "After-Hours Line": {
        "tail": "the calls coming in after you close",
        "cost": "Somebody rear-ended at eight books with whoever picks up.",
        "fix": "I put something on your line that answers every time.",
        "evidence": "voicemail|after.?hours|never answer",
        "question": "When somebody calls %s after seven, where does that go?",
        "impact": ["one missed call a week", "about a day to set up"]},
    "Reviews & Rewards": {
        "tail": "the reviews you are not asking for",
        "cost": "Your happiest patients are the ones who never write one.",
        "fix": "It asks them at the moment they are happiest."}}}

# ── the pieces validate before they travel ──────────────────────────────
check("a normal window survives",
      S.clean_windows({"chiro": [[11, 30, 13, 30]]}), {"chiro": [[11, 30, 13, 30]]})
check("a backwards window is DROPPED, not repaired",
      S.clean_windows({"chiro": [[16, 0, 9, 0]]}), {})
check("  because 16-to-9 could mean overnight, and guessing wrong calls "
      "somebody at 3am",
      S.clean_windows({"chiro": [[16, 0, 9, 0]], "auto": [[9, 0, 11, 0]]}),
      {"auto": [[9, 0, 11, 0]]})
check("a 25th hour is refused", S.clean_windows({"x": [[25, 0, 26, 0]]}), {})
check("a vertical name with a path in it is refused",
      S.clean_windows({"../etc": [[9, 0, 10, 0]]}), {})
check("a three-element window is refused", S.clean_windows({"x": [[9, 0, 10]]}), {})
check("garbage is {} rather than an exception", S.clean_windows("not json"), {})

check("normal gaps survive", S.clean_gaps({"2": 3, "3": 7}), {2: 3, 3: 7})
check("a gap of a year is refused", S.clean_gaps({"2": 400}), {})
check("step 1 is refused - the first email is not a follow-up",
      S.clean_gaps({"1": 3}), {})
check("a non-numeric step is skipped", S.clean_gaps({"soon": 3}), {})
check("max step clamps to something sane", S.clean_max_step(99), 3)
check("  and a blank falls back to the default", S.clean_max_step(""), 3)

# ── round trip ──────────────────────────────────────────────────────────
built = S.build(name="Chiropractic clinic", catalog=OFFERS,
                windows={"chiro": [[11, 30, 13, 30]]}, gaps={2: 3, 3: 7},
                max_step=3, vertical="chiro", source_workspace="sourceco")
back = S.parse(json.loads(json.dumps(built)), W.parse_catalog)
check("a snapshot survives a round trip through JSON",
      sorted(back["catalog"]["offers"]), ["After-Hours Line", "Reviews & Rewards"])
check("  the evidence regex comes back with it",
      back["catalog"]["offers"]["After-Hours Line"]["evidence"],
      "voicemail|after.?hours|never answer")
check("  so do the call windows", back["call_windows"], {"chiro": [[11, 30, 13, 30]]})
check("  and the follow-up gaps", back["followup_gaps"], {"2": 3, "3": 7})

check("a file that is not a snapshot is refused",
      S.parse({"hello": "world"}, W.parse_catalog), {})
check("a snapshot from a future version is refused",
      S.parse(dict(built, just_grit_snapshot=99), W.parse_catalog), {})
check("a snapshot whose offers are all half-written is refused",
      S.parse(dict(built, catalog={"offers": {"Thing": {"tail": "x"}}}),
              W.parse_catalog), {})

# ── what must NOT travel ────────────────────────────────────────────────
W.create_workspace(Req(), W.NewWorkspaceBody(
    slug="sourceco", name="Source Clinic", short="Source", buyer="clinics"))

tok = ws.CURRENT.set("sourceco")
try:
    W.set_setting(W.CATALOG_SETTING, json.dumps(OFFERS))
    W.set_setting("sender_email", "daniel@sourceclinic.example")
    W.set_setting("sender_name", "Daniel Watson")
    W.set_setting("sender_address", "3217 Wild Iris, New Braunfels, TX 78130")
    W.set_setting("sender_phone", "(210) 555-0134")
    W.set_setting("telnyx_api_key", "KEY01FACE0000000000000000000000000_ZZTOPSECRET")
    W.set_setting("telnyx_from_number", "8307152300")
    W.set_setting("public_base_url", "https://source.example")
    W.set_setting("daily_email_cap", "999999")
    W.set_setting("default_city", "San Antonio, TX")
    # A setting invented today that nobody has thought about yet. An
    # allow-list must exclude it without anyone naming it.
    W.set_setting("some_future_key", "sk-futurekeyvaluenobodyhasseen")

    c = W.db()
    c.execute("INSERT INTO prospects (company,status,vertical,email) "
              "VALUES ('Zzyzx Family Clinic','new','chiro','owner@zzyzx.example')")
    c.commit(); c.close()

    out = W.export_snapshot(Req(), name="Chiro clinic", vertical="chiro")
    blob = json.dumps(out["snapshot"])
finally:
    ws.CURRENT.reset(tok)

for label, needle in [
        ("the sender's email address", "daniel@sourceclinic.example"),
        ("the sender's name", "Daniel Watson"),
        ("the mailing address", "Wild Iris"),
        ("the sender's phone number", "555-0134"),
        ("the Telnyx API key", "ZZTOPSECRET"),
        ("the outbound phone number", "8307152300"),
        ("the public hostname", "source.example"),
        ("the daily sending cap", "999999"),
        ("the default city", "San Antonio"),
        ("a setting invented after the allow-list was written",
         "sk-futurekeyvaluenobodyhasseen"),
        ("a prospect's company name", "Zzyzx"),
        ("a prospect's email address", "owner@zzyzx.example")]:
    check("%s is absent from the snapshot" % label, needle not in blob, True)

check("what DOES travel is the copy", sorted(out["offers"]),
      ["After-Hours Line", "Reviews & Rewards"])
check("  plus the windows for the vertical it names",
      out["snapshot"]["call_windows"].get("chiro"), [[11, 30, 13, 30], [14, 0, 15, 30]])

# ── the advisory secret-sniffer ─────────────────────────────────────────
dirty = S.build(name="x", catalog={"offers": {"Thing": {
    "tail": "t", "cost": "c",
    "fix": "Log in with KEY01FACE0000000000000000000000000 and it works."}}})
check("a key pasted into offer copy is flagged", bool(S.suspect_secrets(dirty)), True)
check("  clean copy is not flagged", S.suspect_secrets(built), [])

# ── loading it into the next customer ───────────────────────────────────
W.create_workspace(Req(), W.NewWorkspaceBody(
    slug="targetco", name="Target Clinic", short="Target", buyer="clinics"))

tok = ws.CURRENT.set("targetco")
try:
    check("a fresh workspace refuses to draft", W.has_catalog(), False)
    loaded = W.import_snapshot(Req(), W.SnapshotBody(snapshot=out["snapshot"]))
    check("the snapshot loads", loaded["ok"], True)
    check("  and now it has offers", W.has_catalog(), True)
    check("  the copy came across", sorted(loaded["offers"]),
          ["After-Hours Line", "Reviews & Rewards"])
    check("  the call windows came across",
          W.call_windows("chiro"), [[11, 30, 13, 30], [14, 0, 15, 30]])
    check("  a vertical the snapshot never mentioned keeps the code default",
          W.call_windows("restaurant"), W.CALL_WINDOWS["restaurant"])
    check("  the follow-up gaps came across", W.followup_gaps(), {2: 3, 3: 7})
    check("  and NOT the sender identity - that is still blank",
          W.setting("sender_email", ""), "")
    check("  nor the sending cap", W.setting("daily_email_cap", ""), "")

    refused = False
    try:
        W.import_snapshot(Req(), W.SnapshotBody(snapshot=out["snapshot"]))
    except Exception as e:
        refused = "already has offers" in str(e)
    check("importing over an existing catalog is refused", refused, True)
    ok2 = W.import_snapshot(Req(), W.SnapshotBody(snapshot=out["snapshot"],
                                                  overwrite=True))
    check("  unless you say overwrite", ok2["ok"], True)

    junk = False
    try:
        W.import_snapshot(Req(), W.SnapshotBody(snapshot={"hello": "world"},
                                                overwrite=True))
    except Exception as e:
        junk = "not a readable" in str(e)
    check("a file that is not a snapshot is refused at the door", junk, True)
finally:
    ws.CURRENT.reset(tok)

# and it did not leak sideways while we were in there
tok = ws.CURRENT.set("sourceco")
try:
    check("the source workspace's own cap is untouched",
          W.setting("daily_email_cap", ""), "999999")
finally:
    ws.CURRENT.reset(tok)

# ── the built-ins ───────────────────────────────────────────────────────
tok = ws.CURRENT.set("watson")
try:
    b = W.builtin_catalog()
    check("Watson's code copy materialises into catalog shape",
          "AI Vision" in b["offers"], True)
    check("  with the three sentences an email needs",
          all(k in b["offers"]["AI Vision"] for k in ("tail", "cost", "fix")), True)
    check("  and it passes the same validator a customer's catalog does",
          sorted(W.parse_catalog(json.dumps(b))["offers"]) == sorted(b["offers"]), True)
    seeded = W.export_snapshot(Req(), name="Watson baseline")
    check("  so the first snapshot can be cut from the business with proven copy",
          "AI Vision" in seeded["offers"], True)

    blocked = False
    try:
        W.import_snapshot(Req(), W.SnapshotBody(snapshot=out["snapshot"],
                                                overwrite=True))
    except Exception as e:
        blocked = "lives in code" in str(e)
    check("importing INTO a built-in is refused rather than silently ignored",
          blocked, True)
finally:
    ws.CURRENT.reset(tok)

# ── the cache cannot go stale ───────────────────────────────────────────
tok = ws.CURRENT.set("targetco")
try:
    before = W.call_windows("chiro")
    W.set_setting(S.SETTING_WINDOWS, json.dumps({"chiro": [[8, 0, 9, 0]]}))
    check("a settings write is visible on the very next read",
          W.call_windows("chiro"), [[8, 0, 9, 0]])
    check("  (and it really did change)", before != W.call_windows("chiro"), True)
finally:
    ws.CURRENT.reset(tok)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
