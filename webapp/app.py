#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 动植物检疫政策监测面板(Flask, 只读展示 + 一键生成政策日报)。

用法:
  python webapp/app.py                                   # 默认 0.0.0.0:8000
  DASHBOARD_HOST=127.0.0.1 DASHBOARD_PORT=8080 python webapp/app.py

页面三块:
  政策影响地图   按发布/涉及国家标注政策影响范围,按对华影响着色
  政策台账       按动作/领域/核验/影响/时间筛选,关键字搜索
  政策日报       生成并查看 Markdown / Word / Excel 政策监测报告

本页面只处理外国政府动植物检疫政策变化记录。
Agent 全流程(搜索→抽取→核验→研判)仍由 Hermes 执行,可用 RADAR_GENERATE_CMD 接管。

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
    impact_level = e.get("impact_level")
    level_code = {"高影响": "high", "中影响": "medium", "低影响": "low"}.get(impact_level)
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
        "event_id": e.get("event_id"),
        "record_type": e.get("record_type"),
        "title_cn": e.get("title_cn"), "title_en": e.get("title_en"),
        "action_type": e.get("action_type"), "policy_domain": e.get("policy_domain"),
        "policy_status": e.get("policy_status"), "issuer_cn": e.get("issuer_cn"),
        "issuer_en": e.get("issuer_en"), "target_countries": e.get("target_countries"),
        "products": e.get("products"), "effective_date": e.get("effective_date"),
        "effective_until": e.get("effective_until"), "legal_basis": e.get("legal_basis"),
        "scope": e.get("scope"),
        "disease_name_cn": e.get("disease_name_cn"),
        "disease_name_en": e.get("disease_name_en"), "category": e.get("category"),
        "country_cn": e.get("country_cn"), "country_en": e.get("country_en"),
        "region": e.get("region"), "event_date": e.get("event_date"),
        "first_seen": str(e.get("first_seen", ""))[:10],
        "published_date": e.get("published_date") or e.get("report_date") or (e.get("source") or {}).get("publish_date"),
        "quantity": e.get("quantity"), "host_species": e.get("host_species"),
        "verification_status": e.get("verification_status"),
        "summary_cn": e.get("summary_cn"),
        "impact_type": e.get("impact_type"), "impact_level": impact_level,
        "impact_score": e.get("impact_score"), "china_relevance": e.get("china_relevance"),
        "impact_focus": e.get("impact_focus"), "impact_rationale": e.get("impact_rationale"),
        "dimension_scores": e.get("dimension_scores") or {},
        "recommended_action": e.get("recommended_action"), "impact_code": level_code,
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
        return jsonify({"record_type": "policy", "total": 0, "today_new": 0,
                        "focus_now": 0, "high": 0, "unverified": 0,
                        "tighten": 0, "relax": 0, "countries": 0,
                        "constraints": 0, "opportunities": 0, "neutral": 0})
    today = datetime.date.today().isoformat()
    all_e = [e for e in events["all"] if e.get("verification_status") != "merged"]
    covered = [e for e in all_e if str(e.get("first_seen", ""))[:10] == today]
    countries = {e.get("country_cn") for e in all_e if e.get("country_cn")}
    levels = [report.policy_impact_level(e) for e in all_e]
    focus_now = sum(1 for e, level in zip(all_e, levels) if level == "高影响")
    high = sum(1 for level in levels if level == "高影响")
    constraints = sum(1 for e in all_e if e.get("impact_type") == "约束")
    opportunities = sum(1 for e in all_e if e.get("impact_type") == "机会")
    neutral = sum(1 for e in all_e if e.get("impact_type") == "中性")
    return jsonify({
        "record_type": "policy", "total": len(all_e), "today_new": len(covered),
        "focus_now": focus_now, "high": high,
        "unverified": sum(1 for e in all_e if e.get("verification_status") == "unverified"),
        "tighten": sum(1 for e in all_e if e.get("action_type") == "收紧"),
        "relax": sum(1 for e in all_e if e.get("action_type") in ("放松", "恢复")),
        "constraints": constraints, "opportunities": opportunities, "neutral": neutral,
        "countries": len(countries),
    })


@app.get("/api/events")
def api_events():
    from normalize import load_events, open_db
    con = open_db()
    all_events = load_events(con)
    con.close()
    out = [slim_event(e) for e in all_events
           if e.get("verification_status") != "merged"
           and e.get("record_type") == "policy"]
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
    # 政策监测是默认产品:旧 RADAR_GENERATE_CMD 不再覆盖政策命令,避免服务器旧配置导致返回码 1。
    # 如确需自定义政策生成流程,请显式设置 RADAR_POLICY_GENERATE_CMD。
    cmd = os.environ.get("RADAR_POLICY_GENERATE_CMD")
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
<title>疫见全球 · 动植物检疫政策监测</title>
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
  <div class="brand"><span class="dot"></span>疫见全球 · 全球动植物检疫政策监测
    <div class="sub">只跟踪外国政府动植物疫情管控政策变化</div></div>
  <div class="stats" id="stats"></div>
  <button class="primary" id="genBtn">⚙ 生成今日政策日报(Word / Excel / Markdown)</button>
</header>
<nav>
  <button class="tab active" data-tab="map">🗺 政策影响地图</button>
  <button class="tab" data-tab="list">📋 政策台账</button>
  <button class="tab" data-tab="reports">📄 政策日报</button>
</nav>
<main>
<section id="tab-map">
  <div style="position:relative">
    <div id="map"></div>
    <div class="legend">
      <span><i style="background:#ff4d4f"></i>高影响</span>
      <span><i style="background:#faad14"></i>中影响</span>
      <span><i style="background:#52c41a"></i>低影响</span>
      <span><i style="background:#8c8c8c"></i>未研判</span>
    </div>
  </div>
  <div id="genLog"></div>
</section>
<section id="tab-list" hidden>
  <div class="filters">
    <select id="fCat"><option value="">全部政策领域</option><option value="animal">动物卫生</option><option value="plant">植物保护</option><option value="trade">进出口贸易</option><option value="measures">口岸措施</option></select>
    <select id="fRisk"><option value="">全部影响</option><option value="high">高影响</option><option value="medium">中影响</option><option value="low">低影响</option><option value="none">未研判</option></select>
    <select id="fImpactType"><option value="">全部影响类型</option><option value="约束">约束</option><option value="机会">机会</option><option value="中性">中性</option></select>
    <select id="fAction"><option value="">全部动作</option><option value="收紧">收紧</option><option value="放松">放松</option><option value="调整">调整</option><option value="恢复">恢复</option></select>
    <select id="fStatus"><option value="">全部核验状态</option><option value="verified">verified</option><option value="single_source">single_source</option><option value="unverified">unverified</option><option value="false_positive">false_positive</option></select>
    <select id="fDays"><option value="0">全部时间</option><option value="7">近7天</option><option value="30">近30天</option><option value="90">近90天</option></select>
    <input type="text" id="fQ" placeholder="搜索政策/国家/机构/商品…">
    <span id="rowCount" style="color:var(--dim);font-size:12px"></span>
  </div>
  <div style="overflow:auto;max-height:calc(100vh - 230px);border:1px solid var(--line);border-radius:10px">
    <table><thead><tr>
      <th>政策标题</th><th>动作</th><th>领域</th><th>国家/地区</th><th>商品/病害</th>
      <th>生效日期</th><th>政策状态</th><th>核验</th><th>影响类型/等级</th><th>来源</th>
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
   '<span class="chip">政策记录<b>'+s.total+'</b></span>'+
   '<span class="chip">今日新增<b>'+s.today_new+'</b></span>'+
   '<span class="chip">收紧<b style="color:#ff7875">'+(s.tighten||0)+'</b></span>'+
   '<span class="chip">约束<b style="color:#ff7875">'+(s.constraints||0)+'</b></span>'+
   '<span class="chip">机会<b style="color:#52c41a">'+(s.opportunities||0)+'</b></span>'+
   '<span class="chip">涉及国家<b>'+ (s.countries||0)+'</b></span>'+
   '<span class="chip">待核实<b>'+s.unverified+'</b></span>';
}

function initMap(){
  if(typeof L==='undefined'){
    $('#map').innerHTML='<div style="padding:40px;color:#8493ab">Leaflet 未加载: 请确认服务器 webapp/static/ 存在且未被拦截(刷新或查看浏览器控制台)</div>';
    return;
  }
  MAP=L.map('map',{center:[28,45],zoom:2,worldCopyJump:true,minZoom:2,attributionControl:false});
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',{
    maxZoom:16,attribution:'Tiles © Esri — Esri, HERE, Garmin, USGS, NGA'}).addTo(MAP);
  MARKERS=L.layerGroup().addTo(MAP);
  MAP_READY=true;
}

function popupHtml(e){
  const title=e.title_cn||e.title_en||'（无标题）';
  const sub='🛃政策'+(e.action_type?' · '+e.action_type:'');
  return '<div style="min-width:260px">'+
   '<b style="font-size:14px">'+esc(title)+'</b> <span style="color:#8493ab">'+esc(sub)+'</span><br>'+
   esc(e.country_cn)+(e.region?(' · '+esc(e.region)):'')+' | '+esc(e.event_date)+
   ' <span style="color:#60a5fa">🛃政策</span><br>'+
   chip(e.impact_code||'none','影响 '+(e.impact_level||'未研判'))+' '+
   chip('none',e.verification_status)+'<br>'+
   '<span style="color:#b9c6dd">'+esc(e.summary_cn||'')+'</span>'+
   (e.impact_type?'<br><span style="color:#8493ab">影响类型: '+esc(e.impact_type)+(e.china_relevance?' · '+esc(e.china_relevance):'')+'</span>':'')+
   (e.impact_rationale?'<br><span style="color:#b9c6dd">依据: '+esc(e.impact_rationale)+'</span>':'')+
   (e.dimension_scores&&Object.keys(e.dimension_scores).length?'<br><span style="color:#8493ab">四维: '+esc(Object.entries(e.dimension_scores).map(([k,v])=>k+' '+v).join(' / '))+(e.impact_score!=null?' → '+esc(e.impact_score)+'分':'')+'</span>':'')+
   (e.recommended_action?'<br><span style="color:#8493ab">建议动作: '+esc(e.recommended_action)+'</span>':'')+
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
    const c=RISK_COLOR[e.impact_code===null||e.impact_code===undefined?'null':e.impact_code];
    L.circleMarker([e.lat,e.lon],{radius:7,color:c,weight:1.5,fillColor:c,fillOpacity:.55})
     .bindPopup(popupHtml(e)).addTo(MARKERS);
    pts.push([e.lat,e.lon]);
  });
  if(pts.length)MAP.fitBounds(pts,{padding:[40,40],maxZoom:6});
}

function renderTable(){
  const cat=$('#fCat').value,risk=$('#fRisk').value,st=$('#fStatus').value,
        impactType=$('#fImpactType').value, action=$('#fAction').value,
        days=+$('#fDays').value,q=$('#fQ').value.trim().toLowerCase();
  const limit=days?Date.now()-days*864e5:0;
  const rows=EVENTS.filter(e=>{
    if(cat&&e.policy_domain!==cat)return false;
    const lv=e.impact_code||'none';
    if(risk&&lv!==risk)return false;
    if(action&&e.action_type!==action)return false;
    if(impactType&&e.impact_type!==impactType)return false;
    if(st&&e.verification_status!==st)return false;
    if(limit&&new Date(e.event_date).getTime()<limit)return false;
    if(q&&!(JSON.stringify(e).toLowerCase().includes(q)))return false;
    return true;
  });
  const order={'立即关注':0,'持续观察':1,'常规记录':2};
  const act={'收紧':0,'调整':1,'放松':2,'恢复':3};
  const impactOrder={'高影响':0,'中影响':1,'低影响':2};
  rows.sort((a,b)=>((act[a.action_type]??9)-(act[b.action_type]??9))||
                   ((impactOrder[a.impact_level]??3)-(impactOrder[b.impact_level]??3)));
  $('#rowCount').textContent=rows.length+' 条';
  $('#tbody').innerHTML=rows.map(e=>{
    const name=e.title_cn||e.title_en||'（无标题）';
    const products=(e.products||[]).join('、')||e.disease_name_cn||'-';
    const effective=e.effective_date||e.event_date||'-';
    return '<tr>'+
    '<td><b>'+esc(name)+'</b></td>'+
    '<td>'+esc(e.action_type||'-')+'</td>'+
    '<td>'+esc(e.policy_domain||'-')+'</td>'+
    '<td>'+esc(e.country_cn)+(e.region?' · '+esc(e.region):'')+'</td>'+
    '<td>'+esc(products)+'</td>'+
    '<td>'+esc(effective)+'</td>'+
    '<td>'+esc(e.policy_status||'已生效')+'</td>'+
    '<td>'+chip('none',e.verification_status)+'</td>'+
    '<td>'+chip(e.impact_code||'none',(e.impact_type||'未研判')+'·'+(e.impact_level||'未研判'))+'</td>'+
    '<td>'+(e.source_url?'<a href="'+esc(e.source_url)+'" target="_blank">'+esc(e.source_name||'链接')+'</a>':'-')+
    (e.cross_count?' <span style="color:#8493ab">+'+e.cross_count+'</span>':'')+'</td>';
  }).join('');
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
    log.innerHTML='<b>正在生成今日政策日报(Markdown / Excel / Word)…</b>';
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
  b.disabled=false;b.textContent='⚙ 生成今日政策日报(Word / Excel / Markdown)';
};

document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  ['map','list','reports'].forEach(id=>$('#tab-'+id).hidden=(t.dataset.tab!==id));
  if(t.dataset.tab==='map'&&MAP_READY)MAP.invalidateSize();
  if(t.dataset.tab==='reports')loadReports();
});
['fCat','fRisk','fImpactType','fAction','fStatus','fDays'].forEach(id=>$('#'+id).onchange=renderTable);
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
