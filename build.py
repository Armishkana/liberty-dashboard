#!/usr/bin/env python3
"""
Liberty Politics dashboard builder.
Runs in GitHub Actions. Pulls fresh video tweets per topic from the X API,
scores/labels them, and regenerates index.html. Designed to fail safe:
if the API returns too little, it leaves the existing page untouched.
"""
import os, sys, json, time, math, urllib.parse, urllib.request, datetime

TOKEN = os.environ.get("X_BEARER_TOKEN", "").strip()
SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"
PER_TOPIC = 15            # target clips per topic
MIN_TOTAL_TO_PUBLISH = 12 # safety: don't overwrite the page with a near-empty build

# ---- topic config (summary + angle are curated; queries drive the API) ----
TOPICS = [
    dict(id="hormuz", name="Naval War &amp; the Strait of Hormuz", accent="#5aa9ff",
         query='(Hormuz OR "Strait of Hormuz") (Iran OR US OR Rubio OR Trump OR IRGC OR Navy) has:videos -is:retweet lang:en',
         summary="Iran claims it controls the strait; CENTCOM and Rubio flatly reject that. Trump threatened Iran's bridges and power plants, then said the US doesn't need the strait anyway. Ship traffic is collapsing; oil keeps climbing.",
         angle="Play CENTCOM's and Rubio's line against the regime's claim, and flag Trump's contradiction. Watch the RT clips: that's the regime framing to rebut."),
    dict(id="strikes", name="US Strikes &amp; Escalation", accent="#ff5252",
         query='(Iran OR IRGC OR Tehran) (strike OR strikes OR airstrike OR bombed OR missile OR base) has:videos -is:retweet lang:en',
         summary="The strike map keeps widening: ships, then IRGC bases, then water plants. The info war is just as hot, with the biggest 'battle damage' clips coming from hype accounts with unconfirmed numbers.",
         angle="Cheer the real hits on the IRGC, but separate them from the disinfo. Flag the hype accounts (marked 'don't trust') live."),
    dict(id="senate", name="The Senate Reckoning", accent="#4bd08b",
         query='(Hegseth OR "Senate hearing" OR "war funding") (Iran OR war OR strikes OR $87) has:videos -is:retweet lang:en',
         summary="Hegseth asked for $87.6B more after admitting the war cost $37.5B. Anti-war senators and protesters push the 'US war crimes' framing, the narrative your show exists to rebut.",
         angle="Rebut the war-crime / 'stop bombing' framing: the regime is the villain. Straight GOP oversight is fair game (neutral); the anti-war framing is not."),
    dict(id="regime", name="Iran's Regime &amp; the Opposition", accent="#ffb02e",
         query='(Khamenei OR "Islamic Republic" OR IRGC OR Tasnim OR Pahlavi OR opposition) (Iran OR regime OR Iranians) has:videos -is:retweet lang:en',
         summary="The regime and its IRGC broadcast defiance and brag about operations. Against that, the opposition (Reza Pahlavi) appeals directly to Iranians.",
         angle="Good-vs-evil segment: regime/IRGC propaganda to tear apart, opposition to amplify. Regime accounts are flagged 'propaganda'."),
    dict(id="netanyahu", name='Netanyahu &amp; the "Days From a Nuke" Claim', accent="#b98cff",
         query='Netanyahu (Iran OR nuclear OR nuke OR bomb OR weapon) has:videos -is:retweet lang:en',
         summary="Anti-war voices are resurfacing old clips of Netanyahu predicting an imminent Iranian bomb, going back 30 years, to discredit the case against the regime.",
         angle="Rebut the 'boy who cried nuke' line: mocking Netanyahu doesn't make the regime less dangerous. Bait to undermine pressure on the Islamic Republic."),
    dict(id="mamdani", name="Mamdani, the ICC &amp; the Arrest Fight", accent="#ff7eb6",
         query='Mamdani (Netanyahu OR ICC OR arrest OR "war criminal") has:videos -is:retweet lang:en',
         summary="The war's biggest domestic story. Mayor Mamdani called Netanyahu a war criminal and urged the feds to execute the ICC warrant. Trump shut it down; Israel's UN ambassador said Mamdani should be arrested.",
         angle="Your lane exactly: Mamdani siding against the man fighting the Islamic Republic. Amplify Danon's and Trump's pushback; rebut the 'war criminal' framing."),
]

# ---- curated account intelligence (username lowercased) ----
# stance: rage | side | neu ; cred: ok|official|state|warn|osint|op
ACCOUNTS = {
    "rt_com": ("rage", "state"), "rt_on_x": ("neu", "state"),
    "tasnimnews_en": ("rage", "state"), "irgc_press": ("rage", "state"),
    "presstv": ("rage", "state"), "iranintl_en": ("side", "ok"),
    "pahlavireza": ("side", "op"),
    "centcom": ("side", "official"), "dod": ("neu", "official"), "statedept": ("neu", "official"),
    "reuters": ("neu", "ok"), "ap": ("neu", "ok"), "apnews": ("neu", "ok"),
    "bbcworld": ("neu", "ok"), "cnn": ("neu", "ok"), "watcherguru": ("neu", "ok"),
    "atrupar": ("neu", "ok"), "acyn": ("neu", "ok"), "aaronrupar": ("neu", "ok"),
    "popbase": ("rage", "ok"), "pop_base": ("rage", "ok"),
    "nycmayor": ("rage", "official"),
    "usairforce_x": ("side", "warn"), "warhorizon": ("side", "osint"),
    "eyakoby": ("side", "op"), "emilykschrader": ("side", "op"), "dannydanon": ("side", "op"),
    "israelnewspulse": ("rage", "op"), "partisan_12": ("rage", "op"),
    "owenshroyer1776": ("rage", "op"), "nothoodlum": ("rage", "op"),
    "imperiumfirst": ("neu", "op"),
}
# per-topic default stance for unknown accounts (kept conservative)
TOPIC_DEFAULT_STANCE = {"hormuz": "neu", "strikes": "side", "senate": "neu",
                        "regime": "rage", "netanyahu": "rage", "mamdani": "rage"}

STANCE_LABEL = {"rage": ("🟥 Argue against", "st-rage", "rage"),
                "side": ("🟩 Your side", "st-side", "side"),
                "neu":  ("⬜ Neutral", "st-neu", "neu")}
CRED_LABEL = {"ok": ("✅ Trusted", "ok"), "official": ("📊 Official", "official"),
              "state": ("🏴 State/regime propaganda", "state"), "warn": ("⚠️ Don't trust — hype", "warn"),
              "osint": ("🟡 Unconfirmed — OSINT", "osint"), "op": ("🗣️ Opinion / commentary", "op")}


def api_get(query, max_results=30):
    params = {
        "query": query,
        "max_results": str(max_results),
        "tweet.fields": "public_metrics,created_at,attachments,lang",
        "expansions": "author_id,attachments.media_keys",
        "user.fields": "username,name,verified",
        "media.fields": "type",
    }
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + TOKEN,
                                               "User-Agent": "liberty-dashboard-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def collect_topic(t):
    """Return a list of selected clip dicts for a topic."""
    try:
        data = api_get(t["query"])
    except Exception as e:
        print(f"[{t['id']}] API error: {e}", file=sys.stderr)
        return []
    tweets = data.get("data", []) or []
    inc = data.get("includes", {}) or {}
    users = {u["id"]: u for u in inc.get("users", [])}
    media = {m["media_key"]: m for m in inc.get("media", [])}
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for tw in tweets:
        keys = (tw.get("attachments") or {}).get("media_keys", [])
        has_video = any(media.get(k, {}).get("type") in ("video", "animated_gif") for k in keys)
        if not has_video:
            continue
        u = users.get(tw.get("author_id"), {})
        uname = (u.get("username") or "").lower()
        pm = tw.get("public_metrics", {}) or {}
        try:
            created = datetime.datetime.fromisoformat(tw["created_at"].replace("Z", "+00:00"))
            age_h = max((now - created).total_seconds() / 3600.0, 0.5)
        except Exception:
            age_h = 12.0
        eng = pm.get("like_count", 0) + 2 * pm.get("retweet_count", 0) + pm.get("quote_count", 0)
        velocity = eng / age_h
        stance, cred = ACCOUNTS.get(uname, (TOPIC_DEFAULT_STANCE.get(t["id"], "neu"), "op"))
        out.append(dict(url=f"https://twitter.com/{u.get('username','i')}/status/{tw['id']}",
                        velocity=velocity, eng=eng, likes=pm.get("like_count", 0),
                        age_h=age_h, stance=stance, cred=cred))
    # rank by velocity, keep top PER_TOPIC
    out.sort(key=lambda c: c["velocity"], reverse=True)
    out = out[:PER_TOPIC]
    # assign 0-100 viral score relative to this topic's top clip
    vmax = max((c["velocity"] for c in out), default=1) or 1
    for c in out:
        c["viral"] = max(6, min(99, round(100 * c["velocity"] / vmax)))
    print(f"[{t['id']}] {len(out)} video clips")
    return out


def tier(v):
    return "exploding" if v >= 90 else "hot" if v >= 70 else "rising" if v >= 45 else "quiet"


def card_html(c):
    slabel, scls, ecls = STANCE_LABEL[c["stance"]]
    clabel, _ = CRED_LABEL[c["cred"]]
    src_cls = CRED_LABEL[c["cred"]][1]
    return f'''<div class="card {ecls}"><div class="lbls"><span class="st {scls}">{slabel}</span><span class="vs">🔥 {c['viral']}<small>/100 {tier(c['viral'])}</small></span><span class="src {src_cls}">{clabel}</span></div>
<blockquote class="twitter-tweet" data-theme="dark" data-dnt="true"><a href="{c['url']}"></a></blockquote></div>'''


def build_page(topics_data):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%b %d, %H:%M UTC")
    tabs, panels, maps = [], [], []
    # hottest 3 across all topics
    allc = [(t["id"], c) for t in TOPICS for c in topics_data.get(t["id"], [])]
    allc.sort(key=lambda x: x[1]["viral"], reverse=True)
    hot = ""
    for tid, c in allc[:3]:
        s = STANCE_LABEL[c["stance"]][0]
        hot += f'<a class="hotcard" href="{c["url"].replace("twitter.com","x.com")}" target="_blank" rel="noopener"><span class="num"><span class="v">{c["viral"]}</span><span class="l">viral</span></span><span class="t">{s} — top clip<b>{tid.upper()}</b></span></a>'
    for t in TOPICS:
        cs = topics_data.get(t["id"], [])
        n = len(cs)
        tabs.append(f'<button class="tabbtn" data-tab="{t["id"]}" style="--accent:{t["accent"]}"><span class="dot"></span>{t["id"].capitalize()} <span class="cnt">{n}</span></button>')
        maps.append(f'<button class="mapcard gotab" data-tab="{t["id"]}" style="--accent:{t["accent"]}"><div class="mh"><h3>{t["name"]}</h3><span class="cnt">{n} clips</span></div><p>{t["summary"][:90]}…</p><span class="go">Open →</span></button>')
        cards = "\n".join(card_html(c) for c in cs) or '<p style="color:#6c7a8b">No fresh clips right now — check back after the next refresh.</p>'
        panels.append(f'''<div class="panel" id="panel-{t['id']}" style="--accent:{t['accent']}">
<div class="thead"><h2>{t['name']}</h2><span class="badge">{n} clips</span></div>
<p class="summary">{t['summary']}</p>
<div class="angle"><span class="tag">Your angle</span><p>{t['angle']}</p></div>
<div class="cards">{cards}</div></div>''')
    return PAGE_TEMPLATE.format(stamp=stamp, tabs="\n".join(tabs), hot=hot,
                                maps="\n".join(maps), panels="\n".join(panels))


PAGE_TEMPLATE = r'''<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Liberty Politics — Show Dashboard</title><meta name="robots" content="noindex, nofollow">
<script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script>
<style>
:root{{--bg:#0b0e13;--panel:#141922;--panel-2:#1b2230;--line:#26303f;--line-2:#333f52;--ink:#eef2f7;--ink-dim:#9aa7b8;--ink-faint:#6c7a8b;--red:#ff5252;--rage:#ff5c5c;--side:#4bd08b;--neu:#8493a6;--mono:ui-monospace,Menlo,Consolas,monospace;--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
*{{box-sizing:border-box}}html,body{{background:#0b0e13;margin:0;padding:0;min-height:100%}}
body{{color:var(--ink);font-family:var(--sans);line-height:1.5;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1180px;margin:0 auto;padding:0 clamp(14px,3vw,40px) 80px}}a{{color:inherit}}
.bar{{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:14px 2px 10px}}
.brand{{display:flex;align-items:center;gap:11px}}.tally{{width:12px;height:12px;border-radius:50%;background:var(--red)}}
.brand b{{font-size:17px}}.brand span{{display:block;color:var(--ink-faint);font-size:10px;letter-spacing:.14em;text-transform:uppercase;margin-top:2px}}
.clock{{font-family:var(--mono);font-size:11px;color:var(--ink-dim);text-align:right}}.clock b{{color:#ffb02e}}
.tabs{{position:sticky;top:0;z-index:40;background:rgba(11,14,19,.96);backdrop-filter:blur(12px);border-bottom:1px solid var(--line);display:flex;gap:6px;padding:9px 0;margin-bottom:20px;overflow-x:auto}}
.tabbtn{{flex:none;cursor:pointer;font:600 13px var(--sans);color:var(--ink-dim);border:1px solid var(--line);background:var(--panel);padding:8px 14px;border-radius:10px;white-space:nowrap;display:flex;align-items:center;gap:7px}}
.tabbtn .dot{{width:8px;height:8px;border-radius:50%;background:var(--accent,var(--ink-faint))}}
.tabbtn .cnt{{font:10px var(--mono);color:var(--ink-faint);background:rgba(255,255,255,.05);padding:1px 6px;border-radius:20px}}
.tabbtn.active{{color:#fff;border-color:var(--accent,#ffb02e);background:color-mix(in srgb,var(--accent,#ffb02e) 16%,var(--panel))}}
.panel{{display:none}}.panel.active{{display:block}}
.sop{{border:1px solid var(--line);border-radius:14px;background:var(--panel);padding:16px 18px;margin:0 0 16px}}
.sop h1{{margin:0 0 7px;font:600 12px var(--mono);letter-spacing:.15em;text-transform:uppercase;color:#ffb02e}}
.sop p{{margin:0;font-size:15px;color:var(--ink-dim)}}.sop b{{color:var(--ink)}}
.blocktitle{{font:10px var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--ink-faint);margin:22px 2px 10px}}
.hot{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}}
.hotcard{{display:flex;gap:12px;align-items:center;border:1px solid var(--line);border-left:4px solid var(--red);border-radius:12px;background:linear-gradient(180deg,var(--panel-2),var(--panel));padding:12px 13px;text-decoration:none;color:inherit}}
.hotcard .num{{text-align:center;flex:none}}.hotcard .v{{font:700 23px var(--mono);color:var(--red);line-height:1}}
.hotcard .l{{font:8px var(--mono);letter-spacing:.06em;color:var(--ink-faint);text-transform:uppercase;margin-top:3px;display:block}}
.hotcard .t{{font-size:12.5px}}.hotcard .t b{{color:var(--ink-faint);font:600 10px var(--mono);display:block;margin-top:4px}}
.map{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}}
.mapcard{{cursor:pointer;text-align:left;border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:13px;background:var(--panel);padding:14px 15px;color:inherit;font-family:var(--sans)}}
.mapcard .mh{{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px}}
.mapcard h3{{margin:0;font-size:15.5px;color:var(--ink)}}.mapcard .cnt{{font:11px var(--mono);color:var(--ink-faint)}}
.mapcard p{{margin:0;font-size:12.5px;color:var(--ink-dim)}}.mapcard .go{{font:11px var(--mono);color:var(--accent);margin-top:9px;display:inline-block}}
.thead{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px}}
.thead h2{{margin:0;font-size:24px;border-left:5px solid var(--accent);padding-left:12px}}
.thead .badge{{font:11px var(--mono);color:var(--ink-dim);border:1px solid var(--line);border-radius:20px;padding:3px 10px}}
.summary{{color:var(--ink-dim);font-size:14.5px;margin:0 0 11px;max-width:90ch}}.summary b{{color:var(--ink)}}
.angle{{display:flex;gap:10px;align-items:flex-start;background:color-mix(in srgb,var(--accent) 12%,transparent);border:1px solid color-mix(in srgb,var(--accent) 32%,transparent);border-radius:11px;padding:10px 14px;margin:0 0 18px;max-width:90ch}}
.angle .tag{{font:10px var(--mono);letter-spacing:.1em;color:var(--accent);text-transform:uppercase;flex:none;padding-top:2px}}
.angle p{{margin:0;font-size:13.5px;color:var(--ink)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px;align-items:start}}
.card{{border:1px solid var(--line);border-left:4px solid var(--e,var(--line));border-radius:13px;background:var(--panel);padding:11px 12px;display:flex;flex-direction:column;gap:9px}}
.rage{{--e:var(--rage)}}.side{{--e:var(--side)}}.neu{{--e:var(--neu)}}
.lbls{{display:flex;align-items:center;gap:7px;flex-wrap:wrap}}
.st{{font:700 11.5px var(--sans);padding:3px 9px;border-radius:7px;white-space:nowrap}}
.st-rage{{color:#ffdada;background:rgba(255,82,82,.22);border:1px solid rgba(255,82,82,.45)}}
.st-side{{color:#c8f7de;background:rgba(75,208,139,.18);border:1px solid rgba(75,208,139,.4)}}
.st-neu{{color:var(--ink-dim);background:rgba(132,147,166,.16);border:1px solid var(--line-2)}}
.vs{{font:700 11px var(--mono);color:var(--ink);white-space:nowrap}}.vs small{{color:var(--ink-faint);font-weight:400}}
.src{{font:600 11px var(--sans);white-space:nowrap}}
.src.ok{{color:#7fe0ac}}.src.official{{color:#8cc6ff}}.src.warn{{color:#ff8f8f}}.src.osint{{color:#ffcf8a}}.src.state{{color:#ff9f7a}}.src.op{{color:var(--ink-dim)}}
.card .twitter-tweet{{margin:0 !important}}
footer{{margin-top:36px;border-top:1px solid var(--line);padding-top:15px;color:var(--ink-faint);font-size:12px}}footer b{{color:var(--ink-dim)}}
</style></head><body><div class="wrap">
<div class="bar"><div class="brand"><span class="tally"></span><span><b>Liberty Politics — Show Dashboard</b><span>On-air rundown · auto-updated</span></span></div>
<div class="clock">Auto-updated<br><b>{stamp}</b></div></div>
<div class="tabs" id="tabs"><button class="tabbtn active" data-tab="overview" style="--accent:#ffb02e"><span class="dot"></span>Overview</button>
{tabs}</div>
<div class="panel active" id="panel-overview">
<div class="sop"><h1>State of play</h1><p>Live from X, refreshed automatically. Tap a topic tab for its clips, or a card below to jump in. Each clip is labeled with your stance, a viral score, and how much to trust the source.</p></div>
<div class="blocktitle">🔥 Biggest clips right now</div><div class="hot">{hot}</div>
<div class="blocktitle">📁 The topics — tap one to open it</div><div class="map">{maps}</div>
</div>
{panels}
<footer><b>Auto-updated from the X API.</b> Viral score = how fast a clip is spreading (engagement ÷ hours live). Stance is from your show's positions. Source = how much to trust it. Labels for known accounts are precise; unknown accounts default to neutral/opinion until reviewed.</footer>
</div>
<script>
function show(id){{document.querySelectorAll('.panel').forEach(function(p){{p.classList.toggle('active',p.id==='panel-'+id)}});document.querySelectorAll('.tabbtn').forEach(function(b){{b.classList.toggle('active',b.dataset.tab===id)}});var t=document.getElementById('tabs');if(t)t.scrollIntoView({{block:'start'}});if(window.twttr&&window.twttr.widgets)window.twttr.widgets.load(document.getElementById('panel-'+id));}}
document.querySelectorAll('.tabbtn,.gotab').forEach(function(b){{b.addEventListener('click',function(){{show(b.dataset.tab)}})}});
</script></body></html>'''


def main():
    if not TOKEN:
        print("No X_BEARER_TOKEN set; aborting.", file=sys.stderr)
        sys.exit(1)
    topics_data = {}
    total = 0
    for t in TOPICS:
        cs = collect_topic(t)
        topics_data[t["id"]] = cs
        total += len(cs)
        time.sleep(2)  # be gentle on rate limits
    print(f"Total clips: {total}")
    if total < MIN_TOTAL_TO_PUBLISH:
        print(f"Only {total} clips (< {MIN_TOTAL_TO_PUBLISH}); leaving existing page untouched.", file=sys.stderr)
        sys.exit(0)
    page = build_page(topics_data)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(page)
    print("index.html written.")


if __name__ == "__main__":
    main()
