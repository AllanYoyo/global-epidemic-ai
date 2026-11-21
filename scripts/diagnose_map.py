#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板地图现场诊断(只读, 不改任何状态)。

背景(2026-11-21 生产实证): 台账/统计正常但地图零打点 —— 旧版地理编码把失败的
查询(None)永久写进 geocache.json, 中国侧网络够不着 Nominatim 时, 新国家的点
整批消失且不自愈。本脚本一次打印:

  1. 运行环境: RADAR_GEO 开关 / 数据目录 / 台账与缓存路径
  2. geocache.json 体检: 历史 None 条目(旧版毒缓存, 现行版本加载时自动丢弃)
  3. 坐标覆盖: 按国家分组, 判定每国坐标来自 内置表/缓存/在线, 统计实际出点数
  4. 内置质心表(webapp/geo_centroids.json)对台账国家的命中情况
  5. 接口自测: 进程内 test_client 直调 /api/summary 与 /api/events(真实数据路径),
     报告状态码/耗时/打点数 —— 它看到的就是浏览器将看到的
  6. --probe: 额外实探一次 Nominatim(默认不联网; 中国侧网络大概率超时, 属已知)

用法:
  python scripts/diagnose_map.py            # 只读诊断(默认不联网)
  python scripts/diagnose_map.py --probe    # 追加 Nominatim 实探(5s 短超时)
退出码: 0=有打点(地图正常) / 1=零打点或接口异常(地图问题仍在) / 2=接口 500。
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)  # namespace package: import webapp.app 需要**仓库根**在 path 上

import normalize  # noqa: E402
import webapp.app as radar  # noqa: E402


def _fmt_pos(pos):
    return "(%.2f, %.2f)" % pos if pos else "(无坐标)"


def main():
    ap = argparse.ArgumentParser(description="面板地图现场诊断(只读)")
    ap.add_argument("--probe", action="store_true",
                    help="实探一次 Nominatim(默认不联网; 5 秒短超时)")
    args = ap.parse_args()

    print("== 运行环境 ==")
    print("RADAR_GEO=%r -> GEO_ENABLED=%s" % (os.environ.get("RADAR_GEO"), radar.GEO_ENABLED))
    print("数据目录: %s" % normalize.DATA_DIR)
    print("台账: %s" % normalize.DB_PATH)
    print("地理缓存: %s" % radar.GEO_CACHE)
    print("内置质心表: %s" % radar._BUILTIN_GEO_PATH)

    # ---- geocache 体检 ----
    print("\n== geocache.json 体检 ==")
    raw = {}
    try:
        with open(radar.GEO_CACHE, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        print("(文件不存在 —— 全部国家走内置表或在线, 属正常)")
    except (OSError, ValueError) as exc:
        print("[异常] 读取失败: %s" % exc)
    none_keys = sorted(k for k, v in raw.items() if v is None)
    ok_n = sum(1 for v in raw.values() if v is not None)
    print("条目 %d(有效 %d, None %d)" % (len(raw), ok_n, len(none_keys)))
    if none_keys:
        print("历史 None 条目(旧版毒缓存; 现行版本加载时自动丢弃, 会自动重查):")
        for k in none_keys:
            print("  - %s" % k)

    # ---- 内置表与台账覆盖 ----
    print("\n== 坐标覆盖(台账按国家分组) ==")
    con = normalize.open_db(normalize.DB_PATH)
    try:
        events = normalize.load_events(con)
    finally:
        con.close()
    by_country = {}
    for e in events:
        if e.get("verification_status") == "merged" or e.get("record_type") != "policy":
            continue
        by_country.setdefault(str(e.get("country_en") or "?"), []).append(e)
    print("台账 policy 记录 %d 条, 涉及国家 %d 个" % (sum(len(v) for v in by_country.values()),
                                                     len(by_country)))
    cache = radar._load_geo_cache()
    n_hit_cache = n_hit_builtin = n_miss = 0
    miss_countries = []
    for country in sorted(by_country):
        if country in cache and cache[country]:
            src = "缓存"; n_hit_cache += len(by_country[country])
        elif radar._builtin_centroid(country):
            src = "内置表"; n_hit_builtin += len(by_country[country])
        else:
            src = "!! 无坐标(该国的点不会出现在地图上)"; n_miss += len(by_country[country])
            miss_countries.append(country)
        print("  %-28s %3d 条  %s" % (country, len(by_country[country]), src))
    will_plot = n_hit_cache + n_hit_builtin
    total = sum(len(v) for v in by_country.values())
    print("合计: 将出点 %d / %d 条(缓存 %d + 内置表 %d), 无坐标 %d"
          % (will_plot, total, n_hit_cache, n_hit_builtin, n_miss))
    if miss_countries:
        print("无坐标国家(内置表未收录, 在线又不可达时即缺点): %s"
              % ", ".join(miss_countries))

    # ---- 接口自测(进程内, 真实数据路径) ----
    print("\n== 接口自测(test_client, 不起服务) ==")
    with radar.app.test_client() as tc:
        t0 = time.time()
        rs = tc.get("/api/summary")
        print("GET /api/summary  http=%s  %.2fs" % (rs.status_code, time.time() - t0))
        t0 = time.time()
        re_ = tc.get("/api/events")
        dt = time.time() - t0
        n_ev = n_lat = -1
        try:
            data = re_.get_json()
            n_ev = len(data)
            n_lat = sum(1 for e in data if e.get("lat") is not None)
            print("GET /api/events   http=%s  %.2fs  记录 %d, 带坐标(=打点) %d"
                  % (re_.status_code, dt, n_ev, n_lat))
        except Exception:
            print("GET /api/events   http=%s  %.2fs  (响应不是 JSON —— 接口异常)"
                  % (re_.status_code, dt))
    rc = 0 if (re_.status_code == 200 and n_lat > 0) else (2 if re_.status_code != 200 else 1)

    if args.probe:
        print("\n== Nominatim 实探(--probe) ==")
        t0 = time.time()
        pos, net_fail = radar._nominatim_query("Uruguay")
        print("查询 Uruguay: %s  网络级失败=%s  耗时 %.1fs"
              % (_fmt_pos(pos), net_fail, time.time() - t0))
        if net_fail:
            print("(不可达属已知: 中国侧网络对该域经常整体超时 —— 内置表已兜底, 不影响出点)")

    print("\n== 结论 ==")
    if rc == 0:
        print("地图将渲染 %d 个打点, 接口正常。" % n_lat)
    elif rc == 2:
        print("接口异常(非 200) —— 前端 refresh() 会整块不渲染, 优先排查接口报错。")
    else:
        print("接口正常但打点 0 条 —— 若上方无坐标国家列表为空, 检查 RADAR_GEO 是否被置 0;")
        print("否则这些国家在内置表与缓存中都取不到坐标(在线又不可达), 需扩充内置表。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
