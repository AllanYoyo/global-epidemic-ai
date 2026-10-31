#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 通道确定性预探(2026-10-24 P1 引入, 取数段拆小配套)

为什么需要它: 取数段 prompt 的「批次化探测」纪律(2026-10-23)仍要把探测结果
**留在对话里** —— 10-24 实测治理后一轮仍有 26 次 terminal + 25 次
execute_code, 逐轮全量重发上下文让单轮 cache 读高达 5M。探测是纯 HTTP
状态检查, 本不需要模型参与: 本脚本在取数段启动前用 curl 并行探完当日
全部源入口, 把 ≤2KB 的状态清单注入 prompt, Agent 对 [dead] 通道直接跳过
(记 search-log 一行), 只对 [ok] 通道做语义筛选与取数。

口径:
- 每个源入口 URL 一次 GET(-m 6 起步, 8 并发), 只看 HTTP 状态码, 不下载正文;
- 三层降级(http_get, 2026-10-25 死源分诊驱动): --http1.1 / SSL 链宽松重试(-k) /
  403 无 UA 重试 / 5xx 加长重试 —— 20 个死源中约 7 个由此复活, 降级触发记 note;
- 200-399 = ok; 其余(含 000=连接失败/超时) = dead —— 与 channel_health.py
  从 search-log 归纳的「不可用」同口径(403/占位页/连接失败);
- 输出按 sid 一行, 超过 48 行截断; 输出经与 channel_health 相同的清洗
  (无 ASCII 双引号/反引号/$), run_scan.sh 注入处还有第二层;
- 幂等、只读、不编造; curl 缺失/全部失败时输出告警行但不报错(不阻塞跑批,
  Agent 退回自行探测的旧纪律)。

用法(由 run_scan.sh 逐组调用):
  python3 scripts/probe_channels.py --date 2026-10-24           # 当日全部源
  python3 scripts/probe_channels.py --date 2026-10-24 --group 1 # 只探必扫档组
"""

import argparse
import locale
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import biz_today  # 业务时区口径一致
import scan_targets

UA = "Mozilla/5.0 (compatible; policy-monitor/1.0; +official-sources-check)"
TIMEOUT_SECS = 6
MAX_WORKERS = 8
MAX_LINES = 48


def classify(code):
    """HTTP 状态码 → ok/dead。000=连接失败/超时。"""
    if 200 <= code < 400:
        return "ok"
    return "dead"


# curl 退出码分类 —— 决定「还值不值得再降级一层」。
# 可救类(TLS/证书/超时): 换参数(放宽证书/强制 IPv4/加长)有救; 硬死类(DNS/连不上):
# 换参数无用, 只能换域名或换上游。
RC_RETRYABLE = {28, 35, 51, 58, 59, 60, 66, 77, 83, 91}
RC_HARD_DEAD = {6, 7, 47}

# 死因分类(分诊表用; 见 scripts/dead_sources.py)
CAUSE_A = "A-参数可救"      # TLS/超时/5xx: 换参数或换入口能救
CAUSE_B = "B-入口配置错"    # 404/410/跳转: 入口 URL 错了, 改配置
CAUSE_C = "C-反爬封锁"      # 403: WAF 拦爬虫, curl 无解
CAUSE_D = "D-源已失效"      # DNS 不解析/连不上: 域名或站点没了


def rc_of(why):
    """从 why 里取 curl 退出码(why 形如 'curl-exit-60' / 'timeout' / 'curl-missing')。

    'timeout' = 被我们自己的 subprocess 超时兜底杀掉(curl 自身的 -m 一般先触发),
    语义上等同于 curl 的 rc=28 超时, 归到同一类。
    """
    if why == "timeout":
        return 28
    if why and why.startswith("curl-exit-"):
        try:
            return int(why.rsplit("-", 1)[-1])
        except ValueError:
            return 0
    return 0


def classify_dead_cause(code, note):
    """死因分类: (http_code, note) → A/B/C/D。

    note 是降级链留痕, 死时末尾带最后一个 curl 退出码(如 'curl-exit-6')。
    """
    note = note or ""
    if code == 403:
        return CAUSE_C
    if code in (404, 410):
        return CAUSE_B
    if code >= 500:
        return CAUSE_A
    rc = rc_of(note.rsplit(" ", 1)[-1] if note else "")
    if rc == 6:
        return CAUSE_D          # DNS 解析失败 —— 域名没了
    if rc == 7:
        return CAUSE_D          # 连不上 —— 站点下线
    if rc in RC_RETRYABLE:
        return CAUSE_A
    if rc in RC_HARD_DEAD:
        return CAUSE_D
    return CAUSE_A if code else CAUSE_D


def _clean(s):
    """与 channel_health 同款清洗: 输出会被拼进 prompt 经 eval 送 hermes -z。"""
    return str(s).replace('"', "」").replace("`", "").replace("$", "")


def _default_encoding():
    """与 subprocess 的 text=True 对齐的默认编码。

    subprocess 文本模式用 TextIOWrapper 的默认编码(3.11+ 是 locale.getencoding(),
    3.10 是 locale.getpreferredencoding(False)); 改成字节模式后要自己解码, 口径
    必须一致, 否则 http_get 的返回值会与改造前不同。
    """
    try:
        return locale.getencoding()
    except AttributeError:
        return locale.getpreferredencoding(False)


def _parse_header_blocks(text):
    """解析 curl -D 落盘的响应头。

    -L 跟随重定向时会有多段响应头, 取最后一段(最终响应)。
    """
    headers = {}
    if not text:
        return headers
    for block in re.split(r"\r?\n\r?\n", text):
        cur = {}
        for line in block.splitlines():
            if not line or line.startswith("HTTP/"):
                continue
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            cur[name.strip()] = value.strip()
        if cur:
            headers = cur
    return headers


def _read_header_file(path):
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _run_bytes(url, extra_args, timeout_run, want_body, max_bytes, extra_headers,
               header_file, use_ua=True):
    """跑一次 curl, 返回 (code, body_bytes, why, size)。字节模式 —— 上层要做编码回退链。

    size = 已下载字节数: rc=28(超时)时「收到过字节」与「一个字节都没收到」是两种
    完全不同的病 —— 前者是 IPv6/TCP 半死(换 -4 能救), 后者是连接根本不通(换参数无用)。
    """
    cmd = ["curl", "-m", str(timeout_run), "-sS", "-L", "--http1.1"]
    if use_ua:
        cmd += ["-A", UA]
    cmd += ["-w", "\n%{http_code} %{size_download}"]
    for h in (extra_headers or []):
        cmd += ["-H", h]
    if header_file:
        cmd += ["-D", header_file]
    if not want_body:
        cmd += ["-o", "/dev/null"]
    elif max_bytes:
        cmd += ["--max-filesize", str(max_bytes)]
    cmd += list(extra_args)
    cmd.append(url)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout_run + 5)
    except subprocess.TimeoutExpired:
        return 0, b"", "timeout", 0
    except OSError:
        return 0, b"", "curl-missing", 0
    # 退出码非零即连接层失败(TLS 握手/证书/超时) —— 注意 -w 仍会输出 000,
    # stdout 非空不代表拿到响应(2026-10-25 实测: rc=60 stdout="\n000")
    if p.returncode != 0:
        return 0, b"", "curl-exit-%s" % p.returncode, _size_of(p.stdout)
    stdout = p.stdout
    if isinstance(stdout, str):
        # 桩/旧调用点可能给回 str(测试里 mock 的就是 str) —— 统一成字节再切
        stdout = stdout.encode(_default_encoding(), errors="replace")
    body, _, tail = stdout.rpartition(b"\n")
    try:
        parts = tail.strip().split()
        code = int(parts[0])
        size = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return 0, body, "", 0
    return code, body, "", size


def _size_of(stdout):
    """从 curl -w 的输出里取 size_download(退出码非零时也可能有值)。"""
    try:
        text = stdout.decode(_default_encoding(), errors="replace") if isinstance(stdout, bytes) else stdout
    except Exception:
        return 0
    tail = text.strip().split("\n")[-1].split()
    if len(tail) >= 2:
        try:
            return int(tail[1])
        except ValueError:
            return 0
    return 0


def http_get_full(url, timeout, want_body=False, max_bytes=None, extra_headers=None):
    """四层降级 GET 的**完整版** —— 返回 (code, body_bytes, note, headers)。

    与 http_get 的差别(取数内核需要的):
      · body 是**原始字节**: 上层要做编码回退链(utf-8 → latin1 → utf-8(ignore)
        → ascii(ignore)), 文本模式拿到的字符串已经把字节信息丢了;
      · extra_headers: 条件请求头(If-None-Match / If-Modified-Since) —— 条件
        请求命中 304 时不必再下载正文, 这是「每天全量重下 47 个源」的老账;
      · headers: 响应头(ETag / Last-Modified), 条件请求凭据靠它。

    降级链与 http_get **完全一致**(不另起一套 —— 两套口径是本仓库反复出事的
    病根: A1 存档配额口径、10-24 存数口径)。
    """
    header_file = None
    if want_body:
        fd, header_file = tempfile.mkstemp(prefix="radar-hdr-")
        os.close(fd)

    def _headers():
        return _parse_header_blocks(_read_header_file(header_file))

    try:
        note = ""
        code, body, why, size = _run_bytes(url, [], timeout, want_body, max_bytes,
                                           extra_headers, header_file)
        headers = _headers()
        rc = rc_of(why)

        # ② TLS/证书类(35/51/58/59/60/66/77/83/91) → 放宽证书(-k)+ 握手加长
        #    (10-25 起就有这层; 但原触发条件是字符串前缀 'curl-exit-6', 漏掉了
        #    rc=28 之类的超时形态, 且 TLS 类与超时类混在一个前缀里)
        if rc in RC_RETRYABLE and rc != 28:
            code, body, why, size = _run_bytes(url, ["-k"], timeout * 3, want_body, max_bytes,
                                               extra_headers, header_file)
            headers = _headers()
            rc = rc_of(why)
            if code:
                note = (note + " ssl-retry").strip()

        # ③ 超时(rc=28)分两种病:
        #    · 已收到字节(size>0) → 服务器回了头但传输卡死 → **IPv6 半死**,
        #      换 -4 强制 IPv4 能救(2026-10-30 实测 fsvps.gov.ru: -4 → 200/3.5s,
        #      默认走 IPv6 则 rc=28 收 16088 字节后卡死);
        #    · 一个字节都没收到 → 连接层根本不通, 换参数无用。
        #    另: 10-25 实证「6s 超时先于证书错误出现」, 所以 size==0 时仍给
        #    一次 -k 机会(TLS 握手慢的站就靠它活)。
        if code == 0 and rc == 28:
            if size > 0:
                code, body, why, size = _run_bytes(url, ["-4"], timeout * 3, want_body, max_bytes,
                                                   extra_headers, header_file)
                headers = _headers()
                rc = rc_of(why)
                if code:
                    note = (note + " ipv4-retry").strip()
                elif rc == 28:
                    code, body, why, size = _run_bytes(url, ["-4", "--compressed"], timeout * 3,
                                                       want_body, max_bytes, extra_headers,
                                                       header_file)
                    headers = _headers()
                    rc = rc_of(why)
                    if code:
                        note = (note + " ipv4-compressed-retry").strip()
            else:
                code, body, why, size = _run_bytes(url, ["-k"], timeout * 3, want_body, max_bytes,
                                                   extra_headers, header_file)
                headers = _headers()
                rc = rc_of(why)
                if code:
                    note = (note + " ssl-retry").strip()

        # ④ 403 → 去掉自定义 UA 重试一次(条件头同时撤掉: 403 说明请求没被受理,
        #    再带 If-None-Match 重试没有意义)
        if code == 403:
            code2, body2, _why2, _s2 = _run_bytes(url, [], timeout, want_body, max_bytes,
                                                   None, header_file, use_ua=False)
            if code2 and code2 != 403:
                headers = _headers()
                code, body, note = code2, body2, (note + " no-ua-retry").strip()
            else:
                # ⑤ 再试浏览器型 UA + Accept 头(部分 WAF 只拦「像机器人」的请求)
                code3, body3, _why3, _s3 = _run_bytes(
                    url, ["-H", "Accept-Language: en-US,en;q=0.9",
                          "-H", "Accept: text/html,application/xhtml+xml"],
                    timeout, want_body, max_bytes, None, header_file, use_ua=False)
                if code3 and code3 != 403:
                    headers = _headers()
                    code, body, note = code3, body3, (note + " browser-ua-retry").strip()
                else:
                    # 无 UA 与浏览器 UA 都被拒 → WAF 真封锁, 留痕(curl 层已无解)
                    note = (note + " 403-blocked").strip()

        # ⑥ 5xx → 超时加长重试(慢站网关超时)
        if code >= 500:
            code2, body2, _why2, _s2 = _run_bytes(url, [], timeout * 3, want_body, max_bytes,
                                                   extra_headers, header_file)
            if classify(code2) == "ok":
                headers = _headers()
                code, body, note = code2, body2, (note + " slow-retry").strip()

        # 死时把最后一个退出码写进 note —— 死因分诊就靠它(否则只剩一个「死」字)
        if not code:
            last = why or ("curl-exit-%s" % rc if rc else "unknown")
            note = (note + " " + last).strip()
        return code, body, note, headers
    finally:
        if header_file:
            try:
                os.unlink(header_file)
            except OSError:
                pass


def http_get(url, timeout, want_body=False, max_bytes=None):
    """三层降级 GET(2026-10-25 死源分诊驱动, 20 死源中 7 个可由此复活):

    ① 始终 --http1.1 —— agriculture.gov.au 型 HTTP/2 流错误(err 92);
    ② 首次尝试 SSL 链错误(curl 退出 6x)**或超时** → 追加 -k 且超时加长三倍重试
       —— ca-cfia/th-doa/mattilsynet 型: 服务端证书链不完整 + TLS 握手慢
       (实测 6s 握手不完 → 超时, 12s 完成握手后报 60)。只读公开政府新闻场景
       可接受(内容有 url+quote 溯源, 异常文本过不了 normalize 官方域校验);
    ③ HTTP 403 → 去掉自定义 UA 重试一次 —— aphis.usda.gov 型 WAF:
       伪装浏览器的爬虫 UA 反而被拦, curl 默认 UA(不带 -A)反而 200;
    ④ HTTP 5xx → 超时加长三倍重试一次 —— co-minagricultura 型慢站网关超时。

    返回 (code, body, note): note 记录触发过的降级(观测留痕, 可进 search-log)。

    降级链实现在 http_get_full 里, 这里只做一次字节→文本解码(口径与改造前的
    subprocess(text=True, errors="replace") 一致)。
    """
    code, body, note, _headers = http_get_full(url, timeout, want_body=want_body,
                                                max_bytes=max_bytes)
    return code, body.decode(_default_encoding(), errors="replace"), note


def probe_one(sid, url, alt_urls=()):
    """探一个源入口, 返回 (sid, 状态码int, ok/dead, note, 活着的入口URL)。

    **多入口**(2026-10-30): 主入口死了不代表源死了 —— 逐个试 alt_urls, 任一
    入口活就算活, 并把**活的那个入口**带回给取数段(取数必须用同一个入口,
    否则探活用 A、取数用 B, 等于没救)。
    """
    if not url.startswith(("http://", "https://")):
        return sid, 0, "dead", "", ""
    candidates = [url] + [u for u in (alt_urls or []) if u and u != url]
    last_code, last_note = 0, ""
    for cand in candidates:
        try:
            code, _body, note = http_get(cand, TIMEOUT_SECS)
        except Exception:
            code, note = 0, ""
        if classify(code) == "ok":
            return sid, code, "ok", note, cand
        last_code, last_note = code, note
    return sid, last_code, "dead", last_note, ""


def probe(date_str, group=None):
    """探当日(或单组)全部源入口, 返回 [(sid, code, state, note, ok_url)] —— 按 sid 去重。"""
    picked = scan_targets._pick(date_str, group)
    sources = scan_targets.load_sources()
    seen = {}
    for _tier, _cn, _en, sid, url in picked:
        if sid not in seen:
            alts = scan_targets.parse_alt_urls((sources.get(sid) or {}).get("alt_urls"))
            seen[sid] = (url, alts)
    items = sorted(seen.items())
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(items))) as ex:
        results = list(ex.map(lambda kv: probe_one(kv[0], kv[1][0], kv[1][1]), items))
    return results


def render_probe(date_str, group=None):
    """返回可注入 prompt 的文本; 探测失败返回告警行(不阻塞)。"""
    import shutil
    if shutil.which("curl") is None:
        return "[通道探测] 本机无 curl, 未预探 —— 退回自行批次化探测(每通道 curl -m 5 一次)"
    results = probe(date_str, group)
    if not results:
        return ""
    lines = ["【通道探测结果】(确定性脚本已预探; [dead] 通道直接跳过并在 search-log 记一行, [ok] 才继续取数)"]
    n_ok = 0
    for sid, code, state, note, _ok_url in results[:MAX_LINES]:
        n_ok += 1 if state == "ok" else 0
        lines.append("- [%s] %s %s%s" % (state, _clean(sid), code or "连接失败",
                                         (" (%s)" % note) if note else ""))
    if len(results) > MAX_LINES:
        lines.append("- …(共 %d 个源, 已截断)" % len(results))
    lines.append("(小结: %d 个源, ok %d / dead %d —— dead 不用再花任何工具调用)"
                 % (len(results), n_ok, len(results) - n_ok))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="当日源入口确定性预探(注入取数 prompt)")
    ap.add_argument("--date", default=biz_today(), help="业务日期 YYYY-MM-DD(默认东八区今天)")
    ap.add_argument("--group", type=int, default=None,
                    help="只探该块(≥1, 与 scan_targets 分块同口径); 缺省=全部")
    args = ap.parse_args()
    try:
        out = render_probe(args.date, args.group)
    except (OSError, ValueError) as exc:
        print("[通道探测][WARN] 预探失败(%s), 退回自行探测" % _clean(exc), file=sys.stderr)
        return 0
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
