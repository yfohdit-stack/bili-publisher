"""合集（season）相关：列合集、给稿件挂合集、回读校验。

投稿本身不带 season_id（投稿接口没有这个参数），
所以走「投稿成功后再调 switch 接口把 aid 挂进合集」这条路，
两条上传通道都能用。
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from . import quota

MEM = "https://member.bilibili.com/x2/creative/web"
COVER_UP = "https://member.bilibili.com/x/vu/web/cover/up"
REFERER = "https://member.bilibili.com/platform/upload-manager/article"
_cache: Dict[str, Dict[str, Any]] = {}   # 多账号：按 mid 分键缓存，切换账号不会串


def _csrf(cfg: Dict[str, Any]) -> str:
    return ((cfg.get("cookie") or {}).get("bili_jct")) or ""


def _post_json(path: str, body: Dict[str, Any], cfg: Dict[str, Any],
               with_csrf: bool = True) -> Dict[str, Any]:
    csrf = _csrf(cfg)
    url = f"{MEM}{path}"
    if with_csrf:
        url += f"?csrf={csrf}&t={int(time.time() * 1000)}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "User-Agent": quota.UA,
        "Referer": REFERER,
        "Content-Type": "application/json",
        "Cookie": quota.cookie_string(cfg),
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    # 这个接口偶尔把同一段 JSON 连写两遍，只取第一个完整对象
    try:
        value, _ = json.JSONDecoder().raw_decode(raw.lstrip())
    except ValueError:
        raise RuntimeError(f"合集接口返回无法解析：{raw[:200]}")
    return value if isinstance(value, dict) else {}


def list_seasons(cfg: Dict[str, Any], force: bool = False) -> List[Dict[str, Any]]:
    """账号下的合集列表（含默认小节的 section_id）。"""
    now = time.time()
    key = str(((cfg.get("account") or {}).get("mid")) or ((cfg.get("cookie") or {}).get("dedeuserid")) or "_")
    item = _cache.get(key) or {}
    if not force and item.get("at") and now - item.get("at", 0) < 60:
        return item["items"]

    out: List[Dict[str, Any]] = []
    pn = 1
    while pn <= 6:
        body = quota.http_json(f"{MEM}/seasons?pn={pn}&ps=50",
                               quota.cookie_string(cfg), referer=REFERER)
        if body.get("code") != 0:
            raise RuntimeError(body.get("message") or f"code={body.get('code')}")
        data = body.get("data") or {}
        rows = data.get("seasons") or []
        if not rows:
            break
        for row in rows:
            s = row.get("season") or {}
            if not s.get("id"):
                continue
            sections = ((row.get("sections") or {}).get("sections") or [])
            first = sections[0] if sections else {}
            eps = first.get("epCount")
            if eps is None:
                eps = len(row.get("part_episodes") or [])
            out.append({
                "id": int(s["id"]),
                "title": s.get("title") or "",
                "count": int(eps or 0),
                "section_id": int(first.get("id") or 0),
                "state": s.get("state"),
            })
        total = int(data.get("total") or 0)
        if len(rows) < 50 or (total and len(out) >= total):
            break
        pn += 1

    _cache[key] = {"at": now, "items": out}
    return out


_section_cache: Dict[str, Dict[str, Any]] = {}   # mid:season_id -> section_id

SECTION_TTL = 30 * 60   # 小节 ID 基本不变，缓存半小时足够，免得每挂一条都重拉列表


def _section_id(cfg: Dict[str, Any], season_id: int) -> int:
    """从合集列表里取默认小节 ID（/season/section 接口对合集 ID 返回的是空壳）。

    原来是每次 force=True 重拉整个合集分页列表，合集多时要串 6 个请求；
    小节 ID 对同一合集是稳定的，这里按 mid+season_id 缓存，省掉这部分等待。
    """
    key = (str(((cfg.get("account") or {}).get("mid")) or
               ((cfg.get("cookie") or {}).get("dedeuserid")) or "_") + ":" + str(int(season_id)))
    item = _section_cache.get(key) or {}
    if item.get("id") and time.time() - item.get("at", 0) < SECTION_TTL:
        return int(item["id"])

    for s in list_seasons(cfg):
        if s["id"] == int(season_id):
            if s["section_id"]:
                _section_cache[key] = {"at": time.time(), "id": int(s["section_id"])}
                return s["section_id"]
            raise RuntimeError("这个合集没有小节，无法挂载")
    # 列表缓存可能过期（刚新建的合集），强制刷一次再找
    _cache.pop(key.split(":")[0], None)
    for s in list_seasons(cfg, force=True):
        if s["id"] == int(season_id):
            if s["section_id"]:
                _section_cache[key] = {"at": time.time(), "id": int(s["section_id"])}
                return s["section_id"]
            raise RuntimeError("这个合集没有小节，无法挂载")
    raise RuntimeError(f"合集 {season_id} 不在你的账号下")


def aid_from_bvid(bvid: str) -> int:
    if not bvid:
        return 0
    try:
        from bilibili_api.utils.aid_bvid_transformer import bvid2aid
        return int(bvid2aid(bvid))
    except Exception:
        # 兜底：自己算
        table = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
        table = {c: i for i, c in enumerate(table)}
        raw = list(bvid[2:])
        raw[0], raw[1], raw[2], raw[3], raw[4], raw[5], raw[6], raw[7], raw[8] = (
            raw[5], raw[2], raw[4], raw[7], raw[8], raw[6], raw[0], raw[3], raw[1])
        acc = 0
        for ch in raw:
            acc = acc * 58 + table[ch]
        return acc


ARCHIVE_VIEW = "https://member.bilibili.com/x/vupre/web/archive/view"


def archive_state(cfg: Dict[str, Any], aid: int) -> int:
    """回读稿件审核状态：0=开放浏览（已通过），-1=未通过，-6=修复待审。"""
    body = quota.http_json(f"{ARCHIVE_VIEW}?aid={aid}",
                           quota.cookie_string(cfg), referer=REFERER)
    if body.get("code") != 0:
        raise RuntimeError(body.get("message") or f"code={body.get('code')}")
    d = body.get("data") or {}
    a = d.get("archive") or d
    raw = a.get("state")
    return int(raw) if raw is not None else -999


def archive_cid(cfg: Dict[str, Any], aid: int, retries: int = 6) -> int:
    """取稿件第一 P 的 cid（合集挂载接口要 cid）。

    新稿刚提交时主站 view 接口还查不到（返回「啥都木有」），这里自己退避重试，
    避免把「稿件还没入库」误判成接口参数错误。
    """
    body: Dict[str, Any] = {}
    for i in range(retries):
        body = quota.http_json(f"https://api.bilibili.com/x/web-interface/view?aid={int(aid)}",
                               quota.cookie_string(cfg),
                               referer="https://www.bilibili.com/")
        if body.get("code") == 0:
            break
        msg = str(body.get("message") or "")
        if body.get("code") in (-404, -400) or "啥都木有" in msg:
            time.sleep(5 + i * 5)
            continue
        raise RuntimeError(msg or f"code={body.get('code')}")
    if body.get("code") != 0:
        # 主站还没入库：返回 0 让调用方先不带 cid 试一次
        return 0
    cid = (body.get("data") or {}).get("cid")
    if not cid:
        pages = (body.get("data") or {}).get("pages") or []
        cid = pages[0].get("cid") if pages else 0
    if not cid:
        raise RuntimeError("没取到 cid")
    return int(cid)


def verify_archives(cfg: Dict[str, Any], aids: List[int]) -> Dict[str, Any]:
    """预检稿件能否加入合集（B站投稿页同款）。"""
    if not aids:
        return {}
    qs = "&".join(f"aids={int(a)}" for a in aids)
    body = quota.http_json(f"{MEM}/season/section/episode/verify/batch?{qs}",
                           quota.cookie_string(cfg), referer=REFERER)
    if body.get("code") != 0:
        raise RuntimeError(body.get("message") or f"code={body.get('code')}")
    return body.get("data") or {}


def season_of_archive(cfg: Dict[str, Any], aid: int) -> Dict[str, Any]:
    """回读某稿件当前挂在哪个合集；没挂合集时返回 id=0。"""
    body = quota.http_json(f"{MEM}/season/aid?id={aid}",
                           quota.cookie_string(cfg), referer=REFERER)
    code = body.get("code")
    if code != 0:
        # 没挂任何合集时B站返回“啥都木有”这类非 0 码，不能当成登录/接口错误
        if code in (-101, -352, -400):
            raise RuntimeError(body.get("message") or f"code={code}")
        return {"id": 0, "title": ""}
    d = body.get("data") or {}
    s = d.get("season") or d
    return {"id": int(s.get("id") or 0), "title": s.get("title") or ""}


def add_archive(cfg: Dict[str, Any], aid: int, season_id: int, title: str = "",
                cid: int = 0) -> Dict[str, Any]:
    """B站投稿页同款：按小节批量添加稿件（episodes/add），然后回读确认。"""
    if not aid:
        raise ValueError("没有 aid，无法挂合集")
    if not _csrf(cfg):
        raise ValueError("缺少 bili_jct，无法通过合集接口的 csrf 校验")
    section_id = _section_id(cfg, season_id)
    cid = int(cid or 0)

    def _body(cid_value: int) -> Dict[str, Any]:
        ep: Dict[str, Any] = {
            "title": (title or "")[:80],
            "aid": int(aid),
            "charging_pay": 0,
        }
        if cid_value:
            ep["cid"] = int(cid_value)
        return {
            # 这个接口只认 section_id，sectionId 会被当成不存在的小节（返回“啥都木有”）
            "section_id": int(section_id),
            "sectionId": int(section_id),
            "episodes": [ep],
        }

    def _add(body: Dict[str, Any], waits) -> Dict[str, Any]:
        """按给定退避序列重试 episodes/add，返回最后一次响应。"""
        resp: Dict[str, Any] = {}
        for wait in list(waits) + [0]:
            if wait:
                time.sleep(wait)
            resp = _post_json("/season/section/episodes/add", body, cfg)
            if resp.get("code") in (0, 20080):
                return resp
            msg = str(resp.get("message") or "")
            # 只有「稿件还没入库」和「合集编辑过于频繁」值得退避重试
            if not (resp.get("code") == 20111 or "啥都木有" in msg or resp.get("code") == -404):
                return resp
        return resp

    # 先不带 cid 试一次：cid 只是分P标识，省略时B站取第一P。
    # 新稿刚提交时主站查不到它，原逻辑为了拿 cid 白白 sleep 5+10+15 秒（实测每条 31 秒）。
    resp = _add(_body(0), (0, 3, 6))
    if resp.get("code") not in (0, 20080):
        # 还不行说明稿件确实没入库，这才走「退避取 cid 再挂」的原路径
        cid = int(archive_cid(cfg, aid))
        resp = _add(_body(cid), (10, 15, 20, 30, 45))
    if resp.get("code") not in (0, 20080):
        raise RuntimeError(resp.get("message") or f"code={resp.get('code')}")

    got = season_of_archive(cfg, aid)
    if got["id"] != int(season_id):
        raise RuntimeError(f"提交后回读到的合集是 {got['id'] or '空'}，与目标 {season_id} 不一致")
    return {"season_id": got["id"], "season_title": got["title"],
            "section_id": section_id, "cid": cid, "via": "episodes/add"}


def link_archive(cfg: Dict[str, Any], aid: int, season_id: int,
                 title: str = "") -> Dict[str, Any]:
    """把已有稿件挂进合集，并回读确认。"""
    if not aid:
        raise ValueError("没有 aid，无法挂合集")
    if not _csrf(cfg):
        raise ValueError("缺少 bili_jct，无法通过合集接口的 csrf 校验")
    section_id = _section_id(cfg, season_id)
    resp = _post_json("/season/switch", {
        "aid": int(aid),
        "season_id": int(season_id),
        "section_id": int(section_id),
        "title": (title or "")[:80],
    }, cfg)
    if resp.get("code") not in (0, None):
        raise RuntimeError(resp.get("message") or f"code={resp.get('code')}")

    got = season_of_archive(cfg, aid)
    if got["id"] != int(season_id):
        raise RuntimeError(f"提交后回读到的合集是 {got['id'] or '空'}，与目标 {season_id} 不一致")
    return {"season_id": got["id"], "season_title": got["title"], "section_id": section_id}


def _post_form(path: str, form: Dict[str, Any], cfg: Dict[str, Any],
               base: str = MEM, with_csrf: bool = True) -> Dict[str, Any]:
    url = f"{base}{path}"
    if with_csrf:
        url += f"?ts={int(time.time() * 1000)}&csrf={_csrf(cfg)}"
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "User-Agent": quota.UA,
        "Referer": REFERER,
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": quota.cookie_string(cfg),
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    try:
        value, _ = json.JSONDecoder().raw_decode(raw.lstrip())
    except ValueError:
        raise RuntimeError(f"接口返回无法解析：{raw[:200]}")
    return value if isinstance(value, dict) else {}


def upload_cover(cfg: Dict[str, Any], image_path: str) -> str:
    """上传封面图，返回B站图床 URL（新建合集必须要封面）。"""
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"封面不存在：{image_path}")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    ext = os.path.splitext(image_path)[1].lower().lstrip(".") or "jpeg"
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext
    # 这个接口只认 data URI 前缀，裸 base64 会返回 -400
    body = _post_form("/cover/up", {"cover": f"data:image/{mime};base64,{b64}", "csrf": _csrf(cfg)},
                      cfg, base="https://member.bilibili.com/x/vu/web")
    if body.get("code") != 0:
        raise RuntimeError(body.get("message") or f"code={body.get('code')}")
    url = (body.get("data") or {}).get("url") or ""
    if not url:
        raise RuntimeError("封面接口没返回 URL")
    return url


def create_season(cfg: Dict[str, Any], title: str, desc: str = "",
                  cover_path: str = "") -> Dict[str, Any]:
    """新建合集。B站侧标注有人工审核，创建后可能要先审核才会出现在列表里。"""
    title = (title or "").strip()
    if not title:
        raise ValueError("合集名不能为空")
    if not _csrf(cfg):
        raise ValueError("缺少 bili_jct，无法通过合集接口的 csrf 校验")
    if not cover_path:
        raise ValueError("新建合集需要封面图")
    cover = upload_cover(cfg, cover_path)
    body = _post_form("/season/add", {
        "title": title[:80],
        "desc": (desc or "")[:200],
        "cover": cover,
        "season_price": 0,
        "csrf": _csrf(cfg),
    }, cfg)
    if body.get("code") != 0:
        raise RuntimeError(body.get("message") or f"code={body.get('code')}")
    data = body.get("data")
    if isinstance(data, dict):
        sid = int(data.get("id") or 0)
        t = data.get("title") or title
    else:
        # 实测这个接口的 data 直接就是新合集 ID
        sid = int(data or 0)
        t = title
    _cache.clear()  # 列表缓存全部失效，下次重新拉
    return {"id": sid, "title": t, "cover": cover, "raw": body}


def default_cover(title: str, out_dir: str) -> str:
    """没上传封面时生成一张纯色 16:9 封面（新建合集必须带封面）。"""
    from PIL import Image, ImageDraw
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "season-default.png")
    img = Image.new("RGB", (1280, 720), (43, 108, 176))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 560, 1280, 720], fill=(30, 78, 130))
    d.rectangle([80, 80, 88, 640], fill=(255, 255, 255))
    img.save(path, "PNG")
    return path


def delete_season(cfg: Dict[str, Any], season_id: int) -> None:
    body = _post_form("/season/del", {"id": int(season_id), "csrf": _csrf(cfg)}, cfg)
    if body.get("code") != 0:
        raise RuntimeError(body.get("message") or f"code={body.get('code')}")
    _cache.clear()
