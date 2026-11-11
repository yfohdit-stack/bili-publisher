"""话题（topic）相关：搜索话题、按分区取可用话题。

投稿接口 x/vu/web/add/v3 支持 topic_id 字段（可选），B站投稿页顶部
`?topic_id=xxx&topic_name=xxx` 就是活动话题入口，选中后随稿件一起提交。
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Any, Dict, List

from . import quota

BASE = "https://member.bilibili.com/x/vupre/web/topic"
REFERER = "https://member.bilibili.com/platform/upload/video/frame"


def search(cfg: Dict[str, Any], keyword: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    """按关键字搜话题；空关键字返回B站推荐的一批。"""
    params = {
        "page_size": int(limit),
        "offset": 0,
        "keywords": keyword or "",
        "t": int(time.time() * 1000),
    }
    url = f"{BASE}/search?{urllib.parse.urlencode(params)}"
    body = quota.http_json(url, quota.cookie_string(cfg), referer=REFERER)
    if body.get("code") != 0:
        raise RuntimeError(body.get("message") or f"code={body.get('code')}")
    topics = (((body.get("data") or {}).get("result") or {}).get("topics")) or []
    out = []
    for t in topics:
        out.append({
            "id": int(t.get("id") or 0),
            "name": t.get("name") or "",
            "desc": t.get("description") or "",
            "mission_id": int(t.get("mission_id") or 0),
        })
    return [t for t in out if t["id"] and t["name"]]


def info(cfg: Dict[str, Any], topic_id: int) -> Dict[str, Any]:
    """按话题 ID 取详情：名称、活动 mission_id。投稿页分享链接里的 topic_id 走这里。"""
    url = f"{BASE}/info?topic_id={int(topic_id)}"
    body = quota.http_json(url, quota.cookie_string(cfg), referer=REFERER)
    d = body.get("data") or {}
    if body.get("code") != 0 or not d.get("id"):
        raise RuntimeError(body.get("message") or f"话题 {topic_id} 查不到")
    return {
        "id": int(d.get("id") or 0),
        "name": d.get("name") or "",
        "desc": d.get("topic_description") or "",
        "mission_id": int(d.get("mission_id") or 0),
        "mission_state": int(d.get("mission_state") or 0),
        "activity_text": d.get("activity_text") or "",
        "is_valid": bool(d.get("is_valid")),
    }


def resolve(cfg: Dict[str, Any], topic_id: int = 0, name: str = "") -> Dict[str, Any]:
    """从分享链接的 topic_id / topic_name 解析出话题；ID 优先，失败再按名字搜。"""
    if topic_id:
        try:
            t = info(cfg, topic_id)
            if t["name"]:
                return t
        except Exception:
            pass
    if name:
        for t in search(cfg, name, 20):
            if topic_id and t["id"] != topic_id:
                continue
            return t
        if topic_id:
            return {"id": int(topic_id), "name": name, "desc": "", "mission_id": 0,
                    "mission_state": 0, "activity_text": "", "is_valid": False}
    raise RuntimeError("链接里没有可识别的话题")


def by_type(cfg: Dict[str, Any], tid: int = 0) -> List[Dict[str, Any]]:
    """按分区取可用话题（活动话题，含 mission_id）。"""
    params = {"t": int(time.time() * 1000)}
    if tid:
        params["type_id"] = int(tid)
    url = f"{BASE}/type?{urllib.parse.urlencode(params)}"
    body = quota.http_json(url, quota.cookie_string(cfg), referer=REFERER)
    if body.get("code") != 0:
        raise RuntimeError(body.get("message") or f"code={body.get('code')}")
    return (body.get("data") or {}).get("topics") or []
