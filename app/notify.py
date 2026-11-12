"""投稿状态通知：把队列里的关键事件以 HTTP POST JSON 推给外部机器人。

第一版只做「通用 Webhook」，不引入任何第三方依赖：

* 按地址自动识别企业微信 / 钉钉 / 飞书 / Server酱 / Telegram，
  也可以在设置里手动指定平台；识别不了就发通用 JSON（自建服务、n8n 等）。
* 是否推送、推送哪些事件，完全由 ``config.notify`` 决定；**没填地址就完全
  不触发**，连线程都不会起。
* 本模块只负责「怎么发」，不写任何本地状态；发送本身由 worker 丢到独立
  线程里，异常一律转成返回值，绝不向上抛、绝不阻塞投稿队列。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Tuple

DEFAULT_TIMEOUT = 6.0
EVENTS = ("task", "rate", "quota", "resume")

KIND_LABELS = {
    "auto": "自动识别",
    "wecom": "企业微信机器人",
    "dingtalk": "钉钉机器人",
    "feishu": "飞书机器人",
    "serverchan": "Server酱",
    "telegram": "Telegram Bot",
    "raw": "通用 JSON",
}

_LEVEL_ICON = {"error": "❌", "warn": "⚠️", "success": "✅", "info": "ℹ️"}


def normalize(cfg: Any) -> Dict[str, Any]:
    """把 config 里的 notify 段规整成可直接用的字典，脏值一律落回默认。"""
    raw = cfg.get("notify") if isinstance(cfg, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    events = raw.get("events")
    if not isinstance(events, dict):
        events = {}
    try:
        timeout = float(raw.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return {
        "enabled": raw.get("enabled", True) is not False,
        "url": str(raw.get("url") or "").strip(),
        "kind": str(raw.get("kind") or "auto").strip().lower(),
        "chat_id": str(raw.get("chat_id") or "").strip(),
        "timeout": max(1.0, min(15.0, timeout)),
        "events": {name: events.get(name, True) is not False for name in EVENTS},
    }


def detect_kind(url: str, kind: str = "auto") -> str:
    """自动识别机器人平台；手动指定时以配置为准。"""
    k = (kind or "auto").strip().lower()
    if k != "auto":
        return k if k in KIND_LABELS else "raw"
    u = (url or "").lower()
    if "qyapi.weixin.qq.com" in u:
        return "wecom"
    if "oapi.dingtalk.com" in u:
        return "dingtalk"
    if "open.feishu.cn" in u or "open.larksuite.com" in u:
        return "feishu"
    if "ftqq.com" in u:
        return "serverchan"
    if "api.telegram.org" in u:
        return "telegram"
    return "raw"


def compose(opt: Dict[str, Any], event: str, title: str,
            text: str, level: str) -> Tuple[str, Dict[str, Any]]:
    """按平台构造请求体。返回 (平台, payload)。"""
    kind = detect_kind(opt["url"], opt["kind"])
    icon = _LEVEL_ICON.get(level, "")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    body = f"{icon} 【B站投稿台】{title}\n{text}\n\n时间：{stamp}"
    if kind in ("wecom", "dingtalk"):
        payload = {"msgtype": "text", "text": {"content": body}}
    elif kind == "feishu":
        payload = {"msg_type": "text", "content": {"text": body}}
    elif kind == "serverchan":
        payload = {"title": f"【B站投稿台】{title}", "desp": body}
    elif kind == "telegram":
        payload = {"chat_id": opt.get("chat_id") or "", "text": body,
                   "disable_web_page_preview": True}
    else:
        # 通用 JSON：既给结构化字段，也给可直接转发的整段文本
        payload = {"app": "bili-publisher", "event": event, "level": level,
                   "title": title, "text": text, "content": body,
                   "timestamp": int(time.time())}
    return kind, payload


def send_sync(url: str, payload: Dict[str, Any], timeout: float) -> Tuple[bool, str]:
    """真正发一次 POST。任何异常都转成 (False, 原因)，绝不向上抛。"""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return False, "Webhook 地址不是合法的 http(s) URL"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8",
                     "User-Agent": "bili-publisher-webhook/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            tail = resp.read(2048).decode("utf-8", "replace").strip()
            code = int(getattr(resp, "status", 200) or 200)
            if 200 <= code < 300:
                return True, "HTTP %d%s" % (code, ("：" + tail[:160]) if tail else "")
            return False, "HTTP %d：%s" % (code, tail[:200])
    except Exception as e:  # noqa: BLE001  通知失败绝不能影响主流程
        return False, "%s: %s" % (type(e).__name__, str(e)[:200])


def dispatch(cfg: Any, event: str, title: str, text: str,
             level: str = "info") -> Dict[str, Any]:
    """同步走完整流程：规整配置 → 判断是否该发 → 构造 → 发送。

    返回值仅供测试接口与日志使用；worker 的异步封装不依赖它的返回。
    """
    opt = normalize(cfg)
    if not opt["enabled"]:
        return {"sent": False, "reason": "状态通知已关闭"}
    if not opt["url"]:
        return {"sent": False, "reason": "未配置 Webhook 地址"}
    if not opt["events"].get(event, True):
        return {"sent": False, "reason": "该事件已关闭推送"}
    kind, payload = compose(opt, event, title, text, level)
    ok, detail = send_sync(opt["url"], payload, opt["timeout"])
    return {"sent": ok, "kind": kind, "detail": detail,
            "payload": payload, "reason": "" if ok else detail}
