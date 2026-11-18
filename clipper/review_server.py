"""Local review dashboard (http://127.0.0.1:8765). Nothing posts without a
click on Approve here. Binds to localhost only."""
from __future__ import annotations

import json
import logging
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import scheduler
from .db import DB

log = logging.getLogger(__name__)

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clip Review</title>
<style>
:root{--bg:#0e0f12;--panel:#17191e;--line:#2a2d35;--text:#e8e9ec;--mute:#9aa0ab;--accent:#ffd23f;--ok:#3ecf8e;--bad:#ff5d5d;--warn:#ffa94d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:2;background:rgba(14,15,18,.94);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}
h1{font-size:16px;margin:0 12px 0 0;letter-spacing:.02em}
.stat{color:var(--mute)}.stat b{color:var(--text)}
nav{display:flex;gap:6px;flex-wrap:wrap}nav button{background:var(--panel);color:var(--mute);border:1px solid var(--line);border-radius:999px;padding:5px 12px;cursor:pointer}
nav button.on{color:#111;background:var(--accent);border-color:var(--accent)}
#watch{width:100%;color:var(--mute);font-size:12px}
main{padding:16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
video{width:100%;aspect-ratio:9/16;max-height:560px;background:#000;display:block}
.body{padding:12px;display:flex;flex-direction:column;gap:8px}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.pill{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--mute)}
.pill.high{color:var(--bad);border-color:var(--bad)}.pill.low{color:var(--warn);border-color:var(--warn)}.pill.none{color:var(--ok);border-color:var(--ok)}
label{font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.06em}
input,textarea{width:100%;background:#0f1115;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:7px 9px;font:inherit}
textarea{min-height:70px;resize:vertical}
.note{color:var(--mute);font-size:12px}
.actions{display:flex;gap:8px;margin-top:4px}
.actions button{flex:1;border:0;border-radius:8px;padding:9px;font-weight:600;cursor:pointer}
.approve{background:var(--ok);color:#06210f}.reject{background:#2a2d35;color:var(--text)}.rerender{background:#2a2d35;color:var(--accent)}
.empty{color:var(--mute);padding:40px;text-align:center;grid-column:1/-1}
a{color:var(--accent)}
@media (max-width:420px){main{padding:10px;grid-template-columns:1fr}}
</style></head><body>
<header>
  <h1>GTA 6 Clip Review</h1>
  <span class="stat" id="stats"></span>
  <nav id="tabs"></nav>
  <div id="watch"></div>
</header>
<main id="grid"></main>
<script>
const TABS=["ready","approved","posted","queued","rendering","rejected","failed"];
let tab="ready";
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function tabs(counts){document.getElementById("tabs").innerHTML=TABS.map(t=>`<button class="${t===tab?"on":""}" onclick="setTab('${t}')">${t} ${counts[t]||0}</button>`).join("")}
function setTab(t){tab=t;load()}
async function load(){
  const [clips,sum]=await Promise.all([fetch(`/api/clips?status=${tab}`).then(r=>r.json()),fetch("/api/summary").then(r=>r.json())]);
  document.getElementById("stats").innerHTML=`<b>${sum.posts}</b> posts · <b>${sum.views.toLocaleString()}</b> views · est <b>$${sum.est_usd.toFixed(2)}</b>`;
  document.getElementById("watch").textContent=(sum.watchers||[]).join("  |  ");
  tabs(sum.clips||{});
  const g=document.getElementById("grid");
  if(!clips.length){g.innerHTML=`<div class="empty">No ${tab} clips.</div>`;return}
  g.innerHTML=clips.map(c=>{
    const n=c.notes||{};const posts=(c.posts||[]).map(p=>p.url?`<a href="${esc(p.url)}" target="_blank">${esc(p.platform)}</a> ${p.views||0} views`:`${esc(p.platform)}: ${esc(p.status)} ${esc(p.error)}`).join(" · ");
    return `<div class="card" id="c${c.id}">
      ${c.final_path?`<video src="/media/${c.id}.mp4" controls preload="none" poster="/thumb/${c.id}.jpg"></video>`:""}
      <div class="body">
        <div class="row"><b>#${c.id}</b><span class="pill">${esc(c.channel)}</span>
          <span class="pill">${esc(c.campaign_id||"no campaign")}</span>
          <span class="pill ${esc(c.spoiler_risk)}">spoiler: ${esc(c.spoiler_risk)}</span>
          <span class="pill">quality ${c.quality}/10</span><span class="pill">score ${Number(c.score).toFixed(1)}</span>
          ${n.unsafe?'<span class="pill high">UNSAFE</span>':""}</div>
        ${n.why?`<div class="note">${esc(n.why)}</div>`:""}
        ${c.error?`<div class="note" style="color:var(--bad)">${esc(c.error)}</div>`:""}
        <label>Hook (burned into video)</label><input id="h${c.id}" value="${esc(c.hook)}">
        <label>Title</label><input id="t${c.id}" value="${esc(c.title)}">
        <label>Caption</label><textarea id="p${c.id}">${esc(c.caption)}</textarea>
        <label>Hashtags</label><input id="g${c.id}" value="${esc(c.hashtags)}">
        ${n.take?`<div class="note"><b>Your take:</b> ${esc(n.take)}</div>`:""}
        <details><summary class="note">Transcript</summary><div class="note">${esc(c.transcript)||"(none)"}</div></details>
        ${posts?`<div class="note">${posts}</div>`:""}
        <div class="actions">
          ${["ready","rejected","failed"].includes(c.status)?`<button class="approve" onclick="act(${c.id},'approve')">Approve</button>`:""}
          ${c.status!=="posted"&&c.status!=="rejected"?`<button class="reject" onclick="act(${c.id},'reject')">Reject</button>`:""}
          <button class="rerender" onclick="act(${c.id},'rerender')">Save + re-render</button>
        </div>
      </div></div>`}).join("");
}
async function act(id,action){
  const body={action,hook:document.getElementById("h"+id).value,title:document.getElementById("t"+id).value,
    caption:document.getElementById("p"+id).value,hashtags:document.getElementById("g"+id).value};
  const r=await fetch(`/api/clips/${id}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(!r.ok){alert(await r.text());return}
  load();
}
load();setInterval(()=>{if(!document.querySelector("video:not([paused])")&&document.activeElement.tagName==="BODY")load()},15000);
</script></body></html>
"""


def make_handler(db: DB, cfg: dict, watchers: list):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            log.debug(fmt, *args)

        def _json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, ctype: str):
            if not path.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            size = path.stat().st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remaining -= len(chunk)

        def _clip(self, clip_id: int):
            return db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                body = PAGE.encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/clips":
                status = parse_qs(url.query).get("status", ["ready"])[0]
                rows = db.query(
                    "SELECT * FROM clips WHERE status = ? ORDER BY quality DESC, score DESC, id DESC LIMIT 200",
                    (status,),
                )
                out = []
                for r in rows:
                    d = dict(r)
                    d.pop("words_json", None)
                    try:
                        d["notes"] = json.loads(d.pop("ai_notes") or "{}")
                    except json.JSONDecodeError:
                        d["notes"] = {}
                    d["posts"] = [dict(p) for p in db.query(
                        "SELECT platform, url, status, error, views FROM posts WHERE clip_id = ?", (r["id"],))]
                    out.append(d)
                self._json(out)
            elif url.path == "/api/summary":
                s = scheduler.earnings_summary(db, cfg)
                s["watchers"] = [f"{w.login}: {w.status}" for w in watchers]
                self._json(s)
            elif m := re.fullmatch(r"/media/(\d+)\.mp4", url.path):
                row = self._clip(int(m.group(1)))
                if not row or not row["final_path"]:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._file(Path(row["final_path"]), "video/mp4")
            elif m := re.fullmatch(r"/thumb/(\d+)\.jpg", url.path):
                row = self._clip(int(m.group(1)))
                if not row or not row["final_path"]:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._file(Path(row["final_path"]).with_name("thumb.jpg"), "image/jpeg")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self):
            m = re.fullmatch(r"/api/clips/(\d+)", urlparse(self.path).path)
            if not m:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            # same-origin only: the page is served from here, reject cross-site posts
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and urlparse(origin).netloc != host:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            clip_id = int(m.group(1))
            row = self._clip(clip_id)
            if not row:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            try:
                body = json.loads(self.rfile.read(min(length, 1_000_000)) or b"{}")
            except json.JSONDecodeError:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            fields = {k: str(body[k])[:3000] for k in ("hook", "title", "caption", "hashtags") if k in body}
            action = body.get("action")
            if action == "approve":
                if row["status"] not in ("ready", "rejected", "failed") or not row["final_path"]:
                    self._json({"error": f"can't approve a {row['status']} clip"}, HTTPStatus.CONFLICT)
                    return
                if fields.get("hook", row["hook"]) != row["hook"]:
                    self._json({"error": "hook changed: use Save + re-render first"}, HTTPStatus.CONFLICT)
                    return
                db.update("clips", clip_id, status="approved", **fields)
            elif action == "reject":
                db.update("clips", clip_id, status="rejected", **fields)
            elif action == "rerender":
                if row["status"] in ("rendering", "posted"):
                    self._json({"error": f"clip is {row['status']}"}, HTTPStatus.CONFLICT)
                    return
                if not fields.get("hook", row["hook"]):
                    fields["hook"] = " "  # keep empty hook, skip regeneration
                db.update("clips", clip_id, status="queued", error="", **fields)
            else:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True})

    return Handler


def serve(db: DB, cfg: dict, watchers: list, stop: threading.Event) -> ThreadingHTTPServer:
    host, port = cfg["review"]["host"], int(cfg["review"]["port"])
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("review UI bound to %s: anyone on that network can approve posts", host)
    httpd = ThreadingHTTPServer((host, port), make_handler(db, cfg, watchers))
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="review")
    t.start()

    def _shutdown():
        stop.wait()
        httpd.shutdown()

    threading.Thread(target=_shutdown, daemon=True).start()
    log.info("review UI: http://%s:%s", host, port)
    return httpd
