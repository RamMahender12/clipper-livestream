"""The /stats page: one screen for how the operation is doing. Data comes from /api/stats (stats.build)."""

STATS_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clipper Stats</title>
<style>
:root{--bg:#0e0f12;--panel:#17191e;--line:#2a2d35;--text:#e8e9ec;--mute:#9aa0ab;--accent:#ffd23f;--ok:#3ecf8e;--bad:#ff5d5d;--warn:#ffa94d;--yt:#ff4e45;--ig:#c86bfa;--pk:#5aa9ff;--vc:#ffd23f;--jd:#3ecf8e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:2;background:rgba(14,15,18,.94);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0}header a{color:var(--accent);text-decoration:none}#when{color:var(--mute);font-size:12px;margin-left:auto}
main{padding:16px;display:grid;grid-template-columns:minmax(0,1fr);gap:16px;max-width:1280px;margin:0 auto}
main>*,.grid2>*{min-width:0}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:0 0 10px}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}
.kpi,.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.kpi .v{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}.kpi .s{color:var(--mute);font-size:12px;margin-top:2px}
.pos{color:var(--ok)}.neg{color:var(--bad)}.mute{color:var(--mute)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap;vertical-align:top}
th{color:var(--mute);font-weight:500;font-size:12px}td.num,th.num{text-align:right}
td.wrap{white-space:normal;min-width:220px;color:var(--mute);font-size:12px}
a{color:var(--accent)}
.bar{height:8px;background:#23262e;border-radius:99px;overflow:hidden;min-width:120px}.bar i{display:block;height:100%;background:var(--accent)}
.chart{display:flex;align-items:flex-end;gap:8px;height:150px;padding-top:6px}
.col{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;min-width:0}
.stack{width:100%;max-width:46px;display:flex;flex-direction:column-reverse;border-radius:6px 6px 0 0;overflow:hidden;background:#1d2026}
.stack i{display:block;width:100%}.lab{font-size:11px;color:var(--mute)}.tot{font-size:11px}
.legend{display:flex;gap:12px;font-size:12px;color:var(--mute);margin-top:8px;flex-wrap:wrap}.legend b{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.pill{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--mute)}
.note{color:var(--mute);font-size:12px}
@media (max-width:600px){main{padding:12px}.kpi .v{font-size:20px}}
</style></head><body>
<header><h1>📊 GTA 6 Clipper · Stats</h1><a href="/">← Review clips</a><span id="when">loading…</span></header>
<main>
  <section class="kpis" id="kpis"></section>
  <section class="panel"><h2>Campaigns</h2><div class="scroll"><table id="camps"></table></div>
    <p class="note">Per-view campaigns pay rate × views for each post that reaches the campaign's minimum views (capped per clip). Pools pay nothing until our posts together reach the threshold; then a share of the pool, known only when the campaign ends.</p></section>
  <section class="grid2">
    <div class="panel"><h2>Views per day (all posts, end of day)</h2><div class="chart" id="vchart"></div>
      <div class="legend"><span><b style="background:var(--ig)"></b>Instagram</span><span><b style="background:var(--yt)"></b>YouTube</span></div></div>
    <div class="panel"><h2>Claude spend per day</h2><div class="chart" id="schart"></div>
      <div class="legend"><span><b style="background:var(--vc)"></b>video check</span><span><b style="background:var(--jd)"></b>judge</span><span><b style="background:var(--pk)"></b>packaging</span></div></div>
  </section>
  <section class="grid2">
    <div class="panel"><h2>Platforms</h2><div class="scroll"><table id="plats"></table></div></div>
    <div class="panel"><h2>Spend by streamer</h2><div class="scroll"><table id="spend"></table></div><p class="note" id="costnote"></p></div>
  </section>
  <section class="panel"><h2>Clip funnel per streamer (last 24 h)</h2><div class="scroll"><table id="funnel"></table></div></section>
  <section class="panel"><h2>Top posts</h2><div class="scroll"><table id="top"></table></div></section>
  <section class="grid2">
    <div class="panel"><h2>Health</h2><div id="health"></div></div>
    <div class="panel"><h2>Takedowns &amp; pauses</h2><div id="takedowns"></div></div>
  </section>
</main>
<script>
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const n=x=>Number(x||0).toLocaleString();const usd=x=>(x<0?"-":"")+"$"+Math.abs(Number(x||0)).toFixed(2);
const pct=x=>(x*100).toFixed(x<0.01?2:1)+"%";const plat={youtube:"YouTube",instagram:"Instagram"};
const ago=t=>{const m=Math.round((Date.now()/1000-t)/60);return m<60?m+" min":m<2880?Math.round(m/60)+" h":Math.round(m/1440)+" d"};
function kpi(label,value,sub,cls=""){return `<div class="kpi"><div class="s">${label}</div><div class="v ${cls}">${value}</div><div class="s">${sub||""}</div></div>`}
function chart(el,rows,keys,colors,fmt){const max=Math.max(1e-9,...rows.map(r=>keys.reduce((a,k)=>a+(r[k]||0),0)));
 el.innerHTML=rows.map(r=>{const t=keys.reduce((a,k)=>a+(r[k]||0),0);
  const none=keys.every(k=>r[k]==null);
  return `<div class="col"><span class="tot">${none?"–":t?fmt(t):""}</span><div class="stack" style="height:${Math.max(2,t/max*110)}px">${keys.map((k,i)=>`<i style="height:${t?(r[k]||0)/t*100:0}%;background:${colors[i]}"></i>`).join("")}</div><span class="lab">${esc(r.day)}</span></div>`}).join("")}
async function load(){
 const s=await (await fetch("/api/stats")).json();const o=s.overview;
 document.getElementById("when").textContent="updated "+new Date(s.generated_at*1000).toLocaleTimeString()+" · refreshes every minute";
 document.getElementById("kpis").innerHTML=
  kpi("Views (all posts)",n(o.views),`+${n(o.gained_24h)} in 24 h`)+
  kpi("Posts",n(o.posts),`${o.days_running} days running`)+
  kpi("Earned (est.)",usd(o.earned),"per-view campaigns; pools pay later","pos")+
  kpi("Spend",usd(o.spend),`AI ${usd(o.ai_spend)} · server ${usd(o.server_spend)}`)+
  kpi("Cloud cost (est.)",usd(o.server_spend),o.server_per_day?`${usd(o.server_per_day)}/day · ~${usd(o.server_per_day*30)}/month${o.server_provider?" · "+esc(o.server_provider):""}`:"set [costs] in config.toml")+
  (o.top_clip?kpi("Top clip (YouTube + Instagram)",n(o.top_clip.views)+" views",
    `#${o.top_clip.clip_id} · ${esc(o.top_clip.streamer)}<br>${["youtube","instagram"].map(k=>o.top_clip.links[k]?`<a href="${esc(o.top_clip.links[k].url)}" target="_blank" rel="noopener">${plat[k]} ${n(o.top_clip.links[k].views)}</a>`:`${plat[k]} –`).join(" · ")}<br>${esc(o.top_clip.title)}`)
   :kpi("Top clip (YouTube + Instagram)","–","nothing posted yet"))+
  kpi("Net",usd(o.net),o.net<0?"to recover":"in profit",o.net<0?"neg":"pos")+
  kpi("Claude today",usd(o.ai_today),`cap ${usd(o.ai_cap)} · ${o.ai_cap?pct(o.ai_today/o.ai_cap):""}`,o.ai_cap&&o.ai_today>o.ai_cap*.8?"neg":"");
 document.getElementById("camps").innerHTML="<tr><th>Campaign</th><th>Type</th><th class=num>Posts</th><th class=num>Views</th><th class=num>+24 h</th><th class=num>Avg</th><th class=num>Best</th><th class=num>Earned</th><th class=num>Spend</th><th class=num>Net</th><th>Breakeven / progress</th></tr>"+
  s.campaigns.map(c=>{let be;
   if(c.kind==="pool"){be=`<div class="bar"><i style="width:${Math.min(100,(c.progress||0)*100)}%"></i></div><div class="mute">${n(c.views)} / ${n(c.pool_min_views)} views (${pct(c.progress||0)}) · ${n(c.views_to_threshold)} to go${c.breakeven.pool_share_needed!=null?` · then need ≥ ${pct(c.breakeven.pool_share_needed)} of the ${usd(c.budget)} pool`:""}</div>`}
   else{const b=c.breakeven;be=!c.spend&&!c.earned?`<span class="mute">nothing spent or earned yet</span>`:b.usd_needed>0?`need ${usd(b.usd_needed)} more: ${n(b.views_needed)} paying views, e.g. ${n(b.clips_at_min)} clips at ${n(c.min_views)} views<div class="mute">${c.paying_posts} posts past ${n(c.min_views)} so far · $${c.rate_per_1k}/1K${c.cap?` · max $${c.cap}/clip`:""}</div>`:`<span class="pos">broken even</span>`}
   return `<tr><td><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.name)}</a><div class="mute">${esc(c.streamers.join(", "))}</div></td><td><span class="pill">${c.kind}</span></td><td class=num>${n(c.posts)}</td><td class=num>${n(c.views)}</td><td class=num>+${n(c.gained_24h)}</td><td class=num>${n(c.avg_views)}</td><td class=num>${n(c.best)}</td><td class="num pos">${usd(c.earned)}</td><td class=num>${usd(c.spend)}</td><td class="num ${c.net<0?"neg":"pos"}">${usd(c.net)}</td><td>${be}</td></tr>`}).join("");
 chart(document.getElementById("vchart"),s.views_series,["instagram","youtube"],["var(--ig)","var(--yt)"],n);
 chart(document.getElementById("schart"),s.spend_series,["video_check","judge","packaging"],["var(--vc)","var(--jd)","var(--pk)"],v=>"$"+v.toFixed(2));
 document.getElementById("plats").innerHTML="<tr><th>Platform</th><th class=num>Posts</th><th class=num>Views</th><th class=num>Avg / post</th><th class=num>+24 h</th></tr>"+
  Object.entries(s.platforms).map(([k,p])=>`<tr><td>${plat[k]||k}</td><td class=num>${n(p.posts)}</td><td class=num>${n(p.views)}</td><td class=num>${n(p.avg)}</td><td class=num>+${n(p.gained_24h)}</td></tr>`).join("");
 document.getElementById("spend").innerHTML="<tr><th>Streamer</th><th class=num>Claude (metered)</th><th class=num>Shared costs</th><th class=num>Total</th></tr>"+
  Object.entries(s.spend_by_streamer).map(([k,v])=>`<tr><td>${esc(k)}</td><td class=num>${usd(v.ai_metered)}</td><td class=num>${usd(v.shared)}</td><td class=num>${usd(v.total)}</td></tr>`).join("");
 document.getElementById("costnote").textContent=s.notes.costs;
 document.getElementById("funnel").innerHTML="<tr><th>Streamer</th><th class=num>Made</th><th class=num>Rendered</th><th class=num>Video-checked</th><th class=num>Approved</th><th class=num>of which picks</th><th class=num>Posted</th><th class=num>Waiting for you</th><th>Mostly not posted because</th></tr>"+
  s.funnel.map(f=>`<tr><td>${esc(f.streamer)}</td><td class=num>${n(f.made)}</td><td class=num>${n(f.rendered)}</td><td class=num>${n(f.video_checked)}</td><td class=num>${n(f.approved)}</td><td class=num>${n(f.picks)}</td><td class=num>${n(f.posted)}</td><td class=num>${n(f.waiting_for_you)}</td><td class="wrap">${f.top_reasons.map(([r,c])=>esc(r)+" "+c).join(" · ")}</td></tr>`).join("");
 document.getElementById("top").innerHTML="<tr><th>Clip</th><th>Streamer</th><th>Platform</th><th class=num>Views</th><th class=num>+24 h</th><th>Chosen by</th><th>Posted</th><th>Title</th></tr>"+
  s.top_posts.map(p=>`<tr><td><a href="${esc(p.url)}" target="_blank" rel="noopener">#${p.clip_id}</a></td><td>${esc(p.streamer)}</td><td>${plat[p.platform]||p.platform}</td><td class=num>${n(p.views)}</td><td class=num>+${n(p.gained_24h)}</td><td>${esc(p.source)}</td><td>${ago(p.posted_at)} ago</td><td class="wrap">${esc(p.title)}</td></tr>`).join("");
 const h=s.health,sb=h.submissions;
 document.getElementById("health").innerHTML=`<table>
  <tr><td>Render queue</td><td class=num>${n(h.queued)} waiting${h.queued?` · oldest ${h.oldest_queued_min} min`:""}</td></tr>
  ${Object.entries(h.last_clip_min).map(([k,m])=>`<tr><td>Last clip from ${esc(k)}</td><td class="num ${m>180?"neg":""}">${m<60?m+" min":Math.round(m/60)+" h"} ago</td></tr>`).join("")}
  <tr><td>Submissions</td><td class=num>${n(sb.on_time)} on time · ${n(sb.late)} late · <span class="${sb.missed?"neg":""}">${n(sb.missed)} missed</span> · ${n(sb.open)} open</td></tr></table>`;
 document.getElementById("takedowns").innerHTML=(h.paused.length?`<p class="neg">Paused: ${h.paused.map(p=>esc(p.channel+" "+p.platform)).join(", ")}</p>`:`<p class="pos">No account paused.</p>`)+
  (h.takedowns.length?`<table>${h.takedowns.map(t=>`<tr><td>${esc(t.channel)} ${plat[t.platform]||t.platform}</td><td>${esc(t.kind)}</td><td class="wrap">${esc(t.detail)}</td><td><a href="${esc(t.url)}" target="_blank" rel="noopener">post</a></td></tr>`).join("")}</table>`:`<p class="mute">No takedowns, blocks or age restrictions.</p>`);
}
load().catch(e=>document.getElementById("when").textContent="couldn't load: "+e);setInterval(()=>load().catch(()=>{}),60000);
</script></body></html>"""
