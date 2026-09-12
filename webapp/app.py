#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 疫情雷达面板(Flask, 只读展示 + 一键重渲染日报)。

用法:
  python webapp/app.py                                   # 默认 0.0.0.0:8000
  DASHBOARD_HOST=127.0.0.1 DASHBOARD_PORT=8080 python webapp/app.py

页面三块:
  疫情地图   事件按国家/坐标打点, 风险着色(红黄绿灰), 点气泡看详情与来源
  事件库     按类别/风险/核验/时间筛选, 关键字搜索
  日报       渲染当日 Markdown 日报, 下载 Word/Excel

"生成今日日报"按钮: 重跑 scripts/report.py(--excel --docx) 重新渲染产物;
Agent 全流程(搜索→抽取→核验→研判)仍由 Hermes 执行, 可用 RADAR_GENERATE_CMD 接管。

安全: 面板只读; 生产环境建议经 Tailscale 或反向代理访问, 不要裸暴露公网。
事件坐标: 优先用事件自带经纬度, 缺失时按国名查询 OpenStreetMap Nominatim 并缓存
(RADAR_GEO=0 关闭; 同国多点做确定性偏移避免重叠)。
"""
import datetime
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from flask import Flask, Response, jsonify, request, send_file

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import report  # noqa: E402
from normalize import DATA_DIR, REPO  # noqa: E402

REPORTS_DIR = os.path.join(DATA_DIR, "reports")
GEO_CACHE = os.path.join(DATA_DIR, "geocache.json")
GEO_ENABLED = os.environ.get("RADAR_GEO", "1") != "0"
RISK_COLOR = {"high": "#ff4d4f", "medium": "#faad14", "low": "#52c41a", None: "#8c8c8c"}

_geo_lock = threading.Lock()
_geo_cache = None
_geo_last_call = 0.0


def _load_geo_cache():
    global _geo_cache
    if _geo_cache is None:
        try:
            with open(GEO_CACHE, encoding="utf-8") as f:
                _geo_cache = json.load(f)
        except (OSError, ValueError):
            _geo_cache = {}
    return _geo_cache


def _save_geo_cache():
    os.makedirs(os.path.dirname(GEO_CACHE), exist_ok=True)
    with open(GEO_CACHE, "w", encoding="utf-8") as f:
        json.dump(_geo_cache, f, ensure_ascii=False)


def country_centroid(country_en):
    """国名 -> (lat, lon); Nominatim 限速 1 req/s, 结果缓存。"""
    global _geo_last_call
    if not GEO_ENABLED or not country_en:
        return None
    with _geo_lock:
        cache = _load_geo_cache()
        if country_en in cache:
            return cache[country_en]
        try:
            if time.time() - _geo_last_call < 1.1:
                time.sleep(1.1 - (time.time() - _geo_last_call))
            _geo_last_call = time.time()
            qs = urllib.parse.urlencode({"q": country_en, "format": "jsonv2", "limit": 1})
            req = urllib.request.Request(
                "https://nominatim.openstreetmap.org/search?%s" % qs,
                headers={"User-Agent": "GlobalEpidemicRadar/0.1 (country centroid lookup)"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8"))
            pos = (float(data[0]["lat"]), float(data[0]["lon"])) if data else None
        except Exception:
            pos = None
        cache[country_en] = pos
        _save_geo_cache()
        return pos


def _jitter(event_id):
    """同国多点确定性偏移 ±0.8°, 避免打点重叠。"""
    h = int(event_id, 16)
    return ((h % 100) / 50.0 - 1.0) * 0.8, (((h >> 7) % 100) / 50.0 - 1.0) * 0.8


def slim_event(e):
    risk = e.get("china_risk") or {}
    lat, lon, coord_src = None, None, None
    if e.get("latitude") is not None and e.get("longitude") is not None:
        lat, lon, coord_src = float(e["latitude"]), float(e["longitude"]), "point"
    elif e.get("country_en"):
        pos = country_centroid(e["country_en"])
        if pos:
            dlat, dlon = _jitter(e["event_id"])
            lat, lon, coord_src = pos[0] + dlat, pos[1] + dlon, "country"
    src = e.get("source") or {}
    return {
        "event_id": e.get("event_id"), "disease_name_cn": e.get("disease_name_cn"),
        "disease_name_en": e.get("disease_name_en"), "category": e.get("category"),
        "country_cn": e.get("country_cn"), "country_en": e.get("country_en"),
        "region": e.get("region"), "event_date": e.get("event_date"),
        "first_seen": str(e.get("first_seen", ""))[:10],
        "quantity": e.get("quantity"), "host_species": e.get("host_species"),
        "verification_status": e.get("verification_status"),
        "summary_cn": e.get("summary_cn"),
        "risk_level": risk.get("level"), "risk_score": risk.get("score"),
        "focus": risk.get("focus"), "rationale": risk.get("rationale"),
        "trade_relevance": risk.get("trade_relevance"),
        "gacc_measures": risk.get("existing_gacc_measures"),
        "source_name": src.get("name"), "source_url": src.get("url"),
        "cross_count": len(e.get("cross_sources") or []),
        "lat": lat, "lon": lon, "coord_src": coord_src,
    }


app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")


@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.get("/api/summary")
def api_summary():
    events = report.collect(datetime.date.today().isoformat(), 3650)
    if events is None:
        return jsonify({"total": 0})
    today = datetime.date.today().isoformat()
    all_e = [e for e in events["all"] if e.get("verification_status") != "merged"]
    covered = [e for e in all_e if str(e.get("first_seen", ""))[:10] == today]
    return jsonify({
        "total": len(all_e),
        "today_new": len(covered),
        "focus_now": sum(1 for e in all_e
                         if (e.get("china_risk") or {}).get("focus") == "立即关注"),
        "high": sum(1 for e in all_e
                    if (e.get("china_risk") or {}).get("level") == "high"),
        "unverified": sum(1 for e in all_e
                          if e.get("verification_status") == "unverified"),
    })


@app.get("/api/events")
def api_events():
    from normalize import load_events, open_db
    con = open_db()
    all_events = load_events(con)
    con.close()
    out = [slim_event(e) for e in all_events if e.get("verification_status") != "merged"]
    return jsonify(out)


@app.get("/api/reports")
def api_reports():
    if not os.path.isdir(REPORTS_DIR):
        return jsonify([])
    items = []
    for name in os.listdir(REPORTS_DIR):
        path = os.path.join(REPORTS_DIR, name)
        if os.path.isfile(path) and not name.startswith("."):
            items.append({"name": name, "size": os.path.getsize(path),
                          "mtime": int(os.path.getmtime(path))})
    items.sort(key=lambda x: -x["mtime"])
    return jsonify(items)


@app.get("/api/report/<name>")
def api_report(name):
    name = os.path.basename(name)
    if not name.endswith(".md"):
        return jsonify({"error": "only .md"}), 400
    path = os.path.join(REPORTS_DIR, name)
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    with open(path, encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/plain; charset=utf-8")


@app.get("/api/report-file/<name>")
def api_report_file(name):
    name = os.path.basename(name)
    if not name.endswith((".docx", ".xlsx", ".csv")):
        return jsonify({"error": "not downloadable"}), 400
    path = os.path.join(REPORTS_DIR, name)
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    return send_file(path, as_attachment=True)


@app.post("/api/generate")
def api_generate():
    date = datetime.date.today().isoformat()
    cmd = os.environ.get("RADAR_GENERATE_CMD")
    try:
        if cmd:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=600, cwd=REPO)
        else:
            p = subprocess.run(
                [sys.executable, os.path.join(REPO, "scripts", "report.py"),
                 "--date", date, "--excel", "--docx"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=600, cwd=REPO)
        log = (p.stdout or "") + ("\n" + p.stderr if p.stderr and p.stderr.strip() else "")
        return jsonify({"ok": p.returncode == 0, "returncode": p.returncode,
                        "log": log[-4000:]})
    except Exception as exc:
        return jsonify({"ok": False, "log": str(exc)}), 500


INDEX_HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>疫见全球 · 疫情雷达</title>
<link rel="stylesheet" href="/static/leaflet.css">
<script src="/static/leaflet.js"></script>
<script src="/static/marked.min.js"></script>
<style>
:root{--bg:#0b1220;--panel:#121a2b;--line:#1f2a44;--txt:#dbe4f3;--dim:#8493ab;
--accent:#3b82f6;--high:#ff4d4f;--med:#faad14;--low:#52c41a;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel)}
.brand{font-size:17px;font-weight:700}
.brand .sub{font-size:12px;color:var(--dim);font-weight:400}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--high);margin-right:8px;box-shadow:0 0 0 0 rgba(255,77,79,.7);animation:pulse 2s infinite}
@keyframes pulse{70%{box-shadow:0 0 0 10px rgba(255,77,79,0)}100%{box-shadow:0 0 0 0 rgba(255,77,79,0)}}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-left:auto}
.chip{background:#0e1730;border:1px solid var(--line);border-radius:8px;padding:4px 12px;font-size:12px}
.chip b{font-size:15px;margin-left:4px}
button{cursor:pointer;border:1px solid var(--line);background:#0e1730;color:var(--txt);border-radius:8px;padding:7px 14px;font-size:13px}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button:disabled{opacity:.5;cursor:wait}
nav{display:flex;gap:8px;padding:10px 20px;border-bottom:1px solid var(--line)}
nav button.active{border-color:var(--accent);color:var(--accent);font-weight:600}
main{padding:14px 20px}
section[hidden]{display:none}
#map{height:calc(100vh - 190px);border-radius:10px;border:1px solid var(--line)}
.legend{position:absolute;bottom:26px;left:34px;z-index:500;background:rgba(10,16,30,.85);border:1px solid var(--line);border-radius:8px;padding:8px 12px;font-size:12px;display:flex;gap:14px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}
.filters{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;align-items:center}
select,input[type=text]{background:#0e1730;border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:6px 10px;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--dim);font-weight:500;position:sticky;top:0;background:var(--panel)}
tr:hover td{background:#0e1730}
.tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;border:1px solid}
.tag.high{color:var(--high);border-color:var(--high)} .tag.medium{color:var(--med);border-color:var(--med)}
.tag.low{color:var(--low);border-color:var(--low)} .tag.none{color:var(--dim);border-color:var(--dim)}
.tag.focus{color:#ff7875;border-color:#ff7875} .tag.watch{color:var(--med);border-color:var(--med)}
a{color:#7ab3ff;text-decoration:none} a:hover{text-decoration:underline}
#reports{display:grid;grid-template-columns:280px 1fr;gap:14px}
#reportList{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px;max-height:calc(100vh - 190px);overflow:auto}
#reportList div{padding:8px 10px;border-radius:8px;cursor:pointer;font-size:13px;display:flex;justify-content:space-between;gap:8px}
#reportList div:hover,#reportList div.on{background:#0e1730;color:var(--accent)}
#reportList .dl{font-size:11px;color:var(--dim)}
#mdView{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:24px 30px;overflow:auto;max-height:calc(100vh - 190px)}
#mdView h1{font-size:20px;margin:8px 0} #mdView h2{font-size:16px;margin:14px 0 6px;color:#9cc3ff}
#mdView table{border:1px solid var(--line)} #mdView td,#mdView th{border:1px solid var(--line)}
#mdView blockquote{border-left:3px solid var(--line);padding-left:10px;color:var(--dim)}
#genLog{margin-top:8px;background:#0a0f1c;border:1px solid var(--line);border-radius:8px;padding:10px;font:12px/1.5 Consolas,monospace;color:#9fb4d8;white-space:pre-wrap;display:none;max-height:200px;overflow:auto}
.toast{position:fixed;right:20px;bottom:20px;background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:8px;padding:10px 16px;display:none;z-index:999}
</style>
</head>
<body>
<header>
  <div class="brand"><span class="dot"></span>疫见全球 · 全球动植物疫情智能情报雷达
    <div class="sub">让全球疫情信息，从"新闻"变成"风险情报"</div></div>
  <div class="stats" id="stats"></div>
  <button class="primary" id="genBtn">⚙ 生成今日日报</button>
</header>
<nav>
  <button class="tab active" data-tab="map">🗺 疫情地图</button>
  <button class="tab" data-tab="list">📋 事件库</button>
  <button class="tab" data-tab="reports">📄 日报</button>
</nav>
<main>
<section id="tab-map">
  <div style="position:relative">
    <div id="map"></div>
    <div class="legend">
      <span><i style="background:#ff4d4f"></i>高风险</span>
      <span><i style="background:#faad14"></i>中风险</span>
      <span><i style="background:#52c41a"></i>低风险</span>
      <span><i style="background:#8c8c8c"></i>未研判</span>
    </div>
  </div>
  <div id="genLog"></div>
</section>
<section id="tab-list" hidden>
  <div class="filters">
    <select id="fCat"><option value="">全部类别</option><option value="animal">动物</option><option value="plant">植物</option></select>
    <select id="fRisk"><option value="">全部风险</option><option value="high">高风险</option><option value="medium">中风险</option><option value="low">低风险</option><option value="none">未研判</option></select>
    <select id="fStatus"><option value="">全部核验状态</option><option value="verified">verified</option><option value="single_source">single_source</option><option value="unverified">unverified</option><option value="false_positive">false_positive</option></select>
    <select id="fDays"><option value="0">全部时间</option><option value="7">近7天</option><option value="30">近30天</option><option value="90">近90天</option></select>
    <input type="text" id="fQ" placeholder="搜索 病害/国家/关键词…">
    <span id="rowCount" style="color:var(--dim);font-size:12px"></span>
  </div>
  <div style="overflow:auto;max-height:calc(100vh - 230px);border:1px solid var(--line);border-radius:10px">
    <table><thead><tr>
      <th>病害</th><th>类别</th><th>国家/地区</th><th>发生日期</th><th>数量</th>
      <th>核验</th><th>对华风险</th><th>关注</th><th>来源</th><th>入库</th>
    </tr></thead><tbody id="tbody"></tbody></table>
  </div>
</section>
<section id="tab-reports" hidden>
  <div id="reports">
    <div id="reportList"></div>
    <div id="mdView">选择左侧日报查看…</div>
  </div>
</section>
</main>
<div class="toast" id="toast"></div>
<script>
const RISK_COLOR={high:'#ff4d4f',medium:'#faad14',low:'#52c41a',null:'#8c8c8c'};
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const chip=(cls,text)=>'<span class="tag '+cls+'">'+esc(text)+'</span>';
let EVENTS=[],MAP=null,MARKERS=null,MAP_READY=false;

async function jget(u){const r=await fetch(u);return r.json();}

function renderStats(s){
  $('#stats').innerHTML=
   '<span class="chip">事件库<b>'+s.total+'</b></span>'+
   '<span class="chip">今日新增<b>'+s.today_new+'</b></span>'+
   '<span class="chip">立即关注<b style="color:#ff7875">'+s.focus_now+'</b></span>'+
   '<span class="chip">高风险<b style="color:#ff4d4f">'+s.high+'</b></span>'+
   '<span class="chip">待核实<b>'+s.unverified+'</b></span>';
}

function initMap(){
  if(typeof L==='undefined'){
    $('#map').innerHTML='<div style="padding:40px;color:#8493ab">Leaflet 未加载: 请确认服务器 webapp/static/ 存在且未被拦截(刷新或查看浏览器控制台)</div>';
    return;
  }
  MAP=L.map('map',{center:[28,45],zoom:2,worldCopyJump:true,minZoom:2});
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',{
    maxZoom:16,attribution:'Tiles © Esri — Esri, HERE, Garmin, USGS, NGA'}).addTo(MAP);
  MARKERS=L.layerGroup().addTo(MAP);
  MAP_READY=true;
}

function popupHtml(e){
  const rc=RISK_COLOR[e.risk_level===null||e.risk_level===undefined?'null':e.risk_level];
  return '<div style="min-width:260px">'+
   '<b style="font-size:14px">'+esc(e.disease_name_cn)+'</b> <span style="color:#8493ab">'+esc(e.disease_name_en)+'</span><br>'+
   esc(e.country_cn)+(e.region?(' · '+esc(e.region)):'')+' | '+esc(e.event_date)+
   ' <span style="color:'+(e.category==='animal'?'#f59e0b':'#10b981')+'">'+(e.category==='animal'?'🐾动物':'🌱植物')+'</span><br>'+
   chip(e.risk_level||'none','风险 '+(e.risk_level||'未研判'))+' '+
   (e.focus?chip(e.focus==='立即关注'?'focus':'watch',e.focus):'')+' '+
   chip('none',e.verification_status)+'<br>'+
   '<span style="color:#b9c6dd">'+esc(e.summary_cn||'')+'</span>'+
   (e.rationale?'<br><span style="color:#8493ab">研判: '+esc(e.rationale)+'</span>':'')+
   (e.source_url?'<br>来源: <a href="'+esc(e.source_url)+'" target="_blank">'+esc(e.source_name||'链接')+'</a>':'')+
   '</div>';
}

function renderMap(evts){
  if(!MAP_READY)initMap();
  MAP.invalidateSize();
  MARKERS.clearLayers();
  const pts=[];
  evts.forEach(e=>{
    if(e.lat==null)return;
    const c=RISK_COLOR[e.risk_level===null||e.risk_level===undefined?'null':e.risk_level];
    L.circleMarker([e.lat,e.lon],{radius:7,color:c,weight:1.5,fillColor:c,fillOpacity:.55})
     .bindPopup(popupHtml(e)).addTo(MARKERS);
    pts.push([e.lat,e.lon]);
  });
  if(pts.length)MAP.fitBounds(pts,{padding:[40,40],maxZoom:6});
}

function renderTable(){
  const cat=$('#fCat').value,risk=$('#fRisk').value,st=$('#fStatus').value,
        days=+$('#fDays').value,q=$('#fQ').value.trim().toLowerCase();
  const limit=days?Date.now()-days*864e5:0;
  const rows=EVENTS.filter(e=>{
    if(cat&&e.category!==cat)return false;
    const lv=e.risk_level||'none';
    if(risk&&lv!==risk)return false;
    if(st&&e.verification_status!==st)return false;
    if(limit&&new Date(e.event_date).getTime()<limit)return false;
    if(q&&!(JSON.stringify(e).toLowerCase().includes(q)))return false;
    return true;
  });
  const order={'立即关注':0,'持续观察':1,'常规记录':2};
  rows.sort((a,b)=>(order[a.focus]??3)-(order[b.focus]??3)||((b.risk_score||0)-(a.risk_score||0)));
  $('#rowCount').textContent=rows.length+' 条';
  $('#tbody').innerHTML=rows.map(e=>'<tr>'+
    '<td><b>'+esc(e.disease_name_cn)+'</b> <span style="color:#8493ab">'+esc(e.disease_name_en)+'</span></td>'+
    '<td>'+(e.category==='animal'?'🐾':'🌱')+'</td>'+
    '<td>'+esc(e.country_cn)+(e.region?' · '+esc(e.region):'')+'</td>'+
    '<td>'+esc(e.event_date)+'</td>'+
    '<td style="max-width:180px;overflow:hidden;text-overflow:ellipsis">'+esc(JSON.stringify(e.quantity||{}))+'</td>'+
    '<td>'+chip('none',e.verification_status)+'</td>'+
    '<td>'+chip(e.risk_level||'none',(e.risk_score!=null?e.risk_score+' ':'')+(e.risk_level||'未研判'))+'</td>'+
    '<td>'+(e.focus?chip(e.focus==='立即关注'?'focus':'watch',e.focus):'-')+'</td>'+
    '<td>'+(e.source_url?'<a href="'+esc(e.source_url)+'" target="_blank">'+esc(e.source_name||'链接')+'</a>':'-')+
    (e.cross_count?' <span style="color:#8493ab">+'+e.cross_count+'</span>':'')+'</td>'+
    '<td style="color:#8493ab">'+esc(e.first_seen)+'</td></tr>').join('');
}

async function loadReports(){
  const items=await jget('/api/reports');
  const mds=items.filter(i=>i.name.endsWith('.md'));
  $('#reportList').innerHTML=mds.map(i=>{
    const base=i.name.replace('.md','');
    const dl=items.filter(x=>x.name.startsWith(base)&&!x.name.endsWith('.md'))
      .map(x=>'<a class="dl" href="/api/report-file/'+esc(x.name)+'">'+esc(x.name.split('.').pop())+'</a>').join(' ');
    return '<div data-name="'+esc(i.name)+'"><span>'+esc(i.name.replace('-daily-report.md',''))+'</span><span>'+dl+'</span></div>';
  }).join('')||'<div>暂无日报</div>';
  $('#reportList').querySelectorAll('div[data-name]').forEach(d=>d.onclick=()=>{
    $('#reportList').querySelectorAll('div').forEach(x=>x.classList.remove('on'));
    d.classList.add('on');
    fetch('/api/report/'+d.dataset.name).then(r=>r.text()).then(t=>{
      $('#mdView').innerHTML=window.marked?marked.parse(t):'<pre style="white-space:pre-wrap">'+esc(t)+'</pre>';
    });
  });
  if(mds.length)$('#reportList').querySelector('div[data-name]').click();
}

function toast(msg,ok){
  const t=$('#toast');t.textContent=msg;t.style.display='block';
  t.style.borderLeftColor=ok?'#52c41a':'#ff4d4f';
  setTimeout(()=>t.style.display='none',3500);
}

async function refresh(){
  const [s,ev]=await Promise.all([jget('/api/summary'),jget('/api/events')]);
  renderStats(s);EVENTS=ev;renderTable();
  try{renderMap(evtsFiltered());}catch(err){console.warn('map error',err);}
}
function evtsFiltered(){return EVENTS;}

$('#genBtn').onclick=async()=>{
  const b=$('#genBtn');b.disabled=true;b.textContent='⏳ 生成中…';
  const log=$('#genLog');log.style.display='block';
  log.innerHTML='<b>正在重渲染今日日报(Markdown / Excel / Word)…</b>';
  try{
    const r=await fetch('/api/generate',{method:'POST'});
    const j=await r.json();
    if(j.ok){
      const m=(j.log.match(/本期: ([^\n]+)/)||[])[1]||'';
      log.innerHTML='<b style="color:#52c41a">✅ 日报已更新</b>'+(m?' — '+esc(m):'')+
        '<details style="margin-top:6px"><summary style="cursor:pointer;color:#8493ab;font-size:12px">运行日志</summary>'+
        '<pre style="white-space:pre-wrap;margin:6px 0 0;font:11px/1.5 Consolas,monospace;color:#9fb4d8">'+esc(j.log||'')+'</pre></details>';
      toast('日报已更新',true);
      await refresh();
    }else{
      log.innerHTML='<b style="color:#ff4d4f">❌ 生成失败(返回码 '+esc(j.returncode)+')</b>'+
        '<details style="margin-top:6px" open><summary style="cursor:pointer;color:#8493ab;font-size:12px">运行日志</summary>'+
        '<pre style="white-space:pre-wrap;margin:6px 0 0;font:11px/1.5 Consolas,monospace;color:#9fb4d8">'+esc(j.log||'')+'</pre></details>';
      toast('生成失败, 展开"运行日志"查看原因',false);
    }
  }catch(e){
    log.innerHTML='<b style="color:#ff4d4f">❌ 请求失败: '+esc(String(e))+'</b>';
    toast('请求失败',false);
  }
  b.disabled=false;b.textContent='⚙ 生成今日日报';
};

document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  ['map','list','reports'].forEach(id=>$('#tab-'+id).hidden=(t.dataset.tab!==id));
  if(t.dataset.tab==='map'&&MAP_READY)MAP.invalidateSize();
  if(t.dataset.tab==='reports')loadReports();
});
['fCat','fRisk','fStatus','fDays'].forEach(id=>$('#'+id).onchange=renderTable);
$('#fQ').oninput=renderTable;

refresh();
setInterval(async()=>{const s=await jget('/api/summary');renderStats(s);},300000);
</script>
</body>
</html>"""


def main():
    import argparse
    ap = argparse.ArgumentParser(description="疫见全球 · 疫情雷达面板")
    ap.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8000")))
    args = ap.parse_args()
    print("疫见全球 · 疫情雷达: http://%s:%d  (数据: %s)" % (args.host, args.port, DATA_DIR))
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
