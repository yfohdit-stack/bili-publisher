"""账号状态与「近 24 小时投稿数」校验。

投稿前先数一遍这个账号最近 24 小时发出去的稿件，超过上限就拦下来，
避免触发B站的日投稿频率限制。
"""
from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


def http_json(url: str, cookie: str = "", timeout: int = 15,
              referer: str = "https://www.bilibili.com/") -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def credential_from_cfg(cfg: Dict[str, Any]):
    from bilibili_api import Credential

    c = cfg.get("cookie") or {}
    return Credential(
        sessdata=c.get("sessdata") or None,
        bili_jct=c.get("bili_jct") or None,
        buvid3=c.get("buvid3") or None,
        dedeuserid=c.get("dedeuserid") or None,
    )


def cookie_string(cfg: Dict[str, Any]) -> str:
    c = cfg.get("cookie") or {}
    parts = []
    for k, name in (("sessdata", "SESSDATA"), ("bili_jct", "bili_jct"),
                    ("buvid3", "buvid3"), ("dedeuserid", "DedeUserID")):
        if c.get(k):
            parts.append(f"{name}={c[k]}")
    return "; ".join(parts)


def fetch_account(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """用 nav 接口确认登录态。"""
    try:
        data = http_json("https://api.bilibili.com/x/web-interface/nav", cookie_string(cfg))
        d = data.get("data") or {}
        if data.get("code") == 0 and d.get("isLogin"):
            return {
                "logged_in": True,
                "mid": int(d.get("mid") or 0),
                "uname": d.get("uname") or "",
                "face": d.get("face") or "",
                "vip": (d.get("vipStatus") == 1),
                "level": (d.get("level_info") or {}).get("current_level", 0),
                "error": "",
            }
        return {
            "logged_in": False, "mid": 0, "uname": "", "face": "", "vip": False, "level": 0,
            "error": data.get("message") or "未登录",
        }
    except Exception as e:
        return {"logged_in": False, "mid": 0, "uname": "", "face": "", "vip": False, "level": 0,
                "error": f"请求失败: {e}"}


# 创作者中心稿件管理：三种状态都要算进投稿次数
MEM_API = "https://member.bilibili.com/x/web/archives"
MEM_REFERER = "https://member.bilibili.com/platform/upload-manager/article"
MEM_STATUSES = (("pubed", "已发布"), ("is_pubing", "审核中"), ("not_pubed", "未通过"))


def _fmt_duration(sec: Any) -> str:
    try:
        n = int(sec or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    h, m, s = n // 3600, (n % 3600) // 60, n % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _archives_page(cookie: str, status: str, pn: int, retries: int = 3) -> Dict[str, Any]:
    """取创作者中心某状态的稿件页，偶发超时/抖动时重试。"""
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return http_json(f"{MEM_API}?status={status}&pn={pn}&ps=30",
                             cookie, timeout=12, referer=MEM_REFERER)
        except Exception as e:  # 网络抖动、超时、被限速
            last = e
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"读取失败：{type(last).__name__}: {last}")


def _fetch_status(cookie: str, status: str, label: str,
                  cutoff: float) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """拉某一状态的全部分页；由线程池并发调用，互不共享可变状态。"""
    seen: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    pn = 1
    while pn <= 20:
        try:
            body = _archives_page(cookie, status, pn)
        except Exception as e:
            # 某个状态拉不到就先跳过，但明确告诉调用方这批数据不完整
            warnings.append(f"{label}列表没取到（{e}）")
            break
        if body.get("code") != 0:
            warnings.append(f"{label}列表返回异常：{body.get('message') or ''}"
                            f"（code={body.get('code')}）")
            break
        data = body.get("data") or {}
        rows = data.get("arc_audits") or []
        if not rows:
            break
        newest = 0
        for row in rows:
            arc = row.get("Archive") or row
            ctime = int(arc.get("ctime") or 0)
            newest = max(newest, ctime)
            key = str(arc.get("aid") or arc.get("bvid")
                      or f"{status}:{ctime}:{arc.get('title')}")
            if ctime >= cutoff:
                seen[key] = {
                    "title": arc.get("title") or "",
                    "bvid": arc.get("bvid") or "",
                    "aid": int(arc.get("aid") or 0),
                    "created": ctime,
                    "length": _fmt_duration(arc.get("duration")),
                    "status": label,
                    "state": arc.get("state"),
                }
        # 整页都早于窗口就没必要继续往后翻（兼容正序/倒序两种返回）
        if newest < cutoff:
            break
        total = int((data.get("page") or {}).get("total") or 0)
        if len(rows) < 30 or (total and pn * 30 >= total):
            break
        pn += 1
    return seen, warnings


def _creator_items_once(cfg: Dict[str, Any], hours: int) -> Dict[str, Any]:
    """投稿口径：已发布 + 审核中 + 未通过，按 ctime（投稿提交时间）落窗。

    三种状态彼此独立，原来串行请求，投稿前要等 T1+T2+T3（翻页时更久）。
    这里并发拉，耗时降到最慢的那一个，且仍逐条保留「没抓全」的告警。
    """
    cookie = cookie_string(cfg)
    cutoff = time.time() - hours * 3600
    seen: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    with ThreadPoolExecutor(max_workers=len(MEM_STATUSES)) as pool:
        futures = [pool.submit(_fetch_status, cookie, st, label, cutoff)
                   for st, label in MEM_STATUSES]
        # 按提交顺序合并，保证跨状态重复稿件时保留的口径与原串行版一致
        for fut in futures:
            try:
                part, warns = fut.result()
            except Exception as e:
                part, warns = {}, [f"列表拉取异常：{type(e).__name__}: {e}"]
            warnings.extend(warns)
            for k, v in part.items():
                seen.setdefault(k, v)
    return {"items": sorted(seen.values(), key=lambda x: x["created"], reverse=True),
            "warnings": warnings}


def _creator_items(cfg: Dict[str, Any], hours: int, retries: int = 2) -> Dict[str, Any]:
    """抓创作者中心的投稿列表；任一状态没取全就整轮重来一次。

    单个状态列表拉取失败会让统计数字偏小，偏小的数字若被当成「还有额度」，
    就会直接撞上B站的频率限制。所以先重试一轮，仍不全时把 warnings 交回调用方，
    由 check_quota 决定是保守拦截还是只提示。
    """
    got: Dict[str, Any] = {"items": [], "warnings": ["额度数据没取到"]}
    for attempt in range(max(1, retries)):
        got = _creator_items_once(cfg, hours)
        if not got["warnings"]:
            return got
        if attempt + 1 < max(1, retries):
            time.sleep(1.5 * (attempt + 1))
    return got


def submit_key(bvid: Any, aid: Any = 0) -> str:
    """一条稿件的身份键：优先 BV 号，没有 BV 号时才用 aid。

    BV 号是 base58，**大小写敏感**，只能 strip，绝不能转小写（BV1Pm 和 BV1pm 是两篇）。
    两边（B站列表 / 本机账本）都用这个函数生成键，才能真正对上。
    """
    b = str(bvid or "").strip()
    if b:
        return "b:" + b
    try:
        a = int(aid or 0)
    except (TypeError, ValueError):
        a = 0
    return ("a:%d" % a) if a else ""


def next_release_at(items: List[Dict[str, Any]], hours: int,
                    count: int, limit: int) -> float:
    """算出「最早能腾出一个投稿空位」的时刻，供队列等待额度时定时唤醒。

    24h 额度是滚动窗口：一篇稿在 created + hours 时滑出窗口、额度 -1。
    当前 count 已达/超上限时，要滑出 (count - limit + 1) 篇才有 1 个空位，
    取按时间升序第 need 篇的滑出时刻即可。

    它只决定「什么时候再去查一次额度」，不是放行依据：真正投不投仍由
    check_quota 现查决定。数据抓不全时返回 0，调用方退回低频轮询。
    """
    if not items:
        return 0.0
    try:
        need = max(1, int(count) - int(limit) + 1)
        ts = sorted(float(it["created"]) for it in items if it.get("created"))
    except (TypeError, ValueError):
        return 0.0
    if len(ts) < need:
        return 0.0
    return ts[need - 1] + max(1, int(hours)) * 3600


def quota_ban_until(cfg: Dict[str, Any], items: List[Dict[str, Any]],
                    hours: int, count: int, limit: int,
                    trustworthy: bool = True) -> float:
    """维护「24h 投稿封禁」状态，返回封禁截止时刻（0 = 未封禁）。

    B站实际规则：近 24h 投稿数一旦达到 n+1（超过上限），账号会被封禁投稿，
    封禁期内即使老稿陆续滑出、额度降到 n 以下也不放行，必须等窗口里的稿件
    全部过期、额度归 0 才能恢复。所以：

    - 触发：count > limit 时，截止 = 窗口内「最新一篇」ctime + hours，
      也就是额度归 0 的那一刻（不是最早一篇滑出、腾出一个空位的时刻）。
    - 封禁中：沿用已存截止时刻，绝不因为 count 变小而缩短或解除。
    - 解除：count == 0 时清除封禁。
    - 状态落盘到 config，重启/升级/多进程都不会丢。
    - 统计不完整（trustworthy=False）时不新设也不清除，沿用旧值，避免
      用一份偏小/偏大的数字把账号锁死或误放行。
    """
    from . import store as _store
    saved = float(cfg.get("quota_ban_until") or 0.0)
    if not trustworthy:
        return saved
    created = [float(it["created"]) for it in items if it.get("created")]
    newest = max(created) if created else 0.0
    now = time.time()

    def _persist(v: float) -> float:
        try:
            _store.patch_config({"quota_ban_until": v})
        except Exception:          # noqa: BLE001  写盘失败也不能放行本次投稿
            pass
        return v

    # 触发/续期：截止时刻始终指向「窗口内最新一篇 + hours」，即额度归零那一刻
    if count > limit and newest > 0:
        until = newest + max(1, int(hours)) * 3600
        if until > saved:
            saved = _persist(until)
    if count == 0:
        # 只有额度真正归 0 才解除封禁
        return _persist(0.0) if saved else 0.0
    if saved > 0 and newest > 0:
        # 封禁中但窗口还有稿件：绝不因 count 降到 limit 以下就放行。
        # 名义截止已过而稿件还在（列表延迟/状态回写）时短顺延，下一轮再查，
        # 既不会误放行，也不会永久卡死。
        until = newest + max(1, int(hours)) * 3600
        if until > saved:
            saved = _persist(until)
        if saved <= now:
            saved = _persist(now + 60)
    return saved


def _identity_keys(item: Dict[str, Any]) -> set[str]:
    """同时给出 BV 与 aid 两种身份键。

    上传通道可能只返回 aid，而创作者中心列表通常同时有 aid/bvid。若远端只生成
    BV 键、本地只生成 aid 键，同一篇稿就会被算两次。两个键都参与匹配才能对账。
    """
    return {k for k in (submit_key(item.get("bvid")), submit_key("", item.get("aid"))) if k}


def merge_quota(remote_keys: set[str], remote_count: int,
                rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把本机账本中「远端尚未显示」的稿件并入额度。

    本地重复记录也要去重：同一任务可能只有 aid、可能只有 BV，也可能后来补齐两者。
    用别名并查集把这些记录归为同一篇；连身份键都没有的记录保守地各算一篇。
    """
    pending_rows = []
    matched = 0
    for row in rows:
        keys = _identity_keys(row)
        if keys & remote_keys:
            matched += 1
        else:
            pending_rows.append(keys)

    parent = list(range(len(pending_rows)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = root(a), root(b)
        if ra != rb:
            parent[rb] = ra

    seen: Dict[str, int] = {}
    for i, keys in enumerate(pending_rows):
        for key in keys:
            if key in seen:
                union(i, seen[key])
            else:
                seen[key] = i
    # 无身份键的记录无法可靠去重，各算一篇（宁可保守，不漏额度）
    pending = len({root(i) for i, keys in enumerate(pending_rows) if keys})
    pending += sum(1 for keys in pending_rows if not keys)
    return {"confirmed": int(remote_count), "pending": pending,
            "count": int(remote_count) + pending, "intersect": matched,
            "fallback": False}


def split_quota(items: List[Dict[str, Any]], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """额度 = B站列表稿件 + 账本中尚未出现在列表里的稿件（按 BV/aid 去重）。"""
    remote_keys: set[str] = set()
    for item in items:
        remote_keys.update(_identity_keys(item))
    merged = merge_quota(remote_keys, len(items), rows)
    merged["keys"] = sorted(remote_keys)
    return merged


def check_quota(cfg: Dict[str, Any], account_key: Optional[str] = None) -> Dict[str, Any]:
    """返回 {ok, count, limit, mid, error, checked_at, items, incomplete}。

    额度 = 已确认（B站创作者中心能统计到的）+ 未确认（本机已投出、B站还没显示的）。
    后者来自 data/submit_ledger.json：v106 起在「开始上传」时就写一条占位记录，
    B站入库延迟、甚至提交请求超时，都不会让额度少算一篇。

    incomplete=True 表示这次统计没抓全（某个状态列表拉取失败，或只能退回
    「仅已发布」口径）。默认 quota_strict=True：数据不全时按保守策略拦下，
    不拿一个偏小的数字去放行；在设置里关掉严格模式可恢复旧行为（只提示、照常投）。
    """
    limit = int(cfg.get("quota_limit") or 50)
    hours = int(cfg.get("quota_hours") or 24)
    strict = bool(cfg.get("quota_strict", True))
    account = fetch_account(cfg)
    result: Dict[str, Any] = {
        "ok": False, "count": 0, "limit": limit, "hours": hours, "mid": 0,
        "error": "", "checked_at": time.time(), "items": [], "logged_in": account["logged_in"],
        "incomplete": False, "strict": strict,
        # 两个口径分开给界面：已确认=B站已统计到，未确认=本机已投但B站还没显示
        "confirmed": 0, "pending": 0, "remote_count": 0, "local_count": 0,
        "intersect": 0, "merge_fallback": False, "remote_keys": [], "inflight": 0,
        "next_release_at": 0.0, "quota_ban_until": 0.0,
    }
    if not account["logged_in"]:
        result["error"] = "账号未登录，请先在设置里粘贴浏览器 Cookie"
        return result
    mid = account["mid"]
    result["mid"] = mid

    def _guard_incomplete(note: str) -> None:
        """数据不全时的处理：严格模式拦下，宽松模式只在界面上说明。"""
        result["incomplete"] = True
        result["note"] = note
        if strict and result["ok"]:
            result["ok"] = False
            result["error"] = (f"额度统计不完整（{note}），已按保守策略拦下；"
                               f"点「刷新额度」重试，或在设置里关闭「额度严格模式」")

    try:
        got = _creator_items(cfg, hours)
        items = got["items"]
        by_status: Dict[str, int] = {}
        for it in items:
            by_status[it["status"]] = by_status.get(it["status"], 0) + 1
        remote_n = len(items)
        result["items"] = items[:20]
        result["by_status"] = by_status
        result["source"] = "creator+ledger"
        result["remote_count"] = remote_n
        # 本机账本：投稿成功即写，没有入库延迟，用来补「刚投出去还没显示的」
        rows: List[Dict[str, Any]] = []
        merged: Dict[str, Any] = {"confirmed": remote_n, "pending": 0, "count": remote_n,
                                  "intersect": 0, "fallback": True, "keys": []}
        try:
            from . import ledger
            from . import store as _store
            # 账号键必须落到同一只桶：记录的调用点用「任务账号 or 生效账号」，
            # 这里如果让空串原样透传，未确认会永远读成 0（投出去的稿全都不算额度）。
            key = account_key or _store.active_account_id() or ""
            rows = ledger.recent(key, hours)
            merged = split_quota(items, rows)
        except Exception:   # noqa: BLE001  账本读失败就退回纯远端口径，不阻断投稿
            pass
        result["local_count"] = len(rows)
        # 其中还没落地（上传中 / 结果未知）的条数，界面用它说明「未确认」的构成
        result["inflight"] = sum(1 for r in rows
                                 if r.get("state") in ("inflight", "uncertain"))
        result["confirmed"] = merged["confirmed"]
        result["pending"] = merged["pending"]
        result["intersect"] = merged["intersect"]
        result["merge_fallback"] = merged["fallback"]
        result["remote_keys"] = merged["keys"]
        result["count"] = merged["count"]
        # 达到 n+1 会触发 B站 24h 投稿封禁：封禁期内即使额度降到 n 以下也不能投，
        # 必须等窗口清空（count 归 0）。数据抓不全时沿用旧封禁值，不新设不清除。
        ban_until = quota_ban_until(cfg, items, hours, merged["count"], limit,
                                    trustworthy=not got["warnings"])
        result["quota_ban_until"] = ban_until
        now_ts = time.time()
        if ban_until > now_ts:
            result["ok"] = False
            result["next_release_at"] = ban_until
            result["error"] = (
                f"已触发 B站 {hours}h 投稿封禁（近 {hours} 小时投稿 {merged['count']} 个，"
                f"超过上限 {limit}）。封禁期内额度下降也不能投稿，需等额度归零后自动恢复；"
                f"预计 {time.strftime('%m-%d %H:%M', time.localtime(ban_until))} 解除")
        else:
            result["ok"] = merged["count"] < limit
            if not result["ok"]:
                # 只是刚好满 n（没到 n+1），等最早一篇滑出腾出空位即可
                result["next_release_at"] = next_release_at(items, hours, merged["count"], limit)
                extra = (f"（已确认 {merged['confirmed']} + 未确认 {merged['pending']}"
                         + (f"，其中 {result.get('inflight') or 0} 篇上传中/结果未知"
                            if result.get("inflight") else "")
                         + "）" if merged["pending"] > 0 else "")
                result["error"] = (f"近 {hours} 小时已投稿 {merged['count']} 个"
                                   f"（含审核中与未通过{extra}），达到上限 {limit}")
        if got["warnings"]:
            _guard_incomplete("；".join(got["warnings"]))
    except Exception as e:
        # 只保留创作者中心这一份口径；拿不到就算「统计不完整」，不再退回「仅已发布」的偏小数
        note = f"额度统计失败（创作者中心接口不可用）：{type(e).__name__}: {e}"
        result["incomplete"] = True
        result["source"] = "creator"
        result["note"] = note
        # 接口挂了不能把已有的封禁「忘掉」：封禁期内一律拦下，连宽松模式也不放行
        saved_ban = float(cfg.get("quota_ban_until") or 0.0)
        result["quota_ban_until"] = saved_ban
        if saved_ban > time.time():
            result["ok"] = False
            result["next_release_at"] = saved_ban
            result["error"] = (note + f"；且 {hours}h 投稿封禁尚未解除（预计 "
                               + time.strftime("%m-%d %H:%M", time.localtime(saved_ban))
                               + "），队列保持暂停")
        elif strict:
            result["ok"] = False
            result["error"] = note + "，已按保守策略拦下；点「刷新额度」重试，或在设置里关闭「额度严格模式」"
        else:
            result["ok"] = True
            result["error"] = ""
            result["note"] = note + "；宽松模式放行，实际投稿数可能已接近上限"
    return result


def quick_account(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return fetch_account(cfg)

def _state_label(arc: Dict[str, Any]) -> str:
    """把创作中心 state 码换成中文；-50 且 is_only_self 即「仅自己可见」。"""
    st = arc.get("state")
    if int(arc.get("is_only_self") or 0):
        return "仅自己可见"
    desc = (arc.get("state_desc") or "").strip()
    if desc and not desc.startswith("-"):
        return desc
    return {-2: "未通过", -30: "审核中", -1: "待审核", -4: "审核中",
            0: "开放浏览", -100: "已删除"}.get(st, f"状态 {st}")


ARCHIVE_STATUS = ("pubed", "is_pubing", "not_pubed")


def list_archives(cfg: Dict[str, Any], pn: int = 1, ps: int = 10,
                  status: str | None = None) -> Dict[str, Any]:
    """账号名下全部稿件（含审核中/未通过），按投稿时间倒序分页。

    status 可选 pubed / is_pubing / not_pubed，与概览条的「审核通过 / 审核中 / 未通过」
    一一对应，点概览格即可带着筛选进列表。
    """
    cookie = cookie_string(cfg)
    url = f"{MEM_API}?pn={pn}&ps={ps}"
    if status in ARCHIVE_STATUS:
        url += f"&status={status}"
    body = http_json(url, cookie, timeout=15, referer=MEM_REFERER)
    if body.get("code") != 0:
        raise RuntimeError(body.get("message") or f"code={body.get('code')}")
    data = body.get("data") or {}
    rows = data.get("arc_audits") or []
    items: List[Dict[str, Any]] = []
    for row in rows:
        arc = row.get("Archive") or row
        items.append({
            "title": arc.get("title") or "",
            "bvid": arc.get("bvid") or "",
            "aid": arc.get("aid") or 0,
            "state": arc.get("state"),
            "state_label": _state_label(arc),
            "created": int(arc.get("ctime") or 0),
            "duration": _fmt_duration(arc.get("duration")),
            "cover": (arc.get("cover") or "").replace("http://", "https://"),
            "is_only_self": int(arc.get("is_only_self") or 0),
            "mission_id": arc.get("mission_id") or 0,
            "tid": arc.get("tid") or 0,
        })
    total = int((data.get("page") or {}).get("count") or 0)
    total_pages = (total + ps - 1) // ps if total else 0
    return {"items": items, "page": pn, "ps": ps, "total": total,
            "status": status if status in ARCHIVE_STATUS else "",
            "total_pages": total_pages, "has_more": (pn < total_pages) if total_pages else len(rows) >= ps}

# ---------------------------------------------------------------- 概览统计
# 只取「第一页」读 page.count，绝不翻页 —— 这是账号级汇总数字，翻页既无意义
# 又容易触发 B站风控。四个状态各一次请求，由线程池并发，总计 4 次 GET。
CREATOR_STAT_API = "https://member.bilibili.com/x/web/index/stat"


def _page_count_once(url: str, cookie: str) -> int:
    """请求创作中心稿件列表第一页，只读 page.count；不拉稿件明细、不翻页。"""
    body = http_json(url, cookie, timeout=10, referer=MEM_REFERER)
    if body.get("code") != 0:
        raise RuntimeError(body.get("message") or ("code=%s" % body.get("code")))
    page = (body.get("data") or {}).get("page") or {}
    # 注意：count 缺失/为 0 时不能当真实 0——B站偶发返回空 page，重试一次再信
    if "count" not in page and "total" not in page:
        raise RuntimeError("接口未返回 page.count")
    return int(page.get("count") or page.get("total") or 0)


def _page_count(url: str, cookie: str, retries: int = 1) -> int:
    """带一次重试：B站对短时间多次请求会偶发返回空 page / 风控，单次失败不代表真是 0。"""
    last: Optional[Exception] = None
    for i in range(retries + 1):
        try:
            return _page_count_once(url, cookie)
        except Exception as e:   # noqa: BLE001
            last = e
            if i < retries:
                time.sleep(0.7)
    raise last if last else RuntimeError("稿件计数失败")


def account_stats(cfg: Dict[str, Any], part: str) -> Dict[str, Any]:
    """概览条数据源。part = workbench | archive。

    workbench -> 账号累计 播放/点赞/投币/收藏（一次 GET）
    archive   -> 稿件数/审核通过/审核中/未通过（4 次 GET，并发，只读 count）
    """
    cookie_obj = cfg.get("cookie") or {}
    if not cookie_obj.get("sessdata"):
        return {"ok": False, "error": "账号未登录，扫码登录后可查看", "items": {}}
    cookie = cookie_string(cfg)

    if part == "workbench":
        body = http_json(CREATOR_STAT_API, cookie, timeout=10, referer=MEM_REFERER)
        if body.get("code") != 0:
            raise RuntimeError(body.get("message") or ("code=%s" % body.get("code")))
        d = body.get("data") or {}

        def _n(*keys: str) -> int:
            # 逐个尝试候选字段名：B站改版时字段名可能变化，第一个缺失不能就此返回 0
            for k in keys:
                try:
                    v = int(d.get(k) or 0)
                except (TypeError, ValueError):
                    continue
                if v:
                    return v
            return 0

        def _delta(*keys: str) -> Optional[int]:
            """较昨日增量：0 是有效值，字段缺失才返回 None（前端据此决定显示还是不显示）。"""
            for k in keys:
                if k not in d:
                    continue
                try:
                    return int(d.get(k) or 0)
                except (TypeError, ValueError):
                    continue
            return None

        return {"ok": True,
                "items": {
                    "play": _n("total_click", "total_play", "click"),
                    "like": _n("total_like", "likes", "like"),
                    "coin": _n("total_coin", "coin"),
                    "fav": _n("total_fav", "total_favorite", "fav"),
                },
                # inc_* 是 B站给的「较昨日」增量，字段名前缀不统一（incr_click 但 inc_like）
                "incr": {
                    "play": _delta("incr_click", "inc_click"),
                    "like": _delta("inc_like", "incr_like"),
                    "coin": _delta("inc_coin", "incr_coin"),
                    "fav": _delta("inc_fav", "incr_fav"),
                }}

    if part == "archive":
        urls = {
            "total": "%s?pn=1&ps=1" % MEM_API,
            "pubed": "%s?status=pubed&pn=1&ps=1" % MEM_API,
            "is_pubing": "%s?status=is_pubing&pn=1&ps=1" % MEM_API,
            "not_pubed": "%s?status=not_pubed&pn=1&ps=1" % MEM_API,
        }
        got: Dict[str, int] = {}
        errs: Dict[str, str] = {}
        # 4 个并发容易被 B站风控（返回空 page / 412），降为 2 并配合单次重试
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = {k: pool.submit(_page_count, u, cookie) for k, u in urls.items()}
            for k, fut in futs.items():
                try:
                    got[k] = fut.result()
                except Exception as e:   # noqa: BLE001
                    errs[k] = "%s:%s" % (k, type(e).__name__)
        if len(errs) == len(urls):
            raise RuntimeError("稿件统计读取失败：" + "；".join(list(errs.values())[:2]))

        # 取不到的字段一律 None（前端显示「—」），绝不能拿 0 冒充真实数字
        items: Dict[str, Any] = {k: (int(got[k]) if k in got else None)
                                for k in ("total", "pubed", "is_pubing", "not_pubed")}
        if items["total"] is None:
            raise RuntimeError("稿件总数读取失败：" + "；".join(list(errs.values())[:2]))
        if not items["total"]:
            partial = [items[k] for k in ("pubed", "is_pubing", "not_pubed")]
            if all(v is not None for v in partial):
                # 总数接口偶发返回 0 时，用三种状态求和兜底；三状态都为真 0 才算真的 0
                items["total"] = sum(partial)   # type: ignore[arg-type]
            elif errs:
                raise RuntimeError("稿件总数为 0 且状态读取不全，" + "；".join(list(errs.values())[:2]))
        return {"ok": True, "items": items, "partial": bool(errs)}

    raise ValueError("未知的统计分组：%s" % part)

