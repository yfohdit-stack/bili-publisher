"""稿件编辑：标题/简介/标签/分区/封面/可见性、更换视频、追加分P。

B站网页端「稿件编辑」用的就是 /x/vu/web/edit，提交时要带上 aid、new_web_edit=1
以及完整 videos 列表（分P 的 filename + cid），只改标题也必须原样回传分P信息，
否则会把分P 弄丢。这里先读 /x/vupre/web/archive/view 拿到现状，再在现状上覆盖改动。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import urllib.request
from typing import Any, Dict, List

from . import quota, season

VIEW = "https://member.bilibili.com/x/vupre/web/archive/view"
EDIT = "https://member.bilibili.com/x/vu/web/edit"
REFERER = "https://member.bilibili.com/platform/upload-manager/article"


def _get(url: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={
        "User-Agent": quota.UA,
        "Cookie": quota.cookie_string(cfg),
        "Referer": REFERER,
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _post_edit(body: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    csrf = ((cfg.get("cookie") or {}).get("bili_jct") or "")
    if not csrf:
        raise ValueError("缺少 bili_jct，无法通过编辑接口的 csrf 校验")
    url = f"{EDIT}?csrf={csrf}&t={int(time.time() * 1000)}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "User-Agent": quota.UA,
        "Cookie": quota.cookie_string(cfg),
        "Referer": REFERER,
        "Origin": "https://member.bilibili.com",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=40) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError(f"编辑接口返回无法解析：{raw[:200]}")


def detail(cfg: Dict[str, Any], aid: int) -> Dict[str, Any]:
    body = _get(f"{VIEW}?aid={int(aid)}", cfg)
    if body.get("code") != 0:
        raise RuntimeError(body.get("message") or f"code={body.get('code')}")
    d = body.get("data") or {}
    a = d.get("archive") or {}
    videos = d.get("videos") or []
    return {
        "aid": int(a.get("aid") or aid),
        "bvid": a.get("bvid") or "",
        "title": a.get("title") or "",
        "desc": a.get("desc") or "",
        "tag": a.get("tag") or "",
        "tid": int(a.get("tid") or 21),
        "copyright": int(a.get("copyright") or 1),
        "cover": a.get("cover") or "",
        "is_only_self": int(a.get("is_only_self") or 0),
        "no_reprint": int(a.get("no_reprint") or 0),
        "dynamic": a.get("dynamic") or "",
        "state": int(a.get("state") if a.get("state") is not None else -999),
        "mission_id": int(a.get("mission_id") or 0),
        "topic_id": int(a.get("topic_id") or 0),
        "topic_name": a.get("topic_name") or "",
        "videos": [{
            "cid": int(v.get("cid") or 0),
            "filename": v.get("filename") or "",
            "title": v.get("title") or "",
        } for v in videos],
    }


def _videos_of(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"filename": v["filename"], "title": v["title"], "desc": "", "cid": v["cid"]}
            for v in (d.get("videos") or [])]


def _payload(d: Dict[str, Any], *, title=None, desc=None, tag=None, tid=None,
             cover=None, is_only_self=None, videos=None) -> Dict[str, Any]:
    return {
        "aid": d["aid"],
        "title": (title if title is not None else d["title"])[:80],
        "copyright": int(d.get("copyright") or 1),
        "tid": int(tid if tid is not None else (d.get("tid") or 21)),
        "tag": tag if tag is not None else (d.get("tag") or "日常"),
        "desc": (desc if desc is not None else (d.get("desc") or ""))[:2000],
        "dynamic": d.get("dynamic") or "",
        "videos": videos if videos is not None else _videos_of(d),
        "cover": cover if cover is not None else d["cover"],
        "is_only_self": int(is_only_self if is_only_self is not None else (d.get("is_only_self") or 0)),
        "space_hidden": 2,
        "no_reprint": int(d.get("no_reprint") or 0),
        "new_web_edit": 1,
        "interactive": 0,
        "recreate": -1,
        "watermark": {"state": 0},
    }


def update(cfg: Dict[str, Any], aid: int, **fields) -> Dict[str, Any]:
    """改标题/简介/标签/分区/封面/可见性；回读核对。"""
    before = detail(cfg, aid)
    body = _payload(before, **fields)
    resp = _post_edit(body, cfg)
    if resp.get("code") != 0:
        raise RuntimeError(resp.get("message") or f"code={resp.get('code')}")
    after = detail(cfg, aid)
    checks = {
        "title": body["title"], "desc": body["desc"], "tag": body["tag"],
        "tid": body["tid"], "is_only_self": int(body["is_only_self"]),
    }
    bad = []
    for k, want in checks.items():
        got = after.get(k)
        if k == "tag":
            got = got or ""
            if str(want).replace("，", ",") != str(got).replace("，", ","):
                bad.append(f"{k}: {want} -> {got}")
        elif k in ("tid", "is_only_self") and int(got or 0) != int(want or 0):
            bad.append(f"{k}: {want} -> {got}")
        elif k in ("title", "desc") and str(got) != str(want):
            bad.append(f"{k}: {want} -> {got}")
    if bad:
        raise RuntimeError("提交成功但回读不一致：" + "；".join(bad))
    return after


def set_cover(cfg: Dict[str, Any], aid: int, image_path: str) -> Dict[str, Any]:
    url = season.upload_cover(cfg, image_path)
    return update(cfg, aid, cover=url)


def change_video(cfg: Dict[str, Any], aid: int, video_path: str, mode: str,
                 title: str = "") -> Dict[str, Any]:
    """mode=replace 换掉 P1；mode=append 追加分P。"""
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"视频不存在：{video_path}")
    before = detail(cfg, aid)
    if mode not in ("replace", "append"):
        raise ValueError("mode 只能是 replace 或 append")
    if mode == "replace" and len(before["videos"]) > 1:
        raise ValueError("多分P稿件不支持整体更换视频，请用追加分P")
    meta_title = (title or (before["videos"][0]["title"] if before["videos"] else before["title"])
                  or before["title"])[:80]
    uploaded = _upload_page(cfg, video_path, meta_title)
    entry = {"filename": uploaded["filename"], "title": meta_title, "desc": "", "cid": uploaded["cid"]}
    videos = [entry] if mode == "replace" else (_videos_of(before) + [entry])
    result = update(cfg, aid, videos=videos)
    return {"detail": result, "uploaded": uploaded, "mode": mode}


def _dummy_cover() -> str:
    from PIL import Image
    path = os.path.join(tempfile.gettempdir(), "bili-edit-cover.png")
    if not os.path.isfile(path):
        Image.new("RGB", (1146, 717), (20, 20, 20)).save(path, "PNG")
    return path


async def _upload_async(cfg: Dict[str, Any], path: str, title: str) -> Dict[str, Any]:
    from bilibili_api import video_uploader as vu

    cred = quota.credential_from_cfg(cfg)
    cover = _dummy_cover()
    stage_page = vu.VideoUploaderPage(path=path, title=title[:80])
    meta = vu.VideoMeta(tid=21, title=title[:80], desc="", cover=cover, tags=["日常"])
    line_name = str(cfg.get("upload_line") or "").strip().lower()
    line = getattr(vu.Lines, line_name.upper(), None) if line_name and line_name != "auto" else None
    uploader = vu.VideoUploader([stage_page], meta, cred, cover=cover, line=line)
    uploader.line = await vu._choose_line(uploader.line)
    return await uploader._upload_page(stage_page)


def _upload_page(cfg: Dict[str, Any], path: str, title: str) -> Dict[str, Any]:
    """只上传分P、不提交稿件，返回 filename/cid 供 edit 使用。"""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_upload_async(cfg, path, title))
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()
