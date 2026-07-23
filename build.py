#!/usr/bin/env python3
"""
Liberty Politics dashboard builder (runs in GitHub Actions).
Pulls fresh video tweets per topic from the X API, scores/labels them,
and regenerates index.html. Fails safe if the API returns too little.

Feedback loop: per-clip up/down votes + optional "why", plus a Send button
that files a GitHub issue (which Claude reads) so feedback reaches Claude
directly - no copy/paste - and includes which tab it came from.
"""
import os, sys, json, time, urllib.parse, urllib.request, datetime

TOKEN = os.environ.get("X_BEARER_TOKEN", "").strip()
SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"
PER_TOPIC = 18            # aim ~18 per topic
PER_STANCE = 7            # aim up to this many of each: for / against / neutral
LIKE_MIN = 1000          # only clips with thousands of likes
FETCH = 100              # pull a big pool per topic, then filter hard
MIN_TOTAL_TO_PUBLISH = 12
REPO = "Armishkana/liberty-dashboard"

# content keywords -> stance, aligned to areas/armin-views.md (used when the account is unknown)
FOR_KW = ["regime change", "free iran", "reza pahlavi", " pahlavi", "woman life freedom",
          "woman, life, freedom", "mahsa", "down with khamenei", "death to khamenei",
          "down with the regime", "iranians rise", "long live"]
AGAINST_KW = ["war criminal", "genocide", "free palestine", "stop bombing", "illegal war",
              "no war", "ceasefire now", "anti-war", "zionist", "quincy", "apartheid",
              "from the river", "stop the war", "hands off iran"]

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
         angle="Good-vs-evil segment: regime/IRGC propaganda to tear apart, opposition (Pahlavi) to amplify. Regime accounts are flagged 'propaganda'."),
    dict(id="netanyahu", name='Netanyahu &amp; the "Days From a Nuke" Claim', accent="#b98cff",
         query='Netanyahu (Iran OR nuclear OR nuke OR bomb OR weapon) has:videos -is:retweet lang:en',
         summary="Anti-war voices are resurfacing old clips of Netanyahu predicting an imminent Iranian bomb, going back 30 years, to discredit the case against the regime.",
         angle="Rebut the 'boy who cried nuke' line: mocking Netanyahu doesn't make the regime less dangerous. Bait to undermine pressure on the Islamic Republic."),
    dict(id="mamdani", name="Mamdani, the ICC &amp; the Arrest Fight", accent="#ff7eb6",
         query='Mamdani (Netanyahu OR ICC OR arrest OR "war criminal") has:videos -is:retweet lang:en',
         summary="The war's biggest domestic story. Mayor Mamdani called Netanyahu a war criminal and urged the feds to execute the ICC warrant. Trump shut it down; Israel's UN ambassador said Mamdani should be arrested.",
         angle="Your lane exactly: Mamdani siding against the man fighting the Islamic Republic. Amplify Danon's and Trump's pushback; rebut the 'war criminal' framing."),
]

ACCOUNTS = {
    "rt_com": ("rage", "state"), "rt_on_x": ("rage", "state"),
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
TOPIC_DEFAULT_STANCE = {"hormuz": "neu", "strikes": "side", "senate": "neu",
                        "regime": "rage", "netanyahu": "rage", "mamdani": "rage"}
STANCE_LABEL = {"rage": ("&#128997; Argue against", "st-rage", "rage"),
                "side": ("&#128994; Your side", "st-side", "side"),
                "neu":  ("&#11036; Neutral", "st-neu", "neu")}
CRED_LABEL = {"ok": ("&#9989; Trusted", "ok"), "official": ("&#128202; Official", "official"),
              "state": ("&#127988; State/regime propaganda", "state"), "warn": ("&#9888;&#65039; Don't trust - hype", "warn"),
              "osint": ("&#128993; Unconfirmed - OSINT", "osint"), "op": ("&#128483;&#65039; Opinion / commentary", "op")}


def api_get(query, max_results=30):
    params = {"query": query, "max_results": str(max_results),
              "sort_order": "relevancy",  # engagement-ranked, not newest-first (surfaces the viral, high-like clips)
              "tweet.fields": "public_metrics,created_at,attachments,lang",
              "expansions": "author_id,attachments.media_keys",
              "user.fields": "username,name,verified", "media.fields": "type"}
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + TOKEN,
                                               "User-Agent": "liberty-dashboard-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def classify(text, uname, topic):
    """Return (stance, credibility). Known account wins; else read the text."""
    if uname in ACCOUNTS:
        return ACCOUNTS[uname]
    t = (text or "").lower()
    for_sig = any(k in t for k in FOR_KW)
    against_sig = any(k in t for k in AGAINST_KW)
    if for_sig and not against_sig:
        return ("side", "op")
    if against_sig and not for_sig:
        return ("rage", "op")
    return (TOPIC_DEFAULT_STANCE.get(topic, "neu"), "op")


def collect_topic(t):
    try:
        data = api_get(t["query"], max_results=FETCH)
    except Exception as e:
        print(f"[{t['id']}] API error: {e}", file=sys.stderr)
        return []
    tweets = data.get("data", []) or []
    inc = data.get("includes", {}) or {}
    users = {u["id"]: u for u in inc.get("users", [])}
    media = {m["media_key"]: m for m in inc.get("media", [])}
    vids = []
    for tw in tweets:
        keys = (tw.get("attachments") or {}).get("media_keys", [])
        if not any(media.get(k, {}).get("type") in ("video", "animated_gif") for k in keys):
            continue
        u = users.get(tw.get("author_id"), {})
        uname = (u.get("username") or "").lower()
        likes = (tw.get("public_metrics", {}) or {}).get("like_count", 0)
        stance, cred = classify(tw.get("text", ""), uname, t["id"])
        vids.append(dict(id=str(tw["id"]), user=u.get("username", "i"),
                         likes=likes, stance=stance, cred=cred))
    vids.sort(key=lambda c: c["likes"], reverse=True)
    # hard quality bar: only thousands of likes. If too few, relax to top-by-likes so the page still fills.
    strong = [c for c in vids if c["likes"] >= LIKE_MIN]
    pool = strong if len(strong) >= 10 else vids[:PER_TOPIC + 6]
    # balance: take up to PER_STANCE of each stance (highest-liked first), then fill by likes
    buckets = {"side": [], "rage": [], "neu": []}
    for c in pool:
        buckets[c["stance"]].append(c)
    picked, seen = [], set()
    for st in ("side", "neu", "rage"):
        for c in buckets[st][:PER_STANCE]:
            picked.append(c); seen.add(c["id"])
    if len(picked) < PER_TOPIC:
        for c in pool:
            if c["id"] not in seen:
                picked.append(c); seen.add(c["id"])
                if len(picked) >= PER_TOPIC:
                    break
    picked.sort(key=lambda c: c["likes"], reverse=True)
    picked = picked[:PER_TOPIC]
    lmax = max((c["likes"] for c in picked), default=1) or 1
    for c in picked:
        c["viral"] = max(6, min(99, round(100 * c["likes"] / lmax)))
    b = {k: sum(1 for c in picked if c["stance"] == k) for k in ("side", "rage", "neu")}
    print(f"[{t['id']}] {len(picked)} clips  (>= {LIKE_MIN} likes: {len(strong)})  for={b['side']} against={b['rage']} neutral={b['neu']}")
    return picked


def tier(v):
    return "exploding" if v >= 90 else "hot" if v >= 70 else "rising" if v >= 45 else "quiet"


def card_html(c, topic):
    slabel, scls, ecls = STANCE_LABEL[c["stance"]]
    clabel, ccls = CRED_LABEL[c["cred"]]
    return (f'<div class="card {ecls}">'
            f'<div class="lbls"><span class="st {scls}">{slabel}</span>'
            f'<span class="vs">&#128293; {c["viral"]}<small>/100 {tier(c["viral"])}</small></span>'
            f'<span class="src {ccls}">{clabel}</span></div>'
            f'<div class="tw" data-id="{c["id"]}"></div>'
            f'<div class="vote" data-id="{c["id"]}" data-topic="{topic}">'
            f'<button class="up" data-v="1" data-id="{c["id"]}">&#128077; Good</button>'
            f'<button class="down" data-v="-1" data-id="{c["id"]}">&#128078; Not this</button></div>'
            f'<textarea class="why" data-id="{c["id"]}" placeholder="why? (optional - helps me learn your taste)"></textarea>'
            f'</div>')


def build_page(topics_data):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%b %d, %H:%M UTC")
    tabs, panels, maps = [], [], []
    allc = [(t["id"], c) for t in TOPICS for c in topics_data.get(t["id"], [])]
    allc.sort(key=lambda x: x[1]["viral"], reverse=True)
    hot = ""
    for tid, c in allc[:3]:
        slabel, scls, _ = STANCE_LABEL[c["stance"]]
        hot += (f'<div class="hotcard">'
                f'<div class="hh"><span class="hv">{c["viral"]}<small>viral</small></span>'
                f'<span class="hl st {scls}">{slabel}</span><span class="ht">{tid.upper()}</span></div>'
                f'<div class="tw" data-id="{c["id"]}"></div>'
                f'<div class="vote" data-id="{c["id"]}" data-topic="{tid}">'
                f'<button class="up" data-v="1" data-id="{c["id"]}">&#128077; Good</button>'
                f'<button class="down" data-v="-1" data-id="{c["id"]}">&#128078; Not this</button></div>'
                f'<textarea class="why" data-id="{c["id"]}" placeholder="why? (optional)"></textarea></div>')
    for t in TOPICS:
        cs = topics_data.get(t["id"], [])
        n = len(cs)
        tabs.append(f'<button class="tabbtn" data-tab="{t["id"]}" style="--accent:{t["accent"]}"><span class="dot"></span>{t["id"].capitalize()} <span class="cnt">{n}</span></button>')
        maps.append(f'<button class="mapcard gotab" data-tab="{t["id"]}" style="--accent:{t["accent"]}"><div class="mh"><h3>{t["name"]}</h3><span class="cnt">{n} clips</span></div><p>{t["summary"][:90]}&hellip;</p><span class="go">Open &rarr;</span></button>')
        cards = "".join(card_html(c, t["id"]) for c in cs) or '<p style="color:#6c7a8b">No fresh clips right now &mdash; check back after the next refresh.</p>'
        panels.append(f'<div class="panel" id="panel-{t["id"]}" style="--accent:{t["accent"]}">'
                      f'<div class="thead"><h2>{t["name"]}</h2><span class="badge">{n} clips</span></div>'
                      f'<p class="summary">{t["summary"]}</p>'
                      f'<div class="angle"><span class="tag">Your angle</span><p>{t["angle"]}</p></div>'
                      f'<div class="cards">{cards}</div></div>')
    page = PAGE_TEMPLATE
    page = page.replace("%%STAMP%%", stamp).replace("%%TABS%%", "".join(tabs))
    page = page.replace("%%HOT%%", hot).replace("%%MAPS%%", "".join(maps))
    page = page.replace("%%PANELS%%", "".join(panels)).replace("%%REPO%%", REPO)
    return page


PAGE_TEMPLATE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Liberty Politics - Show Dashboard</title><meta name="robots" content="noindex, nofollow">
<script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script>
<style>
:root{--bg:#0b0e13;--panel:#141922;--panel-2:#1b2230;--line:#26303f;--line-2:#333f52;--ink:#eef2f7;--ink-dim:#9aa7b8;--ink-faint:#6c7a8b;--red:#ff5252;--rage:#ff5c5c;--side:#4bd08b;--neu:#8493a6;--mono:ui-monospace,Menlo,Consolas,monospace;--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
*{box-sizing:border-box}html,body{background:#0b0e13;margin:0;padding:0;min-height:100%}
body{color:var(--ink);font-family:var(--sans);line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:0 clamp(14px,3vw,40px) 90px}a{color:inherit}
.bar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:14px 2px 10px}
.brand{display:flex;align-items:center;gap:11px}.tally{width:12px;height:12px;border-radius:50%;background:var(--red)}
.brand b{font-size:17px}.brand span{display:block;color:var(--ink-faint);font-size:10px;letter-spacing:.14em;text-transform:uppercase;margin-top:2px}
.clock{font-family:var(--mono);font-size:11px;color:var(--ink-dim);text-align:right}.clock b{color:#ffb02e}
.tabs{position:sticky;top:0;z-index:40;background:rgba(11,14,19,.96);backdrop-filter:blur(12px);border-bottom:1px solid var(--line);display:flex;gap:6px;padding:9px 0;margin-bottom:20px;overflow-x:auto}
.tabbtn{flex:none;cursor:pointer;font:600 13px var(--sans);color:var(--ink-dim);border:1px solid var(--line);background:var(--panel);padding:8px 14px;border-radius:10px;white-space:nowrap;display:flex;align-items:center;gap:7px}
.tabbtn .dot{width:8px;height:8px;border-radius:50%;background:var(--accent,var(--ink-faint))}
.tabbtn .cnt{font:10px var(--mono);color:var(--ink-faint);background:rgba(255,255,255,.05);padding:1px 6px;border-radius:20px}
.tabbtn.active{color:#fff;border-color:var(--accent,#ffb02e);background:color-mix(in srgb,var(--accent,#ffb02e) 16%,var(--panel))}
.panel{display:none}.panel.active{display:block}
.sop{border:1px solid var(--line);border-radius:14px;background:var(--panel);padding:16px 18px;margin:0 0 16px}
.sop h1{margin:0 0 7px;font:600 12px var(--mono);letter-spacing:.15em;text-transform:uppercase;color:#ffb02e}
.sop p{margin:0;font-size:15px;color:var(--ink-dim)}.sop b{color:var(--ink)}
.blocktitle{font:10px var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--ink-faint);margin:22px 2px 10px}
.hot{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.hotcard{border:1px solid var(--line);border-left:4px solid var(--red);border-radius:13px;background:var(--panel);padding:11px 12px;display:flex;flex-direction:column;gap:9px}
.hh{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.hv{font:700 22px var(--mono);color:var(--red);line-height:1}.hv small{font:8px var(--mono);color:var(--ink-faint);text-transform:uppercase;margin-left:3px}
.ht{font:10px var(--mono);color:var(--ink-faint);margin-left:auto}
.map{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.mapcard{cursor:pointer;text-align:left;border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:13px;background:var(--panel);padding:14px 15px;color:inherit;font-family:var(--sans)}
.mapcard .mh{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px}
.mapcard h3{margin:0;font-size:15.5px;color:var(--ink)}.mapcard .cnt{font:11px var(--mono);color:var(--ink-faint)}
.mapcard p{margin:0;font-size:12.5px;color:var(--ink-dim)}.mapcard .go{font:11px var(--mono);color:var(--accent);margin-top:9px;display:inline-block}
.thead{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.thead h2{margin:0;font-size:24px;border-left:5px solid var(--accent);padding-left:12px}
.thead .badge{font:11px var(--mono);color:var(--ink-dim);border:1px solid var(--line);border-radius:20px;padding:3px 10px}
.summary{color:var(--ink-dim);font-size:14.5px;margin:0 0 11px;max-width:90ch}.summary b{color:var(--ink)}
.angle{display:flex;gap:10px;align-items:flex-start;background:color-mix(in srgb,var(--accent) 12%,transparent);border:1px solid color-mix(in srgb,var(--accent) 32%,transparent);border-radius:11px;padding:10px 14px;margin:0 0 18px;max-width:90ch}
.angle .tag{font:10px var(--mono);letter-spacing:.1em;color:var(--accent);text-transform:uppercase;flex:none;padding-top:2px}
.angle p{margin:0;font-size:13.5px;color:var(--ink)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px;align-items:start}
.card{border:1px solid var(--line);border-left:4px solid var(--e,var(--line));border-radius:13px;background:var(--panel);padding:11px 12px;display:flex;flex-direction:column;gap:9px}
.rage{--e:var(--rage)}.side{--e:var(--side)}.neu{--e:var(--neu)}
.lbls{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.st{font:700 11.5px var(--sans);padding:3px 9px;border-radius:7px;white-space:nowrap}
.st-rage{color:#ffdada;background:rgba(255,82,82,.22);border:1px solid rgba(255,82,82,.45)}
.st-side{color:#c8f7de;background:rgba(75,208,139,.18);border:1px solid rgba(75,208,139,.4)}
.st-neu{color:var(--ink-dim);background:rgba(132,147,166,.16);border:1px solid var(--line-2)}
.vs{font:700 11px var(--mono);color:var(--ink);white-space:nowrap}.vs small{color:var(--ink-faint);font-weight:400}
.src{font:600 11px var(--sans);white-space:nowrap}
.src.ok{color:#7fe0ac}.src.official{color:#8cc6ff}.src.warn{color:#ff8f8f}.src.osint{color:#ffcf8a}.src.state{color:#ff9f7a}.src.op{color:var(--ink-dim)}
.tw{min-height:50px}.tw .twitter-tweet{margin:0 !important}
.tw.loading::before{content:"loading video...";color:var(--ink-faint);font:11px var(--mono);display:block;padding:8px 2px}
.vote{display:flex;gap:8px}
.vote button{cursor:pointer;flex:1;font:600 12px var(--sans);color:var(--ink-dim);background:var(--panel-2);border:1px solid var(--line);border-radius:8px;padding:6px 8px}
.vote button:hover{color:var(--ink)}
.vote button.on-up{color:#c8f7de;border-color:var(--side);background:rgba(75,208,139,.16)}
.vote button.on-down{color:#ffdada;border-color:var(--red);background:rgba(255,82,82,.16)}
.why{display:none;width:100%;height:52px;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:7px;font:12.5px var(--sans);resize:vertical}
.why.show{display:block}
footer{margin-top:36px;border-top:1px solid var(--line);padding-top:15px;color:var(--ink-faint);font-size:12px}footer b{color:var(--ink-dim)}
.fbtn{position:fixed;right:18px;bottom:18px;z-index:60;cursor:pointer;font:700 13px var(--sans);color:#0b0e13;background:#ffb02e;border:none;border-radius:999px;padding:11px 16px;box-shadow:0 6px 20px rgba(0,0,0,.5)}
.fpanel{position:fixed;right:18px;bottom:66px;z-index:60;width:min(360px,92vw);background:var(--panel);border:1px solid var(--line-2);border-radius:14px;padding:14px;display:none;box-shadow:0 10px 30px rgba(0,0,0,.6)}
.fpanel.open{display:block}
.fpanel h4{margin:0 0 4px;font-size:14px}.fpanel p{margin:0 0 8px;font-size:11.5px;color:var(--ink-faint)}
.fpanel textarea{width:100%;height:80px;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:8px;font:13px var(--sans);resize:vertical}
.fpanel .frow{display:flex;gap:8px;margin-top:9px}
.fpanel button{cursor:pointer;flex:1;font:700 12px var(--sans);border-radius:8px;padding:9px;border:1px solid var(--line)}
.fpanel .send{color:#0b0e13;background:#ffb02e;border:none}.fpanel .close{color:var(--ink-dim);background:var(--panel-2)}
.fcount{font:11px var(--mono);color:var(--ink-faint);margin-top:8px}
</style></head><body><div class="wrap">
<div class="bar"><div class="brand"><span class="tally"></span><span><b>Liberty Politics - Show Dashboard</b><span>On-air rundown - auto-updated</span></span></div>
<div class="clock">Auto-updated<br><b>%%STAMP%%</b></div></div>
<div class="tabs" id="tabs"><button class="tabbtn active" data-tab="overview" style="--accent:#ffb02e"><span class="dot"></span>Overview</button>%%TABS%%</div>
<div class="panel active" id="panel-overview">
<div class="sop"><h1>State of play</h1><p>Live from X, refreshed automatically. Tap a topic tab for its clips, or a card below to jump in. Rate clips with the thumbs (and say why) so I learn what you like, then hit the Feedback button to send it to me. Each clip is labeled with your stance, a viral score, and how much to trust the source.</p></div>
<div class="blocktitle">Biggest clips right now (playable)</div><div class="hot" id="hotwrap">%%HOT%%</div>
<div class="blocktitle">The topics - tap one to open it</div><div class="map">%%MAPS%%</div>
</div>
%%PANELS%%
<footer><b>Auto-updated from the X API.</b> Viral score = how fast a clip is spreading. Stance is from your show's positions. Source = how much to trust it. Videos load when you open a tab, so first open is fast.</footer>
</div>
<button class="fbtn" id="fbtn">&#128172; Feedback</button>
<div class="fpanel" id="fpanel">
<h4>Send feedback to Claude</h4>
<p>Your thumbs + reasons are saved as you go. Add any overall notes, then Send - it goes straight to me (as a GitHub note), no copy-paste, and tells me which tab you were on.</p>
<textarea id="ftext" placeholder="e.g. Senate tab is too anti-war, make cards bigger, wrong stance on X..."></textarea>
<div class="fcount" id="fcount"></div>
<div class="frow"><button class="send" id="fsend">Send to Claude</button><button class="close" id="fclose">Close</button></div>
</div>
<script>
var LS=window.localStorage,REPO="%%REPO%%",curTab="overview";
function getV(){try{return JSON.parse(LS.getItem('lp_v2')||'{}')}catch(e){return {}}}
function setV(v){LS.setItem('lp_v2',JSON.stringify(v))}
function paint(){var v=getV();document.querySelectorAll('.vote').forEach(function(box){var id=box.dataset.id,o=v[id]||{};box.querySelector('.up').classList.toggle('on-up',o.v===1);box.querySelector('.down').classList.toggle('on-down',o.v===-1);});
document.querySelectorAll('.why').forEach(function(w){var o=v[w.dataset.id];if(o&&o.v){w.classList.add('show');if(o.why!==undefined&&w.value==='')w.value=o.why;}});count();}
function count(){var v=getV(),up=0,dn=0;for(var k in v){if(v[k].v===1)up++;if(v[k].v===-1)dn++;}var e=document.getElementById('fcount');if(e)e.textContent=up+' liked, '+dn+' disliked so far';}
document.addEventListener('click',function(e){var b=e.target.closest('.vote button');if(!b)return;var box=b.closest('.vote'),id=b.dataset.id,val=parseInt(b.dataset.v,10),topic=box.dataset.topic;var v=getV(),o=v[id]||{why:'',topic:topic};o.v=(o.v===val)?0:val;o.topic=topic;if(o.v===0){delete v[id];}else{v[id]=o;}setV(v);
var w=box.parentNode.querySelector('.why');if(w){w.classList.toggle('show',!!v[id]);}paint();});
document.addEventListener('input',function(e){var w=e.target.closest('.why');if(!w)return;var v=getV(),id=w.dataset.id;if(v[id]){v[id].why=w.value;setV(v);}});
var loaded={};
function render(scope){if(!window.twttr||!twttr.widgets)return;scope.querySelectorAll('.tw').forEach(function(el){var tid=el.dataset.id;if(loaded[tid])return;loaded[tid]=1;el.classList.add('loading');twttr.widgets.createTweet(tid,el,{theme:'dark',dnt:true,conversation:'none',align:'center'}).then(function(){el.classList.remove('loading');});});}
function show(id){curTab=id;document.querySelectorAll('.panel').forEach(function(p){p.classList.toggle('active',p.id==='panel-'+id)});document.querySelectorAll('.tabbtn').forEach(function(b){b.classList.toggle('active',b.dataset.tab===id)});var t=document.getElementById('tabs');if(t)t.scrollIntoView({block:'start'});render(document.getElementById('panel-'+id));paint();}
document.querySelectorAll('.tabbtn,.gotab').forEach(function(b){b.addEventListener('click',function(){show(b.dataset.tab)})});
var fp=document.getElementById('fpanel'),ft=document.getElementById('ftext');
if(ft)ft.value=LS.getItem('lp_notes')||'';
document.getElementById('fbtn').onclick=function(){fp.classList.toggle('open');count();};
document.getElementById('fclose').onclick=function(){fp.classList.remove('open');};
if(ft)ft.oninput=function(){LS.setItem('lp_notes',ft.value)};
document.getElementById('fsend').onclick=function(){var v=getV(),lines=[];for(var id in v){var o=v[id];lines.push((o.v===1?'GOOD':'BAD')+' | '+(o.topic||'?')+' | https://x.com/i/status/'+id+(o.why?(' | '+o.why):''));}
var body='Tab I was on: '+curTab+'\n\nMy clip votes:\n'+(lines.join('\n')||'(none yet)')+'\n\nMy notes:\n'+(ft?ft.value:'');
var url='https://github.com/'+REPO+'/issues/new?title='+encodeURIComponent('Dashboard feedback ('+curTab+')')+'&body='+encodeURIComponent(body)+'&labels=feedback';
window.open(url,'_blank');};
paint();
window.addEventListener('load',function(){setTimeout(function(){render(document.getElementById('panel-overview'));},900);});
</script></body></html>"""


def main():
    if not TOKEN:
        print("No X_BEARER_TOKEN set; aborting.", file=sys.stderr)
        sys.exit(1)
    topics_data, total = {}, 0
    for t in TOPICS:
        cs = collect_topic(t)
        topics_data[t["id"]] = cs
        total += len(cs)
        time.sleep(2)
    print(f"Total clips: {total}")
    if total < MIN_TOTAL_TO_PUBLISH:
        print(f"Only {total} clips (< {MIN_TOTAL_TO_PUBLISH}); leaving page untouched.", file=sys.stderr)
        sys.exit(0)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(build_page(topics_data))
    print("index.html written.")


if __name__ == "__main__":
    main()
