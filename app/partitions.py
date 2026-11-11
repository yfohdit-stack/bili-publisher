"""分区（tid）列表：直接取 B站投稿页的 archive/pre 接口，避免用库里的过期白名单。

B站前端的分区树来自 `x/vupre/web/archive/pre` 的 typelist，一级分区和二级
分区都在里面；投稿接口 add/v3 接受的是这里的 tid。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

from . import quota

PRE = "https://member.bilibili.com/x/vupre/web/archive/pre"
REFERER = "https://member.bilibili.com/platform/upload/video/frame"

# 接口不可用时的兜底（与常见分区一致）
DEFAULT: List[Tuple[int, str]] = [
    (21, "日常"), (160, "生活"), (138, "搞笑"), (4, "游戏"),
    (17, "单机游戏"), (171, "电子竞技"), (65, "网络游戏"), (172, "手机游戏"),
    (36, "知识"), (201, "科学科普"), (124, "社科·法律·心理"), (228, "人文历史"),
    (207, "财经商业"), (208, "校园学习"), (209, "职业职场"), (229, "设计·创意"),
    (3, "音乐"), (130, "音乐综合"), (129, "舞蹈"), (1, "动画"),
    (188, "科技数码"), (230, "软件应用"), (231, "计算机技术"), (232, "科工机械"),
    (181, "影视"), (182, "影视杂谈"), (183, "影视剪辑"), (85, "纪录片"),
    (211, "美食制作"), (217, "动物圈"), (5, "娱乐"), (71, "综艺"),
    (155, "时尚"), (202, "资讯"), (203, "热点"), (234, "运动"),
    (235, "篮球"), (236, "足球"), (119, "鬼畜"), (22, "鬼畜调教"),
]

_cache: Dict[str, Any] = {"at": 0.0, "items": []}


def load(cfg: Dict[str, Any], force: bool = False) -> List[Tuple[int, str]]:
    """返回 [(tid, 显示名)]，一级分区原样，二级分区显示为「父/子」。"""
    global _cache
    now = time.time()
    if not force and _cache["at"] and now - _cache["at"] < 600:
        return _cache["items"]
    try:
        body = quota.http_json(PRE, quota.cookie_string(cfg), referer=REFERER)
        if body.get("code") != 0:
            raise RuntimeError(body.get("message") or "pre 接口失败")
        typelist = ((body.get("data") or {}).get("typelist")) or []
        # 只收二级分区：B站投稿接口不接受一级分区，传一级会被悄悄落到「日常」
        items: List[Tuple[int, str]] = []
        for parent in typelist:
            pname = parent.get("name") or ""
            children = parent.get("children") or []
            for child in children:
                cid = int(child.get("id") or 0)
                if cid:
                    items.append((cid, f"{pname}/{child.get('name') or ''}"))
            if not children:
                pid = int(parent.get("id") or 0)
                if pid:
                    items.append((pid, pname))
        if not items:
            raise RuntimeError("typelist 为空")
        _cache.update({"at": now, "items": items})
        return items
    except Exception:
        if _cache["items"]:
            return _cache["items"]
        return list(DEFAULT)


def ids(cfg: Dict[str, Any]) -> List[int]:
    return [t for t, _ in load(cfg)]
