# -*- coding: utf-8 -*-
"""The door-knock leave-behind: a two-page sheet for one lead.

Daniel, 2026-09-27: "print a one sheeter for that customer so you could take
it in to them ... the product we found in the research already laid out and
with a picture painted for them so they can see the value, and maybe an
example of the service on another page. Push a button and it creates that
sheet for the one you are going to door knock that day."

Page 1 - what we noticed (only things the research actually found), then one
block per product: a scene from their trade ("picture this"), the scene with
the product in it, what the problem costs, what we'd do.
Page 2 - what it looks like: a phone mock-up of each product, filled in with
their business name, plus three steps for how it works. Sample data is
labelled as sample data.

Rules carried over from the emails: a scene is a scene ("picture..."), never
a claim about what happened at their business; nothing about their setup is
asserted that the research didn't find; no invented prices or competitors.
This module only renders - webapp.py gathers the lead and passes a dict in.
"""
import html
import re

def e(s):
    return html.escape(str(s if s is not None else ""))

TRADES = ("restaurant", "auto", "appointment", "contractor")


def trade_of(d):
    v = (d.get("vertical") or "").lower()
    if v == "chiro":
        return "appointment"
    return v if v in TRADES else "generic"


def you(text):
    """Research notes are written about the lead ("their reviews"); the sheet
    is handed to the lead, so it talks to them."""
    t = (text or "").strip()
    t = re.sub(r"^Checked their", "We checked your", t)
    t = re.sub(r"^Checked ", "We checked ", t)
    for a, b in ((r"\bTheir\b", "Your"), (r"\btheir\b", "your"), (r"\bThey're\b", "You're"),
                 (r"\bthey're\b", "you're"), (r"\bthem\b", "you"), (r"\bThey\b", "You"),
                 (r"\bthey\b", "you")):
        t = re.sub(a, b, t)
    t = t.replace(" - ", " — ")
    return t


# ── page 1: the picture, per product ────────────────────────────────────
# Each entry: headline, today (the scene), with (the scene with the product),
# and the fix is passed in from the app's own offer copy so the sheet and the
# email never say different things.

VISION = {
    "restaurant": (
        "Picture Friday at 7:40. Table 12 got their entrées nine minutes ago and nobody has been "
        "back - they want another round and the check. Your manager is at the host stand working "
        "a wait list and can't see it. Tomorrow it's a review that says \"service was slow.\"",
        "A camera over the floor notices the table has gone too long and your manager's phone "
        "buzzes: \"Table 12 - 9 min, no check-back.\" Somebody is there in thirty seconds, while "
        "it can still be fixed."),
    "auto": (
        "Picture a customer at the counter saying the scratch on their door happened here. You're "
        "pretty sure it didn't. Proving it means scrubbing through hours of video on a small "
        "screen while the line backs up.",
        "You type \"silver truck, around 4pm\" and the clip is on the screen in seconds. The claim "
        "is settled before they finish their coffee."),
    "appointment": (
        "Picture the lobby at 10:15. Three people are waiting, the front desk stepped away, and "
        "the person who has been sitting longest is already writing the review in their head.",
        "A camera on the lobby notices the room backing up or the desk sitting empty and pings "
        "whoever is free, so someone steps out while the person is still waiting."),
    "contractor": (
        "Picture 9 PM. The yard gate didn't latch, or a supplier dropped a pallet at the wrong "
        "gate. You find out tomorrow morning - after it's walked off.",
        "A camera on the yard notices and texts you right then: \"Gate open after hours\" or "
        "\"Delivery at the side gate.\" You handle it tonight, from your phone."),
    "generic": (
        "Picture a busy afternoon. Something goes sideways out front while you're in the back, "
        "and you hear about it from the review, not from your team.",
        "The camera already pointed at the room notices it and pings you while it's happening, "
        "so it gets fixed instead of written up."),
}

PICTURE = {
    "Ordering App": lambda d: (
        "Orders that don't pay a commission",
        "Picture a regular who orders from you every Friday. Tonight they order through a "
        "delivery app like always - a $40 order. The app keeps roughly $12 of it, and it keeps "
        "their name and number too. Next Friday the app shows them three other places first.",
        "Same regular, same Friday, but they order from %s's own page - your menu, your prices, "
        "your name at the top. You pay card processing instead of a commission, and they go on "
        "your customer list, so you can text them the Tuesday special." % d["company"]),
    "AI Vision": lambda d: ("Finding out now instead of tomorrow",) + VISION[trade_of(d)],
    "Website": lambda d: (
        "Showing up, and getting the call",
        ("Picture someone on their phone looking for what you do, right now. They find your "
         "site - that's the hard part - and %s. They hit back and call the next one."
         % d["opener"].rstrip(".")) if d.get("opener") and d.get("domain") else
        "Picture someone on their phone searching for what you do, right now, a few miles away. "
        "The businesses that come up get the call. If you aren't one of them, the job goes to "
        "whoever is - not whoever is better.",
        "They land on a page that loads fast, says exactly what you do and where, and has a big "
        "\"Tap to call\" button at the top. One tap and your phone rings."),
    "Reviews & Rewards": lambda d: (
        "The rating going up on its own",
        ("Picture someone choosing between you and two other places on Google. They glance at "
         "the stars - yours says %s from %s reviews - read the top two, and decide in about ten "
         "seconds. Most of your happiest customers never wrote anything, because nobody asked."
         % (d["rating"], d["reviews"])) if d.get("rating") else
        "Picture someone choosing between you and two other places on Google. They glance at "
        "the stars, read the top two reviews, and decide in about ten seconds. Most happy "
        "customers never write anything, because nobody asked.",
        "After every visit the customer gets a short, friendly text: how did we do? Leaving a "
        "Google review is one tap. Anyone with a problem can reply straight to you. The number "
        "climbs on its own."),
    "Portfolio Health Score": lambda d: (
        "Finding it on a Tuesday, not at 9 PM",
        "Picture 9 PM. A tenant calls: the water heater let go and there's water in the hall. "
        "It was twelve years old and nobody had looked at it since the last turnover. Now it's "
        "an emergency call, a flooring claim, and a tenant who remembers.",
        "Every property gets walked on a schedule, photographed, and scored 0-100 across roof "
        "and drainage, plumbing and water, electrical, life safety, building envelope and HVAC. "
        "The water heater shows up on the report a year before it floods, as a line item you "
        "approve on a Tuesday afternoon."),
}


# HomeRepair's services (Daniel, 2026-09-28). The scene, the scene with us in
# it, and why it matters - HomeRepair has no offer-copy table like Watson's.
PICTURE.update({
    "Snap It Send It": lambda d: (
        "Tenant requests without the phone tag",
        "Picture a Tuesday. A tenant texts your office about a drip under the sink, then calls, then "
        "emails a blurry photo. Someone on your team finds a tech, waits for a callback, and chases "
        "the tenant for a time. It's a small job that took three people and two days.",
        "The tenant snaps a photo and sends it in. You see it in our system with the address and the "
        "photo, and you tap approve. We send a tech and stay inside the dollar limit you set for that "
        "property. Your team is out of the middle."),
    "Preventive Maintenance": lambda d: (
        "Fewer emergencies, not faster ones",
        "Picture the first 100-degree week. The AC at one of your rentals quits because the filter was "
        "never changed, and it's a Saturday. Now it's an emergency call, an upset tenant and a "
        "weekend rate.",
        "Every property gets visits on a schedule. We change AC filters, flush the water heater, check "
        "the electrical and look over the rest, and log every visit to that address. Most of those "
        "Saturday calls never happen."),
    "Move-In/Move-Out Reports": lambda d: (
        "No more deposit arguments",
        "Picture move-out day. The tenant says the carpet stain was there when they moved in. Your "
        "notes say it wasn't. There's no photo either of you can point to, so it turns into an "
        "argument over the deposit.",
        "Before the tenant moves in, we photograph every room and date it. After they move out, we do "
        "it again. We're a third party, so both sides are looking at the same photos."),
    "Quarterly Health & Safety Audit": lambda d: (
        "Knowing what's coming before it breaks",
        "Picture an owner asking what shape their property is in. You'd have to send someone out, "
        "and nobody has looked at the smoke detectors or the water heater since the last turnover.",
        "Every quarter we walk each property and send you a written health and safety report with "
        "photos and a score from 0 to 100, with anything that needs attention ranked by urgency."),
    "Common-Area Care": lambda d: (
        "The board always has an answer",
        "Picture a homeowner at the board meeting asking what got fixed in the common areas this "
        "year. Nobody has a list, so the board says it'll get back to them.",
        "We walk the common areas every month, fix the small stuff while we're there, and send the "
        "board photos of everything with a one-page summary."),
    "Repair Estimate": lambda d: (
        "Repair numbers inside the option period",
        "Picture the inspection report landing with three days left. Good contractors are booked a "
        "week out, so the repair credit ends up being a guess.",
        "You take photos of the items on the report and get a written repair estimate with Central "
        "Texas prices the same day, plus a condition score."),
})
WHY = {
    "Snap It Send It": "Every request that goes through your office costs your team time, even the small ones.",
    "Preventive Maintenance": "The cheapest repair is the one that never turns into an emergency call.",
    "Move-In/Move-Out Reports": "A dated photo from a third party settles what two people's memories can't.",
    "Quarterly Health & Safety Audit": "Owners want to know their property is looked after. This shows them.",
    "Common-Area Care": "When nobody writes it down, the board ends up guessing.",
    "Repair Estimate": "A guess in the option period usually costs the seller.",
}


def picture_for(offer, d):
    if offer == "AI Receptionist":
        rec = d.get("receptionist") or {}
        return ("A call that doesn't go to voicemail", rec.get("picture", ""), rec.get("with", ""))
    f = PICTURE.get(offer)
    if f:
        return f(d)
    return None


# ── page 2: what it looks like ──────────────────────────────────────────
SAMPLE_NEED = {
    "restaurant": "Party of 8 tomorrow at 6:30, asking if you do trays for pickup.",
    "auto": "Nail in rear tire, pulled over off 1604, can come in by 3.",
    "appointment": "New patient, wants in this week, mornings are best.",
    "contractor": "AC stopped cooling, house is 84°, wants someone today.",
    "generic": "Has a quick question about pricing, wants a call back today.",
}
VISION_ALERT = {
    "restaurant": ("Table 12", "9 min since last check-back", "Floor camera · just now"),
    "auto": ("Search: silver truck, ~4pm", "3 clips found · Bay 2, 3:58 PM", "Lot camera"),
    "appointment": ("Lobby", "4 waiting · front desk empty 3 min", "Lobby camera · just now"),
    "contractor": ("Side gate", "Open after hours · 9:04 PM", "Yard camera · just now"),
    "generic": ("Front room", "Needs attention now", "Camera · just now"),
}


def phone(inner, label):
    return ('<div class="mock"><div class="phone"><div class="notch"></div>%s</div>'
            '<div class="mocklabel">%s</div></div>' % (inner, e(label)))


def mock_for(offer, d):
    co = e(d["company"])
    t = trade_of(d)
    if offer == "AI Receptionist":
        inner = ('<div class="sms-head">Messages</div>'
                 '<div class="bubble in"><b>Missed call handled for %s</b><br>'
                 'Name: Maria G.<br>Phone: (210) 555-0147<br>Needs: %s<br>'
                 '<span class="dim">Tap to call back</span></div>'
                 '<div class="stamp">2:14 PM</div>' % (co, e(SAMPLE_NEED[t])))
        steps = ["Someone calls and you can't pick up - busy, closed, or on another line.",
                 "The assistant answers in a friendly voice and gets their name, number and what they need.",
                 "It texts you the details within seconds. You call back when your hands are free."]
        return phone(inner, "Example text to you"), steps
    if offer == "AI Vision":
        a, b, c = VISION_ALERT[t]
        inner = ('<div class="lock-time">7:41</div>'
                 '<div class="notif"><div class="n-app">%s · Alert</div><b>%s</b><br>%s'
                 '<div class="dim">%s</div></div>' % (co, e(a), e(b), e(c)))
        steps = ["Works with cameras you already have, or one or two we add.",
                 "You tell it what matters: a table waiting, a lobby backing up, a gate left open.",
                 "It pings your phone while it's happening, and footage is searchable in plain English."]
        return phone(inner, "Example alert on your phone"), steps
    if offer == "Ordering App":
        inner = ('<div class="site-bar" style="background:var(--acc)">%s</div>'
                 '<div class="site-body"><div class="pill">Order for pickup</div>'
                 '<div class="menu-row"><span>Your menu</span><span>your prices</span></div>'
                 '<div class="menu-row"><span>Your photos</span><span>your specials</span></div>'
                 '<div class="menu-row"><span>Their order history</span><span>saved</span></div>'
                 '<div class="cta">Checkout · pay online</div></div>' % co)
        steps = ["We build your ordering page from your menu - your name and colors, not an app's.",
                 "Customers order and pay on it; orders come to you by text or a tablet.",
                 "Every customer lands on your own list, so you can bring them back with a text."]
        return phone(inner, "Example of your ordering page"), steps
    if offer == "Website":
        tel = e(d.get("phone") or "(210) 555-0100")
        inner = ('<div class="site-bar" style="background:var(--acc)">%s</div>'
                 '<div class="site-body"><div class="hero-t">%s</div>'
                 '<div class="cta">Tap to call %s</div>'
                 '<div class="cta ghost">Request a quote</div>'
                 '<div class="menu-row"><span>Hours</span><span>Directions</span></div></div>'
                 % (co, e(d.get("city") or "Serving your area"), tel))
        steps = ["A fast page built for phones, because that's where people find you.",
                 "Your number is a big button at the top. One tap and it dials.",
                 "Set up so Google knows what you do and where, so you show up in local searches."]
        return phone(inner, "Example of your site on a phone"), steps
    if offer == "Reviews & Rewards":
        inner = ('<div class="sms-head">Messages</div>'
                 '<div class="bubble in">Thanks for coming in to %s today! How did we do? '
                 'If you have a second, a quick Google review helps a lot: '
                 '<u>g.page/review</u></div>'
                 '<div class="bubble out">5 stars, great service!</div>'
                 '<div class="stamp">Sent automatically after the visit</div>' % co)
        steps = ["After each visit, the customer gets a short, friendly text from you.",
                 "Leaving a Google review is one tap. Anyone with a problem can reply to you directly.",
                 "You watch the rating and review count climb from one screen."]
        return phone(inner, "Example text to your customer"), steps
    if offer == "Snap It Send It":
        inner = ('<div class="site-bar" style="background:var(--acc)">New request</div>'
                 '<div class="site-body"><div class="menu-row"><span>Unit 4B, 1210 Oak St</span></div>'
                 '<div class="photo">📷 Tenant photo</div>'
                 '<div class="dim">"Water under the kitchen sink, slow drip."</div>'
                 '<div class="menu-row"><span>Your limit</span><span>$250</span></div>'
                 '<div class="cta">Approve</div><div class="cta ghost">Call me first</div></div>')
        steps = ["The tenant snaps a photo of the problem and sends it in.",
                 "You see it in our system with the address and photo, and tap approve.",
                 "We send a tech and stay inside the dollar limit you set for that property."]
        return phone(inner, "Example request (sample)"), steps
    if offer == "Preventive Maintenance":
        rows = [("AC filter", "done"), ("Water heater flush", "done"), ("Electrical check", "done"),
                ("Smoke detectors", "Oct 14"), ("Gutters", "Nov 3")]
        inner = ('<div class="site-bar" style="background:var(--acc)">1210 Oak St</div><div class="site-body">%s'
                 '<div class="dim" style="margin-top:4px">Every visit logged to the address</div></div>'
                 % "".join('<div class="menu-row"><span>%s</span><span>%s</span></div>' % (e(a), "✓" if b == "done" else e(b))
                           for a, b in rows))
        steps = ["We put each property on a visit schedule.",
                 "We change filters, flush the water heater and check the electrical on every visit.",
                 "Every visit gets logged to the address, so you and the owner can see it."]
        return phone(inner, "Example visit log (sample)"), steps
    if offer == "Move-In/Move-Out Reports":
        inner = ('<div class="site-bar" style="background:var(--acc)">Condition report</div>'
                 '<div class="site-body"><div class="menu-row"><span>Move-in</span><span>Aug 1</span></div>'
                 '<div class="menu-row"><span>Move-out</span><span>Jul 31</span></div>'
                 '<div class="photo">Living room · both dates</div><div class="photo">Kitchen · both dates</div>'
                 '<div class="dim">42 photos · shared with owner and tenant</div></div>')
        steps = ["Before move-in we photograph and date every room.",
                 "After move-out we do the same walk again.",
                 "You and the tenant get the same report from a third party."]
        return phone(inner, "Example report (sample)"), steps
    if offer in ("Quarterly Health & Safety Audit", "Portfolio Health Score"):
        cats = [("Roof & drainage", 82), ("Plumbing & water", 58), ("Electrical", 90),
                ("Life safety", 74), ("Building envelope", 69), ("HVAC", 61)]
        bars = "".join('<div class="bar-row"><span>%s</span><i><b style="width:%d%%"></b></i>'
                       '<em>%d</em></div>' % (e(n), v, v) for n, v in cats)
        inner = ('<div class="site-bar" style="background:var(--acc)">Health Score</div>'
                 '<div class="site-body"><div class="score">72<small>/100</small></div>%s'
                 '<div class="dim" style="margin-top:6px">Top item: water heater, 11 yrs - '
                 'plan replacement</div></div>' % bars)
        steps = ["Every quarter we walk the property, roof through foundation.",
                 "You get a written report with photos, a score out of 100, and every item ranked by urgency.",
                 "Forward it to the owner as-is, so they can see their property is looked after."]
        return phone(inner, "Sample report (example numbers)"), steps
    return None, None


CSS = """
:root{--acc:%(acc)s;--ink:#17181b;--ink2:#44474f;--mute:#7a7f89;--hair:#e3e5ea;--tint:%(tint)s}
*{box-sizing:border-box}
html,body{margin:0;background:#e9eaee;color:var(--ink);
  font:10.5pt/1.45 -apple-system,BlinkMacSystemFont,"Helvetica Neue",Helvetica,Arial,sans-serif;
  -webkit-print-color-adjust:exact;print-color-adjust:exact}
@page{size:letter;margin:0}
.page{width:8.5in;height:11in;margin:24px auto;background:#fff;padding:.5in .6in .45in;
  position:relative;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.12);page-break-after:always;
  display:flex;flex-direction:column}
.page:last-child{page-break-after:auto}
@media print{html,body{background:#fff}.page{margin:0;box-shadow:none}.toolbar{display:none}}
.toolbar{position:sticky;top:0;z-index:5;background:#17181b;color:#fff;padding:10px 16px;display:flex;
  gap:10px;align-items:center;font-size:13px}
.toolbar b{flex:1}
.toolbar a,.toolbar button{background:var(--acc);color:#111;border:0;border-radius:8px;padding:7px 14px;
  font:600 13px/1 inherit;text-decoration:none;cursor:pointer}
.top{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:3px solid var(--acc);
  padding-bottom:12px;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:12pt}
.brand img{height:38px;max-width:150px;object-fit:contain}
.prep{text-align:right;font-size:9pt;color:var(--ink2);line-height:1.35}
.prep b{display:block;color:var(--ink);font-size:10.5pt}
h1{font-size:21pt;line-height:1.15;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--ink2);margin:0 0 16px;font-size:10.5pt}
h2{font-size:9pt;letter-spacing:.12em;text-transform:uppercase;color:var(--acc);margin:0 0 7px;font-weight:800}
.noticed{background:var(--tint);border-radius:10px;padding:12px 16px;margin-bottom:16px}
.noticed ul{margin:0;padding-left:18px}
.noticed li{margin:3px 0}
.offer{border:1px solid var(--hair);border-left:5px solid var(--acc);border-radius:10px;padding:13px 16px;margin-bottom:12px}
.offer h3{margin:0 0 8px;font-size:13.5pt}
.offer h3 small{display:block;font-size:8.5pt;color:var(--mute);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:2px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.lbl{font-size:8pt;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--mute);margin-bottom:3px}
.today p{margin:0;font-style:italic;color:var(--ink2)}
.with p{margin:0}
.with .lbl{color:var(--acc)}
.cost{margin:9px 0 0;padding-top:8px;border-top:1px dashed var(--hair);font-size:9.5pt;color:var(--ink2)}
.cost b{color:var(--ink)}
.foot{margin-top:auto;display:flex;justify-content:space-between;align-items:flex-end;gap:16px;
  border-top:1px solid var(--hair);padding-top:12px;font-size:9.5pt}
.foot .who b{font-size:12pt;display:block}
.foot .ask{background:var(--acc);color:#111;border-radius:10px;padding:10px 14px;font-weight:700;max-width:3.3in}
.mocks{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:8px}
.mocks.one{grid-template-columns:1fr}
.mockcol{display:flex;gap:16px;align-items:flex-start}
.mocks:not(.one) .mockcol{flex-direction:column;align-items:center}
.mock{display:flex;flex-direction:column;align-items:center}
.phone{width:2.35in;height:3.7in;border-radius:30px;border:9px solid #17181b;background:#f4f5f7;
  position:relative;overflow:hidden;padding:30px 10px 10px;font-size:8.5pt}
.notch{position:absolute;top:6px;left:50%%;transform:translateX(-50%%);width:70px;height:14px;border-radius:9px;background:#17181b}
.mocklabel{font-size:8pt;color:var(--mute);margin-top:6px;text-transform:uppercase;letter-spacing:.08em;font-weight:700}
.sms-head{text-align:center;font-weight:700;font-size:8pt;color:var(--mute);margin-bottom:10px}
.bubble{border-radius:14px;padding:8px 10px;margin:6px 0;max-width:92%%;line-height:1.35}
.bubble.in{background:#fff;border:1px solid var(--hair)}
.bubble.out{background:#2b7cff;color:#fff;margin-left:auto;width:max-content}
.stamp{text-align:center;color:var(--mute);font-size:7.5pt;margin-top:4px}
.lock-time{text-align:center;font-size:30pt;font-weight:300;margin:18px 0 16px;color:#333}
.notif{background:#fff;border-radius:14px;padding:9px 11px;box-shadow:0 2px 8px rgba(0,0,0,.08);line-height:1.35}
.n-app{font-size:7pt;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);font-weight:700;margin-bottom:3px}
.dim{color:var(--mute);font-size:7.5pt}
.site-bar{margin:-30px -10px 10px;padding:32px 10px 10px;color:#111;font-weight:800;font-size:10pt;text-align:center}
.site-body{display:flex;flex-direction:column;gap:7px}
.hero-t{font-weight:700;text-align:center;margin:4px 0}
.pill{align-self:center;border:1px solid var(--hair);border-radius:99px;padding:3px 10px;font-weight:700;background:#fff}
.menu-row{display:flex;justify-content:space-between;background:#fff;border-radius:8px;padding:7px 9px;border:1px solid var(--hair)}
.cta{background:var(--acc);color:#111;text-align:center;border-radius:9px;padding:9px;font-weight:800}
.cta.ghost{background:#fff;border:1.5px solid var(--acc)}
.photo{height:52px;border-radius:8px;background:repeating-linear-gradient(135deg,#dfe2e8 0 8px,#eceef2 8px 16px);
  display:flex;align-items:center;justify-content:center;font-size:7.5pt;color:var(--mute);font-weight:700}
.score{font-size:30pt;font-weight:800;text-align:center;line-height:1}
.score small{font-size:10pt;color:var(--mute)}
.bar-row{display:grid;grid-template-columns:1fr 60px 18px;gap:5px;align-items:center;font-size:7.5pt}
.bar-row i{height:6px;background:#dde0e6;border-radius:4px;overflow:hidden}
.bar-row b{display:block;height:100%%;background:var(--acc)}
.bar-row em{font-style:normal;font-weight:700;text-align:right}
.how{flex:1}
.how h3{margin:0 0 8px;font-size:13pt}
.how ol{margin:0;padding-left:20px}
.how li{margin:0 0 7px}
.fine{font-size:8pt;color:var(--mute);margin-top:10px}
"""


def dash(h):
    """' - ' in copy is a spoken dash; print it as one."""
    return h.replace(" - ", " \u2014 ")


def tint(hex_):
    m = re.match(r"#?([0-9a-f]{6})$", (hex_ or "").strip(), re.I)
    if not m:
        return "#f6f1e7"
    r, g, b = (int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    mix = lambda c: int(c + (255 - c) * 0.9)
    return "#%02x%02x%02x" % (mix(r), mix(g), mix(b))


def render(d, brand, toolbar=None):
    """d: company, contact, role, vertical, city, phone, domain, rating, reviews,
    opener, offers [names], noticed [strings], copy {offer: (tail, cost, fix)},
    receptionist {picture, with}, date. brand: brand, logo, name, phone, email,
    site, accent."""
    acc = brand.get("accent") or "#e8a33d"
    offers = [o for o in (d.get("offers") or []) if picture_for(o, d)][:2]
    if not offers and d.get("offers"):
        offers = d["offers"][:2]
    copy = d.get("copy") or {}

    # page 1
    logo = ('<img src="%s" alt="">' % brand["logo"]) if brand.get("logo") else ""
    who = e(d.get("contact") or "")
    prep = ('<div class="prep">Prepared for<b>%s</b>%s%s</div>'
            % (e(d["company"]), (who + (" · " + e(d["role"]) if d.get("role") else "") + "<br>") if who else "",
               e(d.get("date", ""))))
    top = '<div class="top"><div class="brand">%s<span>%s</span></div>%s</div>' % (logo, e(brand.get("brand")), prep)
    if d.get("title"):
        title = e(d["title"])
    elif offers:
        title = "%s, here's what we'd fix first" % e(d["company"])
    else:
        title = "A few ideas for %s" % e(d["company"])
    noticed = d.get("noticed") or []
    notice_html = ('<div class="noticed"><h2>What we noticed</h2><ul>%s</ul></div>'
                   % "".join("<li>%s</li>" % e(n) for n in noticed)) if noticed else ""
    blocks = []
    for o in offers:
        p = picture_for(o, d)
        tail, cost, fix = copy.get(o, ("", "", WHY.get(o, "")))
        cost = cost or WHY.get(o, "")
        if p:
            head, today, with_ = p
            blocks.append(
                '<div class="offer"><h3><small>%s</small>%s</h3>'
                '<div class="cols"><div class="today"><div class="lbl">Picture this</div><p>%s</p></div>'
                '<div class="with"><div class="lbl">With %s</div><p>%s</p></div></div>'
                '%s</div>' % (e(o), e(head), e(today), e(o), e(with_),
                               ('<div class="cost"><b>Why it matters:</b> %s</div>' % e(cost)) if cost else ""))
        else:
            blocks.append('<div class="offer"><h3><small>%s</small>%s</h3><p>%s</p><p>%s</p></div>'
                          % (e(o), e(tail.capitalize()), e(cost), e(fix)))
    rest = [o for o in (d.get("offers") or []) if o not in offers][:3]
    if rest and len(noticed) <= 1:        # room check: page 1 is full once "What we noticed" has 2+ lines
        items = []
        for o in rest:
            pic = picture_for(o, d)
            items.append("<li><b>%s</b>%s</li>" % (e(o), (" · " + e(pic[0])) if pic else ""))
        blocks.append('<div class="noticed" style="margin-top:2px"><h2>We also do</h2><ul>%s</ul></div>' % "".join(items))
    contact_bits = " · ".join(e(x) for x in (brand.get("phone"), brand.get("email"), brand.get("site")) if x)
    foot = ('<div class="foot"><div class="who"><b>%s</b>%s%s</div>'
            '<div class="ask">Got 15 minutes this week? I\'ll show you how this would work for %s \u2014 '
            'no cost to talk it through.</div></div>'
            % (e(brand.get("name") or brand.get("brand")), (e(brand["brand"]) + "<br>") if brand.get("name") else "",
               contact_bits, e(d["company"])))
    page1 = ('<section class="page">%s<h1>%s</h1><p class="sub">%s</p>%s%s%s</section>'
             % (top, title,
                e(d["sub"]) if d.get("sub") else
                "We looked %s over the way a new customer would. Here's what stood out, and what "
                "it could look like." % ("at %s" % e(d["domain"]) if d.get("domain") else "your business"),
                notice_html, "".join(blocks), foot))

    # page 2
    mocks = []
    for o in offers:
        m, steps = mock_for(o, d)
        if not m:
            continue
        mocks.append('<div class="mockcol">%s<div class="how"><h3>%s - how it works</h3><ol>%s</ol></div></div>'
                     % (m, e(o), "".join("<li>%s</li>" % e(s) for s in steps)))
    page2 = ""
    if mocks:
        page2 = ('<section class="page">%s<h1>What it looks like</h1>'
                 '<p class="sub">Here\'s the day-to-day version, set up for %s.</p>'
                 '<div class="mocks %s">%s</div>'
                 '<p class="fine">Screens show sample names, numbers and messages to illustrate how it works.</p>'
                 '%s</section>' % (top, e(d["company"]), "one" if len(mocks) == 1 else "", "".join(mocks), foot))

    bar = ""
    if toolbar:
        bar = ('<div class="toolbar"><b>Leave-behind · %s</b><button onclick="window.print()">Print</button>'
               '<a href="%s">Download PDF</a></div>' % (e(d["company"]), e(toolbar)))
    page1, page2 = dash(page1), dash(page2)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>%s - leave-behind</title><style>%s</style></head><body>%s%s%s</body></html>'
            % (e(d["company"]), CSS % {"acc": acc, "tint": tint(acc)}, bar, page1, page2))


def render_many(ds, brand, title="Route", toolbar=None):
    """Every lead's two pages in one printable document - the route stack.
    Each sheet is rendered exactly as it would be alone and the bodies are
    joined, so a single sheet and a stacked one can never drift apart."""
    docs = [render(d, brand) for d in ds]
    if not docs:
        return render({"company": title, "offers": []}, brand)
    head = docs[0][:docs[0].index("<body>")]
    head = re.sub(r"<title>.*?</title>", "<title>%s - leave-behinds</title>" % e(title), head)
    bodies = "".join(doc[doc.index("<body>") + 6:doc.rindex("</body>")] for doc in docs)
    bar = ""
    if toolbar:
        bar = ('<div class="toolbar"><b>%s · %d leave-behind%s</b><button onclick="window.print()">Print all</button>'
               '<a href="%s">Download PDF</a></div>' % (e(title), len(ds), "" if len(ds) == 1 else "s", e(toolbar)))
    return head + "<body>" + bar + bodies + "</body></html>"
