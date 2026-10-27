"""公開資訊觀測站 → Discord 通知

追蹤四種資料,有新的就推到 Discord:
  1. 即時重大訊息(上市、上櫃)      每次執行都檢查
  2. 自結損益(從重大訊息主旨挑出來)  跟著重大訊息一起,另外標記
  3. 月營收(新申報的公司)            每 30 分鐘檢查一次(每月 1~15 日)
  4. 財報(季報 / 年報,新申報的公司)   每 60 分鐘檢查一次(申報期間)

用法:
    pip install -r requirements.txt
    python mops_discord.py --test        # 發一則測試訊息,確認 Webhook 設定正確
    python mops_discord.py               # 檢查一次後結束(GitHub Actions 用這個)
    python mops_discord.py --loop        # 一直跑,每 60 秒檢查一次(放在自己電腦上用)

第一次執行只會記下「目前已有的資料」,不會把今天之前的東西一口氣全部推出去。

設定(擇一,環境變數優先):
    環境變數 DISCORD_WEBHOOK_URL               預設頻道(全部都發這裡)
            DISCORD_WEBHOOK_NEWS / _SELFREPORT / _REVENUE / _FINANCIAL   分頻道(可選)
    或同資料夾的 config.json(參考 config.example.json)
"""
import argparse
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

HERE = Path(__file__).resolve().parent
TPE = timezone(timedelta(hours=8))
MOPS = "https://mopsov.twse.com.tw"
REALTIME = f"{MOPS}/mops/web/ajax_t05sr01_1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MKT_NAME = {"sii": "上市", "otc": "上櫃", "rotc": "興櫃"}

DEFAULTS = {
    "markets": ["sii", "otc"],
    "selfreport_keywords": ["自結", "自行結算"],
    "news_exclude_keywords": [],          # 主旨含這些字的重大訊息不推(例如「代子公司」)
    "news_desc_chars": 800,               # 重大訊息說明最多顯示幾個字
    "detail_max_per_run": 80,             # 每次最多補抓幾則說明(其餘下次)
    "revenue_every_min": 30,
    "financial_every_min": 60,
    "loop_seconds": 60,
}
WEBHOOK_KEYS = ("default", "news", "selfreport", "revenue", "financial")
COLORS = {"news": 0x3B82F6, "selfreport": 0xF59E0B}

S = requests.Session()
S.headers["User-Agent"] = UA


# ------------------------------------------------------------------ 設定 / 狀態
def load_config(path):
    cfg = dict(DEFAULTS)
    hooks = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        hooks.update(raw.pop("webhooks", {}) or {})
        cfg.update(raw)
    env = {"default": "DISCORD_WEBHOOK_URL", "news": "DISCORD_WEBHOOK_NEWS",
           "selfreport": "DISCORD_WEBHOOK_SELFREPORT", "revenue": "DISCORD_WEBHOOK_REVENUE",
           "financial": "DISCORD_WEBHOOK_FINANCIAL"}
    for k, var in env.items():
        if os.environ.get(var, "").strip():
            hooks[k] = os.environ[var].strip()
    cfg["webhooks"] = {k: v for k, v in hooks.items() if k in WEBHOOK_KEYS and v}
    if not cfg["webhooks"]:
        sys.exit("沒有設定 Discord Webhook:請設定環境變數 DISCORD_WEBHOOK_URL 或 config.json")
    return cfg


def hook_for(cfg, kind):
    h = cfg["webhooks"]
    return h.get(kind) or (h.get("news") if kind == "selfreport" else None) or h.get("default")


def load_state(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[state] 讀取失敗({e}),重新開始", file=sys.stderr)
    return {}


def save_state(path, st):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def now():
    return datetime.now(TPE)


def due(st, name, minutes):
    last = st.setdefault("last", {}).get(name)
    return not last or now().timestamp() - last >= minutes * 60 - 30


def mark(st, name):
    st.setdefault("last", {})[name] = now().timestamp()


# ------------------------------------------------------------------ 工具
def roc_date(s):
    """1151007 / 115/10/07 / 20261007 → 2026-10-07"""
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) == 8 and d[:2] in ("19", "20"):
        y, m, dd = int(d[:4]), int(d[4:6]), int(d[6:])
    elif len(d) in (6, 7):
        y, m, dd = int(d[:-4]) + 1911, int(d[-4:-2]), int(d[-2:])
    else:
        return ""
    try:
        return date(y, m, dd).isoformat()
    except ValueError:
        return ""


def hms(s):
    d = re.sub(r"\D", "", str(s or "")).zfill(6)[-6:]
    return f"{d[:2]}:{d[2:4]}:{d[4:]}" if d.strip("0") else ""


def text(h):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", h))).strip()


def fetch(url, method="get", data=None, tries=3, encoding=None):
    """觀測站偶爾回「查詢過於頻繁」,遇到就等一下重試。404 = 還沒公布。"""
    for i in range(tries):
        try:
            r = S.request(method, url, data=data, timeout=45)
            if r.status_code == 404:
                return None
            if encoding:
                r.encoding = encoding
            elif not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = "utf-8"
            if r.status_code == 200 and "頻繁" not in r.text[:3000]:
                return r.text
            print(f"  [retry {i + 1}] {url} HTTP {r.status_code}"
                  f"{' 查詢過於頻繁' if '頻繁' in r.text[:3000] else ''}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  [retry {i + 1}] {url} {e}", file=sys.stderr)
        time.sleep(8 * (i + 1))
    return None


def disp_width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s, w, right=False):
    s = str(s)
    while disp_width(s) > w and len(s) > 1:
        s = s[:-1]
    sp = " " * max(0, w - disp_width(s))
    return sp + s if right else s + sp


def money(k):
    """千元 → 好讀的字串"""
    if k is None:
        return "-"
    yi = k / 100_000
    if abs(yi) >= 1:
        return f"{yi:,.2f}億"
    return f"{k / 10:,.0f}萬"


def eps_str(x):
    return "-" if x is None else f"{x:.2f}"


def pct(x):
    return "-" if x is None else f"{x:+.1f}%"


def num(s):
    s = str(s).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "--", "nan", "NaN", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ------------------------------------------------------------------ Discord
def post(url, payload, tries=5):
    for _ in range(tries):
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After") or r.json().get("retry_after", 2))
            time.sleep(wait + 0.5)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Discord 回應 {r.status_code}: {r.text[:300]}")
        time.sleep(0.7)       # 同一個 Webhook 大約每分鐘 30 則,別撞到上限
        return
    raise RuntimeError("Discord 一直限速,稍後再試")


def post_embeds(url, embeds, on_sent=lambda idx: None):
    """每則訊息最多 10 個 embed、總字數 6000。每送出一批就回報那批的索引。"""
    batch, size = [], 0
    for i, e in enumerate(embeds):
        n = len(json.dumps(e, ensure_ascii=False))
        if batch and (len(batch) == 10 or size + n > 5500):
            post(url, {"embeds": [embeds[j] for j in batch]})
            on_sent(batch)
            batch, size = [], 0
        batch.append(i)
        size += n
    if batch:
        post(url, {"embeds": [embeds[j] for j in batch]})
        on_sent(batch)


def post_table(url, title, header, lines):
    """把表格切成多則訊息(每則 2000 字以內),放在程式碼區塊裡對齊。"""
    chunks, cur = [], []
    budget = 1900 - len(title) - len(header)
    for ln in lines:
        if cur and sum(len(x) + 1 for x in cur) + len(ln) > budget:
            chunks.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        chunks.append(cur)
    for i, c in enumerate(chunks):
        head = title + (f"({i + 1}/{len(chunks)})" if len(chunks) > 1 else "")
        post(url, {"content": f"{head}\n```\n{header}\n" + "\n".join(c) + "\n```"})


# ------------------------------------------------------------------ 1. 即時重大訊息
def news_list(mkt):
    page = fetch(REALTIME, "post", {"encodeURIComponent": 1, "step": 0, "firstin": 1,
                                    "off": 1, "TYPEK": mkt})
    if page is None or "fm_t05sr01_1" not in page:
        if page is not None and "查無" in page:
            return []
        raise RuntimeError("即時重大訊息回應格式不符(可能被擋或改版)")
    rows = []
    for tr in re.findall(r"<tr class='(?:odd|even)'>(.*?)</tr>", page, re.S):
        td = [text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        form = dict(re.findall(r"fm_t05sr01_1\.(\w+)\.value='([^']*)'", tr))
        if len(td) < 5 or not form.get("COMPANY_ID"):
            continue
        r = {"mkt": mkt, "code": td[0], "name": td[1], "date": roc_date(td[2]),
             "time": hms(td[3]), "subject": td[4], "form": form}
        if r["code"] and r["date"]:
            rows.append(r)
    return rows


def news_key(r):
    f = r["form"]
    return f"{r['code']}|{r['date']}|{r['time']}|{f.get('SEQ_NO', '')}"


def news_link(r):
    q = {"firstin": "true", "stp": 1, "step": 1}
    q.update({k: r["form"][k] for k in ("SEQ_NO", "SPOKE_TIME", "SPOKE_DATE", "COMPANY_ID")
              if k in r["form"]})
    return f"{REALTIME}?{urlencode(q)}"


def news_detail(r):
    page = fetch(REALTIME, "post", {"TYPEK": "all", "step": 1, "firstin": "true", **r["form"]})
    if page is None:
        raise RuntimeError("抓不到說明")
    if m := re.search(r"<pre[^>]*>(.*?)</pre>", page, re.S):
        r["desc"] = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).replace("\r\n", "\n").strip()
    if c := re.search(r"符合條款.*?第</th>\s*<td[^>]*>(.*?)</td>", page, re.S):
        r["clause"] = f"第{text(c.group(1))}款"
    if d := re.search(r"事實發生日</th>\s*<td[^>]*>(.*?)</td>", page, re.S):
        r["eventDate"] = roc_date(text(d.group(1))) or text(d.group(1))


def is_selfreport(cfg, subject):
    return any(k in subject for k in cfg["selfreport_keywords"])


def news_embed(cfg, r, selfreport):
    desc = (r.get("desc") or "").strip()
    lim = cfg["news_desc_chars"]
    if len(desc) > lim:
        desc = desc[:lim].rstrip() + "…(全文請點標題)"
    tag = "【自結】" if selfreport else ""
    fields = [{"name": "市場", "value": MKT_NAME.get(r["mkt"], r["mkt"]), "inline": True},
              {"name": "發言時間", "value": f"{r['date']} {r['time']}", "inline": True}]
    if r.get("clause"):
        fields.append({"name": "符合條款", "value": r["clause"], "inline": True})
    if r.get("eventDate"):
        fields.append({"name": "事實發生日", "value": r["eventDate"], "inline": True})
    e = {"title": f"{tag}{r['code']} {r['name']}｜{r['subject']}"[:256],
         "url": news_link(r),
         "color": COLORS["selfreport" if selfreport else "news"],
         "fields": fields,
         "footer": {"text": "公開資訊觀測站・即時重大訊息"}}
    if desc:
        e["description"] = desc[:4000]
    try:
        e["timestamp"] = datetime.fromisoformat(f"{r['date']}T{r['time'] or '00:00:00'}") \
            .replace(tzinfo=TPE).isoformat()
    except ValueError:
        pass
    return e


def run_news(cfg, st, seed):
    seen = st.setdefault("news", {})
    new = []
    for mkt in cfg["markets"]:
        try:
            rows = news_list(mkt)
        except Exception as e:  # noqa: BLE001
            print(f"[重大訊息 {MKT_NAME.get(mkt, mkt)}] 失敗:{e}", file=sys.stderr)
            continue
        print(f"[重大訊息 {MKT_NAME.get(mkt, mkt)}] 今日 {len(rows)} 則")
        new += [r for r in rows if news_key(r) not in seen]
    new.sort(key=lambda r: (r["date"], r["time"], r["code"]))

    if seed:
        for r in new:
            seen[news_key(r)] = r["date"]
        print(f"[重大訊息] 首次執行,記下 {len(new)} 則,不推送")
    else:
        excl = cfg["news_exclude_keywords"]
        out = {"news": [], "selfreport": []}
        for i, r in enumerate(new[:cfg["detail_max_per_run"]]):
            if any(k in r["subject"] for k in excl):
                seen[news_key(r)] = r["date"]
                continue
            try:
                news_detail(r)
            except Exception as e:  # noqa: BLE001
                print(f"[說明] {r['code']} {r['time']} 失敗:{e}(仍會推送標題)", file=sys.stderr)
            time.sleep(0.5)
            kind = "selfreport" if is_selfreport(cfg, r["subject"]) else "news"
            out[kind].append((r, news_embed(cfg, r, kind == "selfreport")))
        # 依 Webhook 分組送出;送成功才記為已推送
        for kind, items in out.items():
            if not items:
                continue
            def sent(idx, items=items):
                for j in idx:
                    seen[news_key(items[j][0])] = items[j][0]["date"]
            post_embeds(hook_for(cfg, kind), [e for _, e in items], sent)
            print(f"[{'自結' if kind == 'selfreport' else '重大訊息'}] 推送 {len(items)} 則")
        if len(new) > cfg["detail_max_per_run"]:
            print(f"[重大訊息] 還有 {len(new) - cfg['detail_max_per_run']} 則下次推送")

    cutoff = (now().date() - timedelta(days=10)).isoformat()
    st["news"] = {k: d for k, d in seen.items() if d >= cutoff}


# ------------------------------------------------------------------ 3. 月營收
def revenue_page(mkt, roc_y, m):
    """回傳 {代號: {...}};還沒公布回傳 {}。"""
    import pandas as pd  # 只有這裡和財報需要

    page = fetch(f"{MOPS}/nas/t21/{mkt}/t21sc03_{roc_y}_{m}_0.html", encoding="big5hkscs")
    if page is None:
        return {}
    try:
        tables = pd.read_html(io.StringIO(page))
    except ValueError:
        return {}
    out = {}
    for t in tables:
        cols = [" ".join(str(x) for x in c if not str(x).startswith("Unnamed"))
                if isinstance(c, tuple) else str(c) for c in t.columns]
        if t.shape[1] < 7 or not any("代號" in c for c in cols):
            continue
        t.columns = cols
        code_c = next(c for c in cols if "代號" in c)
        name_c = next((c for c in cols if "名稱" in c), None)
        rev_c = next((c for c in cols if "當月營收" in c and "累計" not in c), None)
        mom_c = next((c for c in cols if "上月比較" in c), None)
        yoy_c = next((c for c in cols if "去年同月" in c), None)
        if rev_c is None:
            continue
        for _, row in t.iterrows():
            code = str(row[code_c]).strip()
            if not re.fullmatch(r"\d{4,6}[A-Z]?", code):
                continue
            out[code] = {"name": str(row[name_c]).strip() if name_c else "",
                         "rev": num(row[rev_c]),
                         "mom": num(row[mom_c]) if mom_c else None,
                         "yoy": num(row[yoy_c]) if yoy_c else None}
    return out


def run_revenue(cfg, st, seed):
    t = now()
    if not seed and t.day > 15 and st.get("rev"):
        return      # 月營收在每月 10 日前申報,15 日之後就不再頻繁檢查
    if not seed and not due(st, "revenue", cfg["revenue_every_min"]):
        return
    first = t.replace(day=1) - timedelta(days=1)          # 上個月
    roc_y, m = first.year - 1911, first.month
    key = f"{roc_y}_{m:02d}"
    seen = st.setdefault("rev", {}).setdefault(key, [])
    seen_set = set(seen)
    new = []
    for mkt in cfg["markets"]:
        try:
            data = revenue_page(mkt, roc_y, m)
        except Exception as e:  # noqa: BLE001
            print(f"[月營收 {MKT_NAME.get(mkt, mkt)}] 失敗:{e}", file=sys.stderr)
            continue
        print(f"[月營收 {MKT_NAME.get(mkt, mkt)}] {roc_y}年{m}月 已申報 {len(data)} 家")
        new += [(mkt, c, v) for c, v in data.items() if f"{mkt}:{c}" not in seen_set]
        time.sleep(2)
    mark(st, "revenue")
    if new and not seed:
        new.sort(key=lambda x: (x[0], x[1]))
        header = f"{pad('代號', 6)} {pad('名稱', 10)} {pad('當月營收', 12, True)} {pad('月增', 8, True)} {pad('年增', 8, True)}"
        lines = [f"{pad(c, 6)} {pad(v['name'], 10)} {pad(money(v['rev']), 12, True)} "
                 f"{pad(pct(v['mom']), 8, True)} {pad(pct(v['yoy']), 8, True)}"
                 + (" 🔥" if (v["yoy"] or 0) >= 30 else "")
                 for mkt, c, v in new]
        title = f"📈 **{roc_y}年{m}月 月營收｜新申報 {len(new)} 家**"
        post_table(hook_for(cfg, "revenue"), title, header, lines)
        print(f"[月營收] 推送 {len(new)} 家")
    seen += [f"{mkt}:{c}" for mkt, c, _ in new]
    # 只留最近 3 個月
    st["rev"] = dict(sorted(st["rev"].items())[-3:])


# ------------------------------------------------------------------ 4. 財報
DEADLINE = {1: (5, 15), 2: (8, 14), 3: (11, 14), 4: (3, 31)}   # 季報 / 年報法定期限


def open_quarters(d):
    """目前還在申報期間的季度(期限後再多等 10 天,收晚報的)。回傳 [(西元年, 季)]。"""
    out = []
    for y in (d.year - 1, d.year):
        for s in (1, 2, 3, 4):
            q_end = date(y, s * 3, 28)
            mo, dd = DEADLINE[s]
            deadline = date(y + (s == 4), mo, dd)
            if q_end < d <= deadline + timedelta(days=10):
                out.append((y, s))
    return out


def statement(mkt, roc_y, season):
    """綜合損益表彙總(累計值)。回傳 {代號: {...}}。"""
    import pandas as pd

    form = {"encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1, "isQuery": "Y",
            "TYPEK": mkt, "year": str(roc_y), "season": f"{season:02d}"}
    page = fetch(f"{MOPS}/mops/web/ajax_t163sb04", "post", form)
    if page is None:
        return {}
    try:
        tables = pd.read_html(io.StringIO(page))
    except ValueError:
        return {}

    def pick(cols, *pats):
        for p in pats:
            for c in cols:
                if re.search(p, c):
                    return c
        return None

    out = {}
    for t in tables:
        cols = [" ".join(str(x) for x in c if not str(x).startswith("Unnamed"))
                if isinstance(c, tuple) else str(c) for c in t.columns]
        code_c = pick(cols, r"代號")
        if code_c is None or t.shape[1] < 6:
            continue
        t.columns = cols
        name_c = pick(cols, r"名稱")
        rev_c = pick(cols, r"^營業收入$", r"^收益合計$", r"^收入$", r"淨收益$", r"營業收入")
        op_c = pick(cols, r"^營業利益", r"^營業損益")
        ni_c = pick(cols, r"^淨利.*歸屬於母公司業主$", r"^本期淨利", r"^本期稅後淨利")
        eps_c = pick(cols, r"基本每股盈餘")
        for _, row in t.iterrows():
            code = str(row[code_c]).strip()
            if not re.fullmatch(r"\d{4,6}[A-Z]?", code):
                continue
            out[code] = {"name": str(row[name_c]).strip() if name_c else "",
                         "rev": num(row[rev_c]) if rev_c else None,
                         "op": num(row[op_c]) if op_c else None,
                         "ni": num(row[ni_c]) if ni_c else None,
                         "eps": num(row[eps_c]) if eps_c else None}
    return out


def run_financial(cfg, st, seed):
    qs = open_quarters(now().date())
    if not qs or (not seed and not due(st, "financial", cfg["financial_every_min"])):
        return
    fin = st.setdefault("fin", {})
    for y, s in qs:
        roc_y = y - 1911
        key = f"{roc_y}Q{s}"
        seen = fin.setdefault(key, [])
        seen_set = set(seen)
        new = []
        for mkt in cfg["markets"]:
            try:
                data = statement(mkt, roc_y, s)
            except Exception as e:  # noqa: BLE001
                print(f"[財報 {MKT_NAME.get(mkt, mkt)}] 失敗:{e}", file=sys.stderr)
                continue
            print(f"[財報 {MKT_NAME.get(mkt, mkt)}] {roc_y}年Q{s} 已申報 {len(data)} 家")
            new += [(mkt, c, v) for c, v in data.items() if f"{mkt}:{c}" not in seen_set]
            time.sleep(3)
        if new and not seed:
            new.sort(key=lambda x: (x[0], x[1]))
            label = f"{roc_y}年度 年報" if s == 4 else f"{roc_y}年第{s}季"
            ytd = "全年" if s == 4 else ("單季" if s == 1 else f"前{s}季累計")
            header = (f"{pad('代號', 6)} {pad('名稱', 10)} {pad('EPS', 7, True)} "
                      f"{pad('稅後淨利', 11, True)} {pad('營業利益', 11, True)}")
            lines = [f"{pad(c, 6)} {pad(v['name'], 10)} "
                     f"{pad(eps_str(v['eps']), 7, True)} "
                     f"{pad(money(v['ni']), 11, True)} {pad(money(v['op']), 11, True)}"
                     for mkt, c, v in new]
            title = f"📊 **{label} 財報｜新申報 {len(new)} 家**(金額為{ytd})"
            post_table(hook_for(cfg, "financial"), title, header, lines)
            print(f"[財報] 推送 {len(new)} 家")
        seen += [f"{mkt}:{c}" for mkt, c, _ in new]
    mark(st, "financial")
    st["fin"] = {k: v for k, v in fin.items()
                 if k in {f"{y - 1911}Q{s}" for y, s in qs}}


# ------------------------------------------------------------------ 主程式
def run_once(cfg, state_path):
    st = load_state(state_path)
    seed = not st.get("initialized")
    if seed:
        print("[首次執行] 先記下目前已有的資料,之後只推送新的")
    for name, fn in (("重大訊息", run_news), ("月營收", run_revenue), ("財報", run_financial)):
        try:
            fn(cfg, st, seed)
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] 發生錯誤:{e}", file=sys.stderr)
        save_state(state_path, st)      # 每段做完就存,中途失敗也不會重複推送
    if seed:
        st["initialized"] = now().isoformat()
        save_state(state_path, st)
        post(hook_for(cfg, "news"), {"content":
             "✅ 公開資訊觀測站通知已啟動。之後的新重大訊息、自結、月營收與財報會推送到這裡。"})


def main():
    ap = argparse.ArgumentParser(description="公開資訊觀測站 → Discord 通知")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--state", default=str(HERE / "state.json"))
    ap.add_argument("--loop", action="store_true", help="持續執行(自己電腦上用)")
    ap.add_argument("--test", action="store_true", help="發送測試訊息到每個設定的 Webhook")
    a = ap.parse_args()
    cfg = load_config(Path(a.config))
    state_path = Path(a.state)

    if a.test:
        for k, url in cfg["webhooks"].items():
            post(url, {"content": f"🔔 測試訊息:Webhook「{k}」設定成功"})
            print(f"[test] {k} OK")
        return

    if not a.loop:
        run_once(cfg, state_path)
        return
    print(f"[loop] 每 {cfg['loop_seconds']} 秒檢查一次,按 Ctrl+C 結束")
    while True:
        t = now()
        quiet = t.hour < 6       # 半夜幾乎沒有公告,改成 10 分鐘一次
        try:
            run_once(cfg, state_path)
        except Exception as e:  # noqa: BLE001
            print(f"[loop] {e}", file=sys.stderr)
        time.sleep(600 if quiet else cfg["loop_seconds"])


if __name__ == "__main__":
    main()
