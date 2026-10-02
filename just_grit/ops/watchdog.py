# -*- coding: utf-8 -*-
"""Every five minutes: is the app healthy? If not, one text to the owner,
then quiet for two hours, then a text when it's back. Runs from launchd
with the app's own venv; talks to the app over HTTP so it never imports
the app (importing would start the loops)."""
import json, os, pathlib, sys, time, urllib.request, sqlite3

APP = pathlib.Path(__file__).resolve().parent.parent
DATA = pathlib.Path(os.environ.get("JUST_GRIT_DATA") or APP.parent)
PORT = os.environ.get("JUST_GRIT_PORT", "8080")
STATE = APP / "backups" / ".watchdog.json"
COOLDOWN = 2 * 3600
sys.path.insert(0, str(APP))


def health():
    try:
        with urllib.request.urlopen("http://127.0.0.1:%s/health" % PORT, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": type(e).__name__}


def settings():
    """Telnyx key, from-number, and the owner's cell, straight from the
    primary database (the app may be the thing that's down)."""
    try:
        c = sqlite3.connect("file:%s?mode=ro" % (DATA / "justgrit.db"), uri=True)
        rows = dict(c.execute("SELECT k, v FROM settings WHERE k IN ('telnyx_api_key','telnyx_from_number','telnyx_messaging_profile_id','telnyx_rep_number','sender_phone','alert_phone')"))
        c.close()
        return rows
    except Exception:
        return {}


def text(msg: str) -> bool:
    s = settings()
    to = (s.get("alert_phone") or s.get("telnyx_rep_number") or s.get("sender_phone") or "").strip()
    if not (s.get("telnyx_api_key") and s.get("telnyx_from_number") and to):
        print("watchdog: no Telnyx key / from-number / owner phone in settings; cannot text. " + msg)
        return False
    try:
        import textback as tb
        tb._send(api_key=s["telnyx_api_key"], from_number=s["telnyx_from_number"], to_number=to, text=msg,
                 messaging_profile_id=s.get("telnyx_messaging_profile_id", ""))
        return True
    except Exception as e:
        print("watchdog: text failed: %s: %s" % (type(e).__name__, e))
        return False


def main():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {"down_since": 0, "last_alert": 0}
    code, h = health()
    ok = (code == 200)
    why = ("not answering" if code == 0 else "HTTP %d" % code) if not ok else ""
    if not ok and h.get("stale"):
        why += "; stopped: " + ", ".join(h["stale"])
    if not ok and h.get("db") is False:
        why += "; database not answering"
    now = time.time()
    if ok:
        if st.get("down_since"):
            mins = int((now - st["down_since"]) / 60)
            text("Just Grit is back up after %d min." % mins)
        st = {"down_since": 0, "last_alert": 0}
    else:
        st["down_since"] = st.get("down_since") or now
        if now - st.get("last_alert", 0) > COOLDOWN:
            if text("Just Grit needs you: %s. Status: https://justgrit.thewatsonfactor.dev/status" % why):
                st["last_alert"] = now
    STATE.write_text(json.dumps(st))
    print("watchdog: %s%s" % ("ok" if ok else "DOWN", (" (" + why + ")") if why else ""))


if __name__ == "__main__":
    main()
