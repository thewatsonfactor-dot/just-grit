"""Client-facing HTML report renderer.

Escape everything on the way into the DOM. Analyzer output carries content
lifted from the audited site — page titles, URLs, schema values — and some of
our own fix text contains literal HTML (the tel: link example). Every
interpolation below goes through esc(). A hostile page with <script> and
onerror= payloads in its <title> and meta description must render as inert
text; that's a regression test, not a nice-to-have.
"""

from __future__ import annotations

from html import escape as esc


SEV_COLOR = {"critical": "#d03b3b", "serious": "#ec835a",
             "warning": "#fab219", "notice": "#199e70"}
SEV_LABEL = {"critical": "Critical", "serious": "Serious",
             "warning": "Worth fixing", "notice": "Minor"}


def grade_color(score) -> str:
    s = int(score)
    return "#0ca30c" if s >= 90 else "#8ab61d" if s >= 80 else \
           "#fab219" if s >= 70 else "#ec835a" if s >= 60 else "#d03b3b"


def render_report(d: dict, brand: str = "The Watson Factor") -> str:
    if not d.get("ok"):
        return _shell(
            f"Site check — {esc(d.get('host', ''))}",
            f"""
            <div class="card" style="border-left:4px solid {SEV_COLOR['critical']}">
              <h2>We couldn't load {esc(d.get('host') or d.get('url', 'the site'))}</h2>
              <p>{esc(d.get('error', ''))}</p>
              <p>{esc(d.get('verdict', ''))}</p>
            </div>""", brand)

    host = esc(d["host"])
    overall = int(d["overall"])
    ring = _ring(overall)

    bars = "".join(
        f"""<div class="barrow">
              <div class="lbl">{esc(v['label'])}</div>
              <div class="track"><i style="width:{int(v['score'])}%;
                   background:{grade_color(v['score'])}"></i></div>
              <div class="val" style="color:{grade_color(v['score'])}">
                   {int(v['score'])}</div>
            </div>"""
        for v in d["scores"].values())

    metrics = d["metrics"]
    stats = "".join(
        f"""<div class="stat"><div class="k">{esc(k)}</div>
            <div class="v">{esc(str(v))}</div></div>"""
        for k, v in [
            ("Load time", f"{metrics['load_seconds']:.2f}s"),
            ("Server response", f"{metrics['ttfb_ms']} ms"),
            ("Page weight (est.)", f"{metrics['page_weight_kb']:,} KB"),
            ("Files requested", metrics["requests_declared"]),
            ("Render-blocking", metrics["render_blocking"]),
            ("Tap-to-call links", metrics["tel_links"]),
        ])

    findings = d.get("findings", d.get("priorities", []))
    cards = "".join(
        f"""<div class="card" style="border-left:4px solid {SEV_COLOR[f['severity']]}">
              <div class="sev" style="color:{SEV_COLOR[f['severity']]}">
                   {SEV_LABEL[f['severity']]}</div>
              <h3>{i + 1}. {esc(f['title'])}</h3>
              <p><strong>What we found:</strong> {esc(f['evidence'])}</p>
              <p><strong>Why it matters:</strong> {esc(f['impact'])}</p>
              <p><strong>The fix:</strong> {esc(f['fix'])}</p>
            </div>"""
        for i, f in enumerate(findings))

    det = d["stack"]["detected"]
    chips = []
    if d["stack"]["cms"]:
        chips.append(f'<span class="chip hot">{esc(d["stack"]["cms"])}</span>')
    chips.append(f'<span class="chip {"good" if det.get("analytics") else "bad"}">'
                 f'{esc(" · ".join(det["analytics"])) if det.get("analytics") else "No analytics"}</span>')
    chips.append(f'<span class="chip {"good" if det.get("ads") else "bad"}">'
                 f'{esc(" · ".join(det["ads"])) if det.get("ads") else "No retargeting pixel"}</span>')
    for k, v in det.items():
        if k not in ("analytics", "ads"):
            chips.extend(f'<span class="chip">{esc(t)}</span>' for t in v)

    body = f"""
      <div class="hero">
        {ring}
        <div>
          <div class="eyebrow">Website check — {host}</div>
          <h1>Grade {esc(d['grade'])}</h1>
          <p class="verdict">{esc(d['verdict'])}</p>
        </div>
      </div>
      <div class="stats">{stats}</div>
      <div class="section">{bars}</div>
      <h2>What to fix, in order</h2>
      <p class="cap">Ranked by revenue impact, not by how easy each one is.
         {len(findings)} finding{"s" if len(findings) != 1 else ""} across
         {d.get('pages_checked', 1)} page{"s" if d.get('pages_checked', 1) != 1 else ""} checked.</p>
      {cards}
      <h2>Marketing tools detected</h2>
      <div class="chips">{"".join(chips)}</div>
      <div class="card note">
        <p>{esc(d.get('measurement_note', ''))}</p>
      </div>
      <div class="cta">
        <h2>Want this handled?</h2>
        <p>Every fix above is specific on purpose — hand this report to your
           web person as-is, or we'll do it for you and prove what it changed.
           {esc(brand)} · San Antonio &amp; New Braunfels.</p>
      </div>
    """
    return _shell(f"Site check — {host}", body, brand)


def _ring(score: int) -> str:
    color = grade_color(score)
    pct = max(0, min(100, score))
    return f"""
      <div class="ring" style="background:
           conic-gradient({color} {pct * 3.6}deg, #26262a 0deg)">
        <div class="ring-in"><span style="color:{color}">{score}</span>
          <small>/ 100</small></div>
      </div>"""


def _shell(title: str, body: str, brand: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
  :root {{ --grit:#e8a33d; --ink:#1c1b18; --muted:#6d6a62;
           --paper:#faf8f4; --card:#ffffff; --hair:#e6e2d8; }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;
         color:var(--ink); background:var(--paper); }}
  .wrap {{ max-width:820px; margin:0 auto; padding:34px 22px 70px }}
  .brand {{ display:flex; align-items:center; gap:10px; margin-bottom:28px;
           font-weight:800; letter-spacing:-.01em }}
  .mark {{ width:30px; height:30px; border-radius:8px; display:grid;
          place-items:center; color:#1a1206; font-weight:900;
          background:linear-gradient(145deg,var(--grit),#b4761f) }}
  .hero {{ display:flex; gap:26px; align-items:center; flex-wrap:wrap;
          margin-bottom:22px }}
  .eyebrow {{ font-size:11.5px; letter-spacing:.12em; text-transform:uppercase;
             color:var(--grit); font-weight:700 }}
  h1 {{ margin:4px 0 8px; font-size:30px; letter-spacing:-.02em }}
  h2 {{ margin:34px 0 6px; font-size:19px; letter-spacing:-.01em }}
  h3 {{ margin:2px 0 10px; font-size:16px }}
  .verdict {{ margin:0; max-width:56ch; color:#3c3a34 }}
  .cap {{ margin:0 0 14px; color:var(--muted); font-size:13px }}
  .ring {{ width:118px; height:118px; border-radius:50%; display:grid;
          place-items:center; flex:none }}
  .ring-in {{ width:92px; height:92px; border-radius:50%; background:var(--card);
             display:grid; place-items:center; text-align:center;
             box-shadow:0 1px 4px rgba(0,0,0,.08) }}
  .ring-in span {{ font-size:30px; font-weight:800 }}
  .ring-in small {{ display:block; color:var(--muted); font-size:11px }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(118px,1fr));
           gap:10px; margin:18px 0 }}
  .stat {{ background:var(--card); border:1px solid var(--hair);
          border-radius:12px; padding:12px 14px }}
  .stat .k {{ font-size:11px; letter-spacing:.06em; text-transform:uppercase;
             color:var(--muted) }}
  .stat .v {{ font-size:20px; font-weight:750; margin-top:3px }}
  .section {{ background:var(--card); border:1px solid var(--hair);
             border-radius:14px; padding:18px 20px; margin:8px 0 }}
  .barrow {{ display:grid; grid-template-columns:190px 1fr 36px; gap:12px;
            align-items:center; padding:7px 0 }}
  .barrow .lbl {{ font-size:13.5px; font-weight:600 }}
  .track {{ height:9px; background:#eee9df; border-radius:6px; overflow:hidden }}
  .track i {{ display:block; height:100% }}
  .barrow .val {{ font-weight:750; text-align:right }}
  .card {{ background:var(--card); border:1px solid var(--hair);
          border-radius:14px; padding:16px 18px; margin:12px 0 }}
  .card p {{ margin:6px 0 }}
  .sev {{ font-size:11px; font-weight:800; letter-spacing:.09em;
         text-transform:uppercase }}
  .chips {{ display:flex; gap:7px; flex-wrap:wrap; margin-top:10px }}
  .chip {{ font-size:12.5px; font-weight:600; padding:4px 11px;
          border-radius:999px; background:#efece4; border:1px solid var(--hair) }}
  .chip.good {{ background:#e5f4e5; color:#116611; border-color:#cde5cd }}
  .chip.bad {{ background:#fbe9e9; color:#a02222; border-color:#efd2d2 }}
  .chip.hot {{ background:#fdf3e2; color:#8a5a10; border-color:#f0dfc0 }}
  .note {{ color:var(--muted); font-size:13px }}
  .cta {{ margin-top:36px; padding:22px 24px; border-radius:16px;
         background:linear-gradient(135deg,#fff7e8,#fdefd6);
         border:1px solid #f0dfc0 }}
  .cta h2 {{ margin-top:0 }}
</style></head>
<body><div class="wrap">
  <div class="brand"><div class="mark">JG</div>
    <div>Just Grit Marketing<br>
      <small style="font-weight:600;color:var(--muted);font-size:11px;
        letter-spacing:.1em;text-transform:uppercase">by {esc(brand)}</small>
    </div>
  </div>
  {body}
</div></body></html>"""
