"""The /live page: every clip that is up on YouTube or Instagram, one section per streamer, with links."""
from __future__ import annotations

from . import config as config_mod
from .db import DB

PLATFORMS = ("youtube", "instagram")


def build(db: DB, cfg: dict) -> list[dict]:
    """[{channel, name, clips: [{id, hook, posted_at, thumb, campaign, posts: [{platform, url, views, status}]}]}]"""
    rows = db.query(
        "SELECT p.clip_id, p.platform, p.url, p.views, p.status, p.posted_at, p.campaign_id, "
        "c.channel, c.hook, c.title, c.final_path FROM posts p JOIN clips c ON c.id = p.clip_id "
        f"WHERE p.platform IN ({','.join('?' * len(PLATFORMS))}) AND p.status != 'error' AND p.url != '' "
        "ORDER BY p.posted_at DESC", PLATFORMS)
    channels: dict[str, dict] = {}
    for r in rows:
        ch = channels.setdefault(r["channel"], {"channel": r["channel"], "clips": {}})
        clip = ch["clips"].setdefault(r["clip_id"], {
            "id": r["clip_id"], "hook": r["hook"] or r["title"] or "", "posted_at": r["posted_at"],
            "thumb": bool(r["final_path"]), "campaign": r["campaign_id"] or "", "posts": []})
        clip["posted_at"] = max(clip["posted_at"] or 0, r["posted_at"] or 0)
        clip["posts"].append({"platform": r["platform"], "url": r["url"], "views": r["views"] or 0,
                              "status": r["status"]})
    out = []
    for login, ch in channels.items():
        ccfg = config_mod.channel(cfg, login) or {}
        clips = sorted(ch["clips"].values(), key=lambda c: -(c["posted_at"] or 0))
        out.append({"channel": login, "name": ccfg.get("display_name") or login, "clips": clips})
    return sorted(out, key=lambda c: c["name"].lower())


LIVE_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live Posts</title>
<style>
:root{--bg:#0e0f12;--panel:#17191e;--line:#2a2d35;--text:#e8e9ec;--mute:#9aa0ab;--accent:#ffd23f;--yt:#ff4e45;--ig:#c86bfa;--bad:#ff5d5d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:2;background:rgba(14,15,18,.94);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0}header a{color:var(--accent);text-decoration:none}#when{color:var(--mute);font-size:12px;margin-left:auto}
main{padding:16px;max-width:1100px;margin:0 auto;display:grid;gap:20px}
nav{display:flex;gap:8px;flex-wrap:wrap}nav a{font-size:12px;padding:4px 10px;border:1px solid var(--line);border-radius:999px;color:var(--text);text-decoration:none}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
h2{font-size:18px;margin:0 0 12px;display:flex;gap:10px;align-items:baseline}h2 small{font-size:12px;color:var(--mute);font-weight:400}
.clip{display:grid;grid-template-columns:64px minmax(0,1fr);gap:12px;padding:10px 0;border-top:1px solid var(--line)}
.clip:first-of-type{border-top:0}
.clip img{width:64px;height:114px;object-fit:cover;border-radius:6px;background:#23262e}
.hook{font-weight:600;margin-bottom:4px;overflow-wrap:anywhere}.meta{color:var(--mute);font-size:12px;margin-bottom:8px}
.links{display:flex;gap:8px;flex-wrap:wrap}
.links a{display:inline-flex;gap:6px;align-items:center;padding:5px 10px;border-radius:8px;text-decoration:none;font-size:13px;color:#fff}
.yt{background:var(--yt)}.ig{background:var(--ig)}.links a.down{background:#3a2a2a;color:var(--bad)}
.empty{color:var(--mute);text-align:center;padding:40px}
</style></head><body>
<header><h1>📺 Live posts</h1><a href="/">← Review clips</a><a href="/stats">📊 Stats</a><span id="when">loading…</span></header>
<main id="main"></main>
<script>
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const when=t=>t?new Date(t*1000).toLocaleString([], {dateStyle:"medium",timeStyle:"short"}):"";
const id=s=>"ch-"+s.replace(/[^a-z0-9_]/gi,"");
async function load(){
  const data=await fetch("/api/live").then(r=>r.json());
  const m=document.getElementById("main");
  document.getElementById("when").textContent="updated "+new Date().toLocaleTimeString();
  if(!data.length){m.innerHTML='<div class="empty">Nothing posted to YouTube or Instagram yet.</div>';return}
  const total=data.reduce((n,c)=>n+c.clips.length,0);
  m.innerHTML=`<nav>${data.map(c=>`<a href="#${id(c.channel)}">${esc(c.name)} · ${c.clips.length}</a>`).join("")}</nav>`+
  data.map(c=>`<section id="${id(c.channel)}"><h2>${esc(c.name)} <small>${c.clips.length} clip${c.clips.length==1?"":"s"} live</small></h2>`+
    c.clips.map(k=>`<div class="clip">
      ${k.thumb?`<img src="/thumb/${k.id}.jpg" alt="" loading="lazy">`:"<span></span>"}
      <div><div class="hook">${esc(k.hook)}</div>
      <div class="meta">Clip #${k.id} · posted ${esc(when(k.posted_at))}${k.campaign?` · ${esc(k.campaign)}`:""}</div>
      <div class="links">${k.posts.map(p=>{const down=!["published","ok"].includes(p.status);
        return `<a class="${p.platform==="youtube"?"yt":"ig"}${down?" down":""}" href="${esc(p.url)}" target="_blank" rel="noopener">
        ${p.platform==="youtube"?"▶ YouTube":"◎ Instagram"}${p.views?` · ${Number(p.views).toLocaleString()} views`:""}${down?` · ${esc(p.status)}`:""}</a>`}).join("")}</div></div></div>`).join("")+
    `</section>`).join("");
  document.title=`Live Posts (${total})`;
}
load();setInterval(load,60000);
</script></body></html>
"""
