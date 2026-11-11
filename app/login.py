"""B站网页扫码登录。

流程：生成二维码 -> 手机 B站 APP 扫码 -> APP 里点确认 -> 轮询拿到 Cookie 写入配置。
用的是B站网页版扫码登录的公开接口（passport），不涉及任何本地浏览器凭证读取。
"""

import io
import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict

import segno

from . import store

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

# qrcode_key -> 创建时间；只做内存缓存，过期即弃
_TICKETS: Dict[str, Dict[str, Any]] = {}
_TTL = 900


def _get(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        return body, resp.headers


def _gc_tickets() -> None:
    """清掉过期票：_TICKETS 只增不减，长时间反复点扫码会一直攒在内存里。"""
    now = time.time()
    for key, ticket in list(_TICKETS.items()):
        if now - (ticket.get("created") or 0) > _TTL + 60:
            _TICKETS.pop(key, None)


def start() -> Dict[str, Any]:
    """生成二维码，返回 key 与内联 SVG。"""
    _gc_tickets()
    body, _ = _get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
    if body.get("code") != 0:
        raise RuntimeError(body.get("message") or "二维码生成失败")
    data = body.get("data") or {}
    key = data.get("qrcode_key")
    url = data.get("url")
    if not key or not url:
        raise RuntimeError("B站没有返回二维码")

    svg = segno.make(url, error="m").svg_inline(scale=4, border=2)
    _TICKETS[key] = {"created": time.time(), "url": url,
                     "state": "waiting", "message": "等待扫码", "cookie": None}
    # 轮询放到服务端：手机切到B站 APP 后浏览器会挂起定时器，前端轮询会断
    threading.Thread(target=_watch, args=(key,), daemon=True).start()
    return {"key": key, "svg": svg, "expires_in": _TTL}


def qr_png(key: str) -> bytes:
    """把二维码输出成 PNG，方便手机长按保存后用 APP 相册识别。"""
    ticket = _TICKETS.get(key) or {}
    url = ticket.get("url")
    if not url:
        raise RuntimeError("二维码不存在或已刷新")
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=6, border=4)
    return buf.getvalue()


def _collect_cookies(data: Dict[str, Any], headers) -> Dict[str, str]:
    """从跳转链接的 query 和 Set-Cookie 两处一起收集凭证。

    B站网页扫码确认后，较早的版本把 SESSDATA 塞在 data.url 的 query 里，
    现在更多时候只在 Set-Cookie 头里下发，所以两边都要读。
    """
    found: Dict[str, str] = {}

    url = data.get("url") or ""
    for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items():
        if v and v[0]:
            found[k] = urllib.parse.unquote(v[0])

    raw = []
    if hasattr(headers, "get_all"):
        raw = headers.get_all("Set-Cookie") or []
    elif headers.get("Set-Cookie"):
        raw = [headers.get("Set-Cookie")]
    for line in raw:
        head = line.split(";", 1)[0]
        if "=" not in head:
            continue
        name, val = head.split("=", 1)
        name, val = name.strip(), val.strip().strip('"')
        if not val or val == "deleted":
            continue
        # Set-Cookie 优先：它是服务端真正下发的值
        found[name] = urllib.parse.unquote(val)

    return found


def _apply_cookies(found: Dict[str, str]) -> Dict[str, Any]:
    cookie = {
        "sessdata": found.get("SESSDATA", ""),
        "bili_jct": found.get("bili_jct", ""),
        "dedeuserid": found.get("DedeUserID", ""),
        "buvid3": found.get("buvid3", "") or found.get("b_3", ""),
    }
    if not cookie["buvid3"]:
        try:
            body, _ = _get("https://api.bilibili.com/x/frontend/finger/spi")
            cookie["buvid3"] = (body.get("data") or {}).get("b_3", "")
        except Exception:
            pass
    store.add_log(None, f"扫码登录拿到字段：{sorted(found.keys())}")
    store.patch_config({"cookie": cookie})
    return cookie


def _poll_once(key: str) -> Dict[str, Any]:
    """打一次B站的扫码状态接口。"""
    body, headers = _get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
        f"?qrcode_key={urllib.parse.quote(key)}"
    )
    data = body.get("data") or {}
    code = data.get("code")

    if code == 86101:
        return {"state": "waiting", "message": "等待扫码"}
    if code == 86090:
        return {"state": "scanned", "message": "已扫码，请在手机上点确认"}
    if code == 86038:
        return {"state": "expired", "message": "二维码已失效，请刷新"}

    if body.get("code") == 0 and code == 0:
        found = _collect_cookies(data, headers)
        if not (found.get("SESSDATA") and found.get("bili_jct")):
            return {
                "state": "error",
                "message": ("已确认，但没读到 SESSDATA/bili_jct（"
                            f"本次拿到的字段：{sorted(found.keys())}），请刷新二维码重试"),
            }
        cookie = _apply_cookies(found)
        return {"state": "ok", "message": "登录成功", "cookie": cookie}

    return {"state": "waiting", "message": data.get("message") or "等待扫码"}


def _watch(key: str) -> None:
    """后台一直盯到扫码确认或过期，与浏览器是否在前台无关。"""
    while True:
        ticket = _TICKETS.get(key)
        if not ticket:
            return
        if ticket.get("state") in ("ok", "expired", "error"):
            return
        if time.time() - ticket["created"] > _TTL:
            ticket["state"] = "expired"
            ticket["message"] = "二维码已过期，请刷新"
            return
        try:
            result = _poll_once(key)
            ticket["checks"] = ticket.get("checks", 0) + 1
        except Exception as e:
            result = {"state": "waiting", "message": f"网络异常：{e}"}
        ticket["state"] = result["state"]
        ticket["message"] = result["message"]
        if result["state"] == "ok":
            ticket["cookie"] = result.get("cookie")
            return
        if result["state"] in ("expired", "error"):
            return
        time.sleep(1.5)


def poll(key: str) -> Dict[str, Any]:
    """只读服务端缓存里的扫码状态，不再自己打B站接口。"""
    ticket = _TICKETS.get(key)
    if not ticket:
        return {"state": "expired", "message": "二维码已过期或已刷新，请重新生成"}
    return {"state": ticket["state"], "message": ticket["message"]}


def qr_svg_for_test(url: str) -> str:
    """把任意链接渲染成二维码，测试用。"""
    return segno.make(url, error="m").svg_inline(scale=4, border=2)
