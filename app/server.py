"""Flask 服务：本地投稿台的 HTTP 接口与页面。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Tuple

from flask import g, send_file, Flask, Response, jsonify, request, send_from_directory

from . import (editor, ledger, login, media, notify, partitions, quota, season,
               store, topic, worker)
from .channels import official

BASE_DIR = store.BASE_DIR
WEB_DIR = os.path.join(BASE_DIR, "web")
UPLOAD_DIR = os.path.join(store.DATA_DIR, "uploads")
COVER_DIR = os.path.join(store.DATA_DIR, "covers")

APP_VERSION = "v106"

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = None  # 视频文件大，不限制
app.config["JSON_AS_ASCII"] = False

_account_cache: Dict[str, Any] = {"at": 0.0, "data": {}}
_account_lock = threading.Lock()

# 高频轮询接口的响应体缓存：指纹没变就直接复用上一次序列化好的 JSON。
# 避免每 2.2 秒把 200 条任务 / 200 条日志重新查表、解析、序列化一遍。
_json_cache: Dict[str, Dict[str, Any]] = {}

# 概览统计缓存：
#  - workbench（账号累计播放/点赞/投币/收藏 + 较昨日增量）：B站侧每天中午 12:00 才更新，
#    所以本地也按「统计日」缓存，一天只取一次；每天 12:00 后第一次访问自然换新。
#  - archive（稿件数/审核通过/审核中/未通过）：和投稿行为直接相关，要跟手，
#    缓存 90 秒，且投稿入队时主动作废。
# 缓存按「账号 + Cookie 指纹 + 分组」分键，切号不会串数据；落盘到 data/，
# 重启后当天内不用再打 B站。
_stats_cache: Dict[str, Dict[str, Any]] = {}
_stats_lock = threading.Lock()          # 保护 _stats_cache 的读写
_stats_wlock = threading.Lock()         # 落盘串行锁：避免两次 save 交错写文件
STATS_CACHE_FILE = os.path.join(store.DATA_DIR, "stats_cache.json")
ARCHIVE_TTL = 90.0          # 稿件计数缓存秒数
STATS_DAY_SWITCH_HOUR = 12  # 账号累计每天 12:00 换一份
STATS_PREFETCH_GAP = 1.5    # 预取多个账号时的间隔，避免集中打 B站触发风控
_prefetch_started = False


def _stats_day(now: float | None = None) -> str:
    """统计日：北京时间 12:00 换日（B站账号累计数据次日中午才刷新）。"""
    t = time.time() if now is None else now
    shifted = time.gmtime(t + 8 * 3600 - STATS_DAY_SWITCH_HOUR * 3600)
    return time.strftime("%Y-%m-%d", shifted)


def _stats_fp(sessdata: str) -> str:
    """Cookie 指纹：sha256 前 12 位，够区分账号且不会把 SESSDATA 明文落盘。"""
    if not sessdata:
        return "anon"
    return hashlib.sha256(sessdata.encode("utf-8")).hexdigest()[:12]


def _stats_key(cfg: Dict[str, Any], part: str) -> str:
    ck = cfg.get("cookie") or {}
    sd = str(ck.get("sessdata") or "")
    return "%s|%s|%s" % (cfg.get("active_account") or "", _stats_fp(sd), part)


def _stats_key_ok(key: str) -> bool:
    """只接受当前格式的键：账号|12位hex指纹|分组。

    旧版用「长度+前6位」当指纹（4 段），格式不同即视为过期，载入时丢弃，
    避免旧键与新键两套数据混在一起。
    """
    parts = key.split("|")
    if len(parts) != 3:
        return False
    fp = parts[1]
    if fp == "anon":
        return True
    return len(fp) == 12 and all(c in "0123456789abcdef" for c in fp)


def _stats_entry_fresh(entry: Dict[str, Any] | None, part: str, now: float) -> bool:
    if not entry or not entry.get("ok"):
        return False
    if part == "workbench":
        return entry.get("day") == _stats_day(now)
    return (now - float(entry.get("at") or 0)) <= ARCHIVE_TTL


def _stats_note(part: str, entry: Dict[str, Any] | None, partial: bool = False) -> str:
    if part == "workbench":
        day = (entry or {}).get("day") or _stats_day()
        return "B站账号累计数据每天中午 12:00 汇总一次 · 当前数据日 %s" % day
    base = "进入「全部稿件」时更新 · 90 秒缓存，投稿后自动作废"
    return base + (" · 部分字段本次没取到（显示「—」），稍后自动重试" if partial else "")


def _stats_save() -> None:
    """落盘，进程重启/部署后当天内不必再打 B站。

    序列化在内存锁内完成，写文件走独立的落盘锁 + 唯一临时名：
    即便请求线程与后台预取线程同时触发 save，也不会交错写坏同一个 tmp。
    """
    try:
        os.makedirs(store.DATA_DIR, exist_ok=True)
        with _stats_lock:
            payload = json.dumps(_stats_cache, ensure_ascii=False)
        with _stats_wlock:
            tmp = "%s.%d.%d.tmp" % (STATS_CACHE_FILE, os.getpid(), threading.get_ident())
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, STATS_CACHE_FILE)
            finally:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
    except Exception:
        pass


def _stats_load() -> None:
    try:
        with open(STATS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    with _stats_lock:
        for k, v in data.items():
            if not (isinstance(k, str) and isinstance(v, dict)):
                continue
            if not _stats_key_ok(k):
                continue                     # 旧格式键：丢弃，让新键重新取一次
            d = v.get("data") or {}
            if d.get("partial"):
                continue                     # 不完整的旧缓存，直接丢弃
            items = d.get("items") or {}
            if k.endswith("|archive") and items and all(x == 0 for x in items.values()):
                continue                     # 全 0 基本是风控伪值，不当真
            _stats_cache[k] = v


def _stats_invalidate(part: str | None = None) -> None:
    """投稿入队/稿件状态变化后作废稿件计数（不动每天一份的账号累计）。"""
    cfg = store.load_config()
    parts = [part] if part else ["archive"]
    with _stats_lock:
        for p in parts:
            _stats_cache.pop(_stats_key(cfg, p), None)
    _stats_save()


def _stats_prefetch_accounts() -> None:
    """给所有账号补一份当天的账号累计数据：每个号每天只打 1 次 B站。

    12:00 换日后、或者刚部署重启时（内存缓存空但磁盘有上一份）会自动补。
    非当前账号用各自的 cookie 取，互不影响。账号之间串行 + 间隔，
    避免多个号同时打接口被风控。
    """
    cfg0 = store.load_config()
    accounts = cfg0.get("accounts") or []
    if not accounts:
        return
    now = time.time()
    requested = 0
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        if not (acc.get("cookie") or {}).get("sessdata"):
            continue
        cfg = dict(cfg0)
        cfg["cookie"] = acc.get("cookie") or {}
        cfg["active_account"] = acc.get("id") or ""
        cfg["account"] = {"mid": acc.get("mid") or 0, "uname": acc.get("uname") or "",
                          "face": acc.get("face") or ""}
        key = _stats_key(cfg, "workbench")
        with _stats_lock:
            entry = _stats_cache.get(key)
        if _stats_entry_fresh(entry, "workbench", now):
            continue
        if requested:
            time.sleep(STATS_PREFETCH_GAP)
        requested += 1
        try:
            data = quota.account_stats(cfg, "workbench")
        except Exception:
            continue
        if data.get("ok"):
            with _stats_lock:
                _stats_cache[key] = {"at": now, "day": _stats_day(now), "ok": True, "data": data}
    if requested:
        _stats_save()


def _stats_prefetch_loop() -> None:
    """后台每 10 分钟看一眼：哪个号还没有当天数据就补一次（一天最多 1 次/号）。"""
    # 启动后先错开几秒，把第一次接口机会让给用户首屏请求（两边会命中同一份缓存）
    time.sleep(random.uniform(1.2, 2.4))
    while True:
        try:
            _stats_prefetch_accounts()
        except Exception:
            pass
        time.sleep(600)



def _cached_json(key: str, fingerprint: str, builder) -> Response:
    item = _json_cache.get(key)
    if item and item["fp"] == fingerprint:
        return Response(item["body"], mimetype="application/json")
    body = json.dumps(builder(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _json_cache[key] = {"fp": fingerprint, "body": body}
    return Response(body, mimetype="application/json")

# 常用分区（B站 tid），按使用频率排
# 仅当 archive/pre 接口不可用时由 partitions 模块兜底使用
PARTITIONS: List[Tuple[int, str]] = [
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


def _mask(v: str) -> str:
    if not v:
        return ""
    if len(v) <= 8:
        return v[:2] + "***"
    return v[:4] + "***" + v[-3:]


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "video.mp4")
    name = re.sub(r"[^\w.\-() \u4e00-\u9fff]+", "_", name)
    return name[:120] or "video.mp4"


def _task_dir(task_id: str) -> str:
    d = os.path.join(UPLOAD_DIR, task_id)
    os.makedirs(d, exist_ok=True)
    _track_cleanup(d)
    return d


def _track_cleanup(path: str) -> None:
    """登记本次请求落盘的目录；校验没过时统一清掉，不留半成品。"""
    try:
        g.setdefault("_cleanup_dirs", []).append(path)
    except RuntimeError:
        pass  # 非请求上下文（理论上不会发生）就不登记


# ------------------------------------------------------- 上传前置校验
# 以前 MAX_CONTENT_LENGTH=None 且不看扩展名：任何文件都能落盘，磁盘写满、
# 传个 .txt 进来，都得等到 ffprobe 或B站报错才发现。这里在落盘前就拦掉。

MULTIPART_SLACK = 64 * 1024 * 1024    # multipart 边界与表单字段的体积余量
DISK_RESERVE = 512 * 1024 * 1024      # 磁盘至少留这么多余量，别把盘写满
DEF_MAX_FILE_MB = 8192
DEF_MAX_PARTS = 20


def _public_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """下发给前端的设置：除 cookie 脱敏外原样返回。

    以前这里是手写白名单，新增的 auto_resume / overdue_guard / overdue_minutes
    忘了同步，结果「点保存提示成功、重开设置又变回默认值」。改成全量下发 +
    显式默认值兜底，以后加设置项不用再改两处。
    """
    cookie = cfg.get("cookie") or {}
    wa = cfg.get("web_auth") or {}
    if not isinstance(wa, dict):
        wa = {}
    # web_auth 里的 token / 密码绝不能明文下发（即使页面本身已鉴权）。
    # v105：历史 config.json 里可能还留着 biliup_* 键，一律不再下发；
    # channel 也强制成 official，免得前端读到过期的 "biliup"。
    out: Dict[str, Any] = {k: v for k, v in cfg.items()
                           if k not in ("cookie", "web_auth")
                           and not k.startswith("biliup_")}
    out["channel"] = "official"
    out.update({
        "quota_limit": cfg.get("quota_limit") or 50,
        "quota_hours": cfg.get("quota_hours") or 24,
        "max_attempts": cfg.get("max_attempts") if cfg.get("max_attempts") is not None else 2,
        "quota_strict": bool(cfg.get("quota_strict", True)),
        "auto_resume": bool(cfg.get("auto_resume", True)),
        "overdue_guard": bool(cfg.get("overdue_guard", False)),
        "overdue_minutes": int(cfg.get("overdue_minutes") or 30),
        "max_file_mb": cfg.get("max_file_mb") or DEF_MAX_FILE_MB,
        "max_parts": cfg.get("max_parts") or DEF_MAX_PARTS,
        "upload_line": cfg.get("upload_line") or "bda2",
        "delete_after": cfg.get("delete_after") or "off",
        # v103：状态通知与访问鉴权，统一给前端规整过的默认值
        "notify": notify.normalize(cfg),
        "web_auth": {
            "enabled": bool(wa.get("enabled")),
            "mode": str(wa.get("mode") or "token"),
            "username": str(wa.get("username") or ""),
            "token": _mask(str(wa.get("token") or "")),
            "password": _mask(str(wa.get("password") or "")),
        },
        "cookie": {
            "sessdata": _mask(cookie.get("sessdata", "")),
            "bili_jct": _mask(cookie.get("bili_jct", "")),
            "buvid3": _mask(cookie.get("buvid3", "")),
            "dedeuserid": cookie.get("dedeuserid", ""),
            "set": bool(cookie.get("sessdata") and cookie.get("bili_jct")),
        },
    })
    return out


# ------------------------------------------------------- Web 访问鉴权
# 文档鼓励 --host 0.0.0.0 局域网使用，但旧版所有路由零鉴权：任何人都能读
# Cookie、投稿、改配置。这里加一个可开关的 before_request：
#   * config.web_auth 未启用 / 没填凭据 → 完全不拦截，行为与旧版一致；
#   * token 模式：query ?token= / X-Auth-Token / Bearer / Cookie 任一通过即可，
#     首次通过后写 Cookie，后续静态资源与轮询请求自动带上；
#   * basic 模式：标准 HTTP Basic，交给浏览器原生弹窗。
WEB_AUTH_COOKIE = "wb_auth"
AUTH_EXEMPT_PATHS = {"/api/health"}


def _web_auth(cfg: Dict[str, Any]) -> Dict[str, Any]:
    raw = cfg.get("web_auth") or {}
    if not isinstance(raw, dict):
        raw = {}
    mode = str(raw.get("mode") or "token").strip().lower()
    if mode not in ("token", "basic"):
        mode = "token"
    return {"enabled": bool(raw.get("enabled")), "mode": mode,
            "token": str(raw.get("token") or ""),
            "username": str(raw.get("username") or ""),
            "password": str(raw.get("password") or "")}


def _auth_login_page() -> Response:
    """token 模式下的 401 页面：直接输 token，不必手工拼 URL。

    保留原 query（例如平台的 sharecode），不然点「进入」会把分享码丢掉。
    """
    hidden = "".join(
        '<input type="hidden" name="%s" value="%s">' % (
            html.escape(str(k), quote=True), html.escape(str(v), quote=True))
        for k, v in request.args.items() if k not in ("token", "access_token"))
    body = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>需要访问凭证 · B站投稿台</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#f5f6f8;color:#2b303b;font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
.box{width:420px;max-width:90vw;box-sizing:border-box;background:#fff;
border:1px solid #e3e5ea;border-radius:14px;padding:28px;
box-shadow:0 12px 40px rgba(20,25,40,.08)}
h1{font-size:18px;margin:0 0 8px}p{color:#5b6270;margin:0 0 18px}
input{width:100%%;box-sizing:border-box;padding:10px 12px;border:1px solid #d8dce4;
border-radius:8px;font-size:14px;margin-bottom:12px}
button{width:100%%;padding:10px;border:0;border-radius:8px;background:#fb7299;color:#fff;
font-size:14px;cursor:pointer}.hint{font-size:12px;color:#9098a6;margin:14px 0 0}
</style></head><body><div class="box"><h1>需要访问凭证</h1>
<p>本实例已开启 Web 访问鉴权，请输入设置里配置的访问 Token。</p>
<form method="get" action="%s">%s
<input name="token" type="text" autocomplete="off" placeholder="访问 Token" autofocus>
<button type="submit">进入</button></form>
<p class="hint">也可直接用 ?token=你的token 打开；首次通过后写入 Cookie，之后无需重复输入。</p>
</div></body></html>""" % (html.escape(request.path, quote=True), hidden)
    resp = Response(body, mimetype="text/html; charset=utf-8")
    resp.status_code = 401
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.before_request
def _require_web_auth():
    if request.path in AUTH_EXEMPT_PATHS:
        return None
    wa = _web_auth(store.load_config())
    if not wa["enabled"]:
        return None                      # 未启用：零影响
    if wa["mode"] == "basic":
        if not (wa["username"] and wa["password"]):
            return None                  # 凭据没配齐就不启用，避免把自己锁在外面
        hdr = request.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                raw = base64.b64decode(hdr[6:], validate=True).decode("utf-8")
                user, _, pwd = raw.partition(":")
                if (hmac.compare_digest(user, wa["username"])
                        and hmac.compare_digest(pwd, wa["password"])):
                    return None
            except Exception:
                pass
        resp = jsonify({"ok": False, "error": "需要访问鉴权"})
        resp.status_code = 401
        resp.headers["WWW-Authenticate"] = 'Basic realm="bili-publisher", charset="UTF-8"'
        return resp
    if not wa["token"]:
        return None                      # token 没填就不启用
    supplied = (request.args.get("token") or request.args.get("access_token")
                or request.headers.get("X-Auth-Token") or "")
    hdr = request.headers.get("Authorization", "")
    if hdr.startswith("Bearer "):
        supplied = supplied or hdr[7:]
    supplied = supplied or request.cookies.get(WEB_AUTH_COOKIE, "")
    if supplied and hmac.compare_digest(str(supplied), wa["token"]):
        g._auth_cookie_token = wa["token"]   # 由 after_request 落成 Cookie
        return None
    if (request.path == "/" or request.path.endswith(".html")
            or "text/html" in request.headers.get("Accept", "")):
        return _auth_login_page()
    return jsonify({"ok": False, "error": "访问鉴权失败，请用 ?token=... 重新打开页面"}), 401


@app.after_request
def _persist_auth_cookie(resp):
    tok = getattr(g, "_auth_cookie_token", "")
    if tok:
        try:
            resp.set_cookie(WEB_AUTH_COOKIE, tok, max_age=30 * 86400,
                            httponly=True, samesite="Lax", path="/")
        except Exception:
            pass
    return resp


class UploadRejected(ValueError):
    """上传没通过前置校验；str(e) 是可以直接给用户看的原因。"""


def _limits(cfg: Dict[str, Any]) -> Tuple[int, int]:
    """返回 (单文件字节上限, 分P 数量上限)。设置里填了脏值就用默认值。"""
    try:
        max_mb = int(cfg.get("max_file_mb") or DEF_MAX_FILE_MB)
    except (TypeError, ValueError):
        max_mb = DEF_MAX_FILE_MB
    try:
        max_parts = int(cfg.get("max_parts") or DEF_MAX_PARTS)
    except (TypeError, ValueError):
        max_parts = DEF_MAX_PARTS
    return max(1, max_mb) * 1024 * 1024, max(1, max_parts)


def _disk_free() -> int:
    try:
        return shutil.disk_usage(UPLOAD_DIR).free
    except OSError:
        return 0


def _check_ext(filename: str, allowed: set, kind: str) -> str:
    name = _safe_name(filename or "")
    ext = os.path.splitext(name)[1].lower()
    if ext not in allowed:
        raise UploadRejected(
            f"《{name}》不是受支持的{kind}格式（{ext or '没有扩展名'}）；"
            f"支持：{'、'.join(sorted(allowed))}")
    return name


def _check_videos(files, cfg: Dict[str, Any]) -> int:
    """视频上传前置校验：数量、扩展名、整包体积、磁盘余量。返回单文件字节上限。"""
    if not files:
        raise UploadRejected("没有收到视频文件")
    max_bytes, max_parts = _limits(cfg)
    if len(files) > max_parts:
        raise UploadRejected(f"一个稿件最多 {max_parts} 个分P，本次收到 {len(files)} 个"
                             f"（设置里可调「分P 数量上限」）")
    for f in files:
        _check_ext(f.filename or "", media.VIDEO_EXTS, "视频")
    total = int(request.content_length or 0)
    allowed = max_bytes * len(files)
    # 余量取「固定 64MB」与「额度的 10%」中的小值：
    # 否则把上限调成 1MB 时，64MB 的固定余量会把限制整个淹没
    slack = min(MULTIPART_SLACK, max(1024 * 1024, allowed // 10))
    if total > allowed + slack:
        raise UploadRejected(f"上传体积过大：本次约 {media.human_size(total)}，"
                             f"单文件上限 {max_bytes // (1024 * 1024)} MB"
                             f"（设置里可调「单文件上限」）")
    free = _disk_free()
    if total and free and free < total + DISK_RESERVE:
        raise UploadRejected(f"磁盘空间不够：本次约需 {media.human_size(total)}，"
                             f"当前可用 {media.human_size(free)}，"
                             f"另需保留 {DISK_RESERVE // (1024 * 1024)} MB 余量")
    return max_bytes


def _check_image(fs) -> None:
    """封面之类的图片上传：只校验扩展名，体积随后由 normalize_image 处理。"""
    if fs is not None and (fs.filename or ""):
        _check_ext(fs.filename, media.IMAGE_EXTS, "图片")


def _remove_tree(path: str) -> None:
    """校验失败时把已经落盘的东西清掉，不留半成品。"""
    if not path:
        return
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


@app.errorhandler(UploadRejected)
def _on_upload_rejected(e: UploadRejected):
    """上传校验没过：清掉本次请求已经落盘的文件，返回统一的 JSON 400。

    用 errorhandler 而不是在每个视图里 try/except，是为了让校验可以在
    函数任意位置 raise，不必把整段投稿逻辑缩进一层。
    """
    for p in g.get("_cleanup_dirs", []) or []:
        _remove_tree(p)
    store.add_global_log(f"上传被拦下：{e}", "warn")
    return jsonify({"ok": False, "error": str(e)}), 400


def _public_task(t: Dict[str, Any]) -> Dict[str, Any]:
    files = []
    for i, f in enumerate(t.get("files") or []):
        files.append({
            "name": f.get("name") or os.path.basename(f.get("path", "")),
            "size": f.get("size") or 0,
            "size_text": media.human_size(int(f.get("size") or 0)),
            "duration": f.get("duration") or 0,
            "part_title": f.get("part_title") or "",
            "index": i + 1,
        })
    return {
        "id": t["id"],
        "title": t["title"],
        "desc": t.get("desc") or "",
        "tags": t.get("tags") or [],
        "tid": t.get("tid"),
        "copyright": t.get("copyright"),
        "no_reprint": bool(t.get("no_reprint")),
        "dynamic": t.get("dynamic") or "",
        "status": t.get("status"),
        "stage": t.get("stage") or "",
        "progress": round(float(t.get("progress") or 0), 4),
        "channel": t.get("channel"),
        "season_id": t.get("season_id") or 0,
        "topic_id": t.get("topic_id") or 0,
        "topic_name": t.get("topic_name") or "",
        "mission_id": t.get("mission_id") or 0,
        "no_disturbance": t.get("no_disturbance") or 0,
        "is_only_self": t.get("is_only_self") or 0,
        "speed": t.get("speed") or 0,
        "cleanup": t.get("cleanup") or "",
        "schedule_mode": t.get("schedule_mode") or "bili",
        "account_id": t.get("account_id") or "",
        "publish_at": t.get("publish_at"),
        "files": files,
        "bvid": t.get("bvid") or "",
        "aid": t.get("aid"),
        "attempts": t.get("attempts") or 0,
        "max_attempts": t.get("max_attempts") or 2,
        "error": t.get("error") or "",
        "message": t.get("message") or "",
        "has_cover": bool(t.get("cover_path")),
        "cover_rev": int(t.get("cover_rev") or 0),
        "created_at": t.get("created_at"),
        "updated_at": t.get("updated_at"),
    }


def _account(force: bool = False) -> Dict[str, Any]:
    # 加锁避免多线程（Flask threaded=True）同时穿过缓存各打一次 nav 接口
    with _account_lock:
        now = time.time()
        if not force and _account_cache["data"] and now - _account_cache["at"] < 15:
            return _account_cache["data"]
        data = quota.fetch_account(store.load_config())
        _account_cache.update({"at": now, "data": data})
        return data


# ------------------------------------------------------------------ 页面

@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/api/bootstrap")
def bootstrap():
    warm_account_faces_async()
    cfg = store.load_config()
    acc = _account()
    env = {
        "ffmpeg": media.have_ffmpeg(),
        "official": official.available(),
    }
    return jsonify({
        "config": _public_config(cfg),
        "account": acc,
        "accounts": store.list_accounts(),
        "active_account": cfg.get("active_account") or "",
        "quota": worker.cached_quota(),
        "tasks": [_public_task(t) for t in store.list_tasks()],
        "templates": store.list_templates(),
        "queue": {"paused": worker.is_paused(), "current": worker.current_task_id(),
                  "rate_until": worker.rate_until(), "rate_wait": worker.rate_wait(),
                  "pause_reason": worker.pause_reason(),
                  "quota_resume_at": worker.quota_resume_at(),
                  "quota_ban_until": float(cfg.get("quota_ban_until") or 0.0),
                  "auto_resume": cfg.get("auto_resume", True) is not False},
        "partitions": [{"tid": t, "name": n} for t, n in partitions.load(cfg)],
        "env": env,
        "version": APP_VERSION,
        "counts": store.count_status(),
    })


@app.post("/api/config")
def set_config():
    body = request.get_json(force=True, silent=True) or {}
    patch: Dict[str, Any] = {}
    for k in ("channel", "quota_limit", "quota_hours", "max_attempts",
              "upload_line", "delete_after",
              "quota_strict", "max_file_mb", "max_parts",
              "overdue_guard", "overdue_minutes", "auto_resume", "notify"):
        if k not in body:
            continue
        if k == "notify":
            if isinstance(body[k], dict):
                patch[k] = body[k]
        else:
            patch[k] = body[k]
    # web_auth 的 token / 密码下发时是掩码，保存时必须忽略掩码与空值，
    # 否则只改一个别的设置就会把真凭据覆盖成 "***"，把自己锁在门外。
    if "web_auth" in body and isinstance(body["web_auth"], dict):
        inc = body["web_auth"]
        cur = dict(store.load_config().get("web_auth") or {})
        for k in ("enabled", "mode", "username"):
            if k in inc:
                cur[k] = inc[k]
        for k in ("token", "password"):
            v = str(inc.get(k) or "")
            if v and "***" not in v:
                cur[k] = v
        patch["web_auth"] = cur
    if "defaults" in body and isinstance(body["defaults"], dict):
        patch["defaults"] = body["defaults"]
    if "cookie" in body and isinstance(body["cookie"], dict):
        cur = store.load_config().get("cookie") or {}
        nxt = dict(cur)
        for k in ("sessdata", "bili_jct", "buvid3", "dedeuserid"):
            v = (body["cookie"].get(k) or "").strip()
            if v and "***" not in v:  # 掩码值表示不修改
                nxt[k] = v
        patch["cookie"] = nxt
    store.patch_config(patch)
    if patch:
        store.add_global_log("设置已更新：" + "、".join(patch.keys()))
    # 打开「自动恢复队列」时，若当前是部署/重启留下的保护性暂停（boot），
    # 立即解除，不必等下次重启，改完设置就能生效。
    if body.get("auto_resume") is True and worker.pause_reason() == "boot":
        worker.pause(False)
        worker.wake()
    _account_cache["at"] = 0.0
    acc = _account(force=True)     # 只打一次 nav：旧实现这里前后各查了一遍
    if "cookie" in patch and acc.get("logged_in"):
        # 手填 cookie 也归到账号体系里，避免切换账号后丢失
        try:
            store.upsert_account(store.load_config().get("cookie") or {},
                                 {"mid": acc.get("mid"), "uname": acc.get("uname"),
                                  "face": acc.get("face")})
        except Exception:
            pass
    if acc.get("logged_in"):
        store.patch_config({"account": {"mid": acc["mid"], "uname": acc["uname"], "face": acc["face"]}})
    worker.refresh_quota()
    resp = jsonify({"ok": True, "account": acc})
    # 刚启用 / 更新 token 鉴权时，立刻给这次响应种 Cookie：否则前端保存后紧跟的
    # /api/bootstrap 还没带 token，会 401，用户会以为「保存把页面弄坏了」。
    wa = _web_auth(store.load_config())
    if wa["enabled"] and wa["mode"] == "token" and wa["token"]:
        try:
            resp.set_cookie(WEB_AUTH_COOKIE, wa["token"], max_age=30 * 86400,
                            httponly=True, samesite="Lax", path="/")
        except Exception:
            pass
    return resp


@app.post("/api/notify/test")
def notify_test():
    """发一条测试推送：允许先填地址直接测，不必先点「保存设置」。

    用输入框里的临时值覆盖已保存配置，方便改完立刻验证机器人是否通；
    同步执行（用户主动点击、有 6 秒超时），但失败只返回结果，不抛异常。
    """
    body = request.get_json(force=True, silent=True) or {}
    opt = notify.normalize(store.load_config())
    for k in ("url", "kind", "chat_id"):
        if body.get(k):
            opt[k] = str(body[k])
    if not opt["url"]:
        return jsonify({"ok": False, "error": "请先填写 Webhook 地址"}), 400
    opt["enabled"] = True
    opt["events"] = {name: True for name in notify.EVENTS}
    r = notify.dispatch({"notify": opt}, "task", "测试通知",
                        "这是一条来自 B站投稿台 的测试推送，收到就说明配置正确。", "info")
    return jsonify({"ok": True, "sent": bool(r.get("sent")),
                    "kind": r.get("kind", ""),
                    "detail": r.get("detail") or r.get("reason") or ""})


@app.post("/api/cookie")
def set_cookie():
    """粘贴整段 cookie 字符串，自动拆分字段。"""
    body = request.get_json(force=True, silent=True) or {}
    raw = (body.get("raw") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "cookie 为空"}), 400
    pairs = {}
    for item in raw.split(";"):
        if "=" in item:
            k, v = item.split("=", 1)
            pairs[k.strip()] = v.strip()
    cur = store.load_config().get("cookie") or {}
    nxt = dict(cur)
    mapping = {"SESSDATA": "sessdata", "bili_jct": "bili_jct",
               "buvid3": "buvid3", "DedeUserID": "dedeuserid"}
    for src, dst in mapping.items():
        if src in pairs and pairs[src]:
            nxt[dst] = pairs[src]
    if not (nxt.get("sessdata") and nxt.get("bili_jct")):
        return jsonify({"ok": False, "error": "缺少 SESSDATA 或 bili_jct，请从浏览器完整复制"}), 400
    store.patch_config({"cookie": nxt})
    _account_cache["at"] = 0.0
    acc = _account(force=True)
    if acc.get("logged_in"):
        store.patch_config({"account": {"mid": acc["mid"], "uname": acc["uname"], "face": acc["face"]}})
        # 多账号：扫码/粘贴进来的 cookie 登记成独立账号，并切为当前生效账号
        store.upsert_account(store.load_config().get("cookie") or {},
                             {"mid": acc.get("mid"), "uname": acc.get("uname"), "face": acc.get("face")})
        warm_account_faces_async()
        q = worker.refresh_quota()
        return jsonify({"ok": True, "account": acc, "quota": q,
                        "accounts": store.list_accounts()})
    return jsonify({"ok": False, "error": acc.get("error") or "登录校验失败", "account": acc}), 400


@app.post("/api/login/qr")
def login_qr_start():
    """生成扫码登录二维码。"""
    try:
        return jsonify({"ok": True, **login.start()})
    except Exception as e:
        return jsonify({"ok": False, "error": f"二维码生成失败：{e}"}), 500


@app.get("/api/login/qr/image")
def login_qr_png():
    """二维码 PNG，手机长按保存后用 APP 相册识别。"""
    key = request.args.get("key") or ""
    try:
        png = login.qr_png(key)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/login/qr/<key>")
def login_qr_poll(key: str):
    """轮询扫码状态，确认后自动写入 Cookie。"""
    try:
        result = login.poll(key)
    except Exception as e:
        return jsonify({"ok": False, "state": "error", "message": str(e)}), 500

    if result.get("state") == "ok":
        _account_cache["at"] = 0.0
        acc = _account(force=True)
        if acc.get("logged_in"):
            store.patch_config({"account": {"mid": acc["mid"], "uname": acc["uname"],
                                            "face": acc["face"]}})
            store.upsert_account(store.load_config().get("cookie") or {},
                                 {"mid": acc.get("mid"), "uname": acc.get("uname"), "face": acc.get("face")})
            return jsonify({"ok": True, "state": "ok", "message": "登录成功",
                            "account": acc, "quota": worker.refresh_quota(),
                            "accounts": store.list_accounts()})
        return jsonify({"ok": False, "state": "error",
                        "message": acc.get("error") or "登录后校验失败，请重试"}), 400
    if result.get("state") == "error":
        return jsonify({"ok": False, "state": "error",
                        "message": result.get("message") or "登录失败"})
    return jsonify({"ok": True, **result})


@app.get("/api/account/stats")
def api_account_stats():
    """概览条数据源。?part=workbench|archive，可选 &force=1 强制刷新。

    workbench（账号累计播放/点赞/投币/收藏）：B站侧每天中午 12:00 才更新，
    所以按「统计日」缓存，一天只打一次 B站，12:00 后第一次访问自动换新。
    archive（稿件数/审核通过/审核中/未通过）：和投稿直接相关，90 秒缓存 + 投稿时作废。
    失败时返回 ok:false + error（前端显示占位，不影响投稿），不抛 5xx。
    """
    part = (request.args.get("part") or "workbench").strip()
    if part not in ("workbench", "archive"):
        return jsonify({"ok": False, "error": "part 只能是 workbench 或 archive", "items": {}}), 400
    force = request.args.get("force") == "1"
    cfg = store.load_config()
    key = _stats_key(cfg, part)
    now = time.time()
    if not force:
        with _stats_lock:
            entry = _stats_cache.get(key)
        if _stats_entry_fresh(entry, part, now):
            return jsonify(dict(entry["data"], cached=True, note=_stats_note(part, entry),
                                day=(entry.get("day") or "") if part == "workbench" else ""))
    try:
        data = quota.account_stats(cfg, part)
    except Exception as exc:
        data = {"ok": False, "error": str(exc), "items": {}}
    entry = None
    # partial=True 表示 4 个接口里有失败、数字不完整：不写缓存，下次重新取
    if data.get("ok") and not data.get("partial"):
        entry = {"at": now, "day": _stats_day(now), "ok": True, "data": data}
        with _stats_lock:
            _stats_cache[key] = entry
        _stats_save()
    return jsonify(dict(data, cached=False,
                        note=_stats_note(part, entry, bool(data.get("partial"))),
                        day=(_stats_day(now) if part == "workbench" else "")))


@app.post("/api/quota/refresh")
def refresh_quota():
    q = worker.refresh_quota()
    if q.get("error"):
        store.add_global_log(f"手动刷新 24h 投稿量失败：{q['error']}", "warn")
    else:
        store.add_global_log(f"手动刷新 24h 投稿量：{q.get('count')}/{q.get('limit')}")
    return jsonify(q)


@app.get("/api/topics/info")
def api_topic_info():
    cfg = store.load_config()
    tid = int(request.args.get("id") or 0)
    name = (request.args.get("name") or "").strip()
    if not tid and not name:
        return jsonify({"ok": False, "error": "缺少话题 ID 或名称"}), 400
    try:
        return jsonify({"ok": True, "topic": topic.resolve(cfg, tid, name)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/topics")
def search_topics():
    """按关键字搜B站话题；kw 为空时返回推荐话题。"""
    cfg = store.load_config()
    kw = (request.args.get("kw") or "").strip()
    try:
        return jsonify({"ok": True, "items": topic.search(cfg, kw)})
    except Exception as e:
        return jsonify({"ok": False, "items": [],
                        "error": f"{type(e).__name__}: {e}"})


@app.get("/api/seasons")
def list_seasons():
    """账号下的合集列表，供发布表单选择。"""
    cfg = store.load_config()
    try:
        return jsonify({"ok": True, "items": season.list_seasons(cfg)})
    except Exception as e:
        return jsonify({"ok": False, "items": [],
                        "error": f"{type(e).__name__}: {e}"})


@app.post("/api/seasons")
def create_season():
    """新建合集：标题必填，封面可选（没有就用生成的默认封面）。"""
    cfg = store.load_config()
    data = request.get_json(silent=True) or {}
    title = (request.form.get("title") or data.get("title") or "").strip()
    desc = (request.form.get("desc") or data.get("desc") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "合集名不能为空"})
    cover = request.files.get("cover")
    _check_image(cover)
    path = ""
    try:
        if cover and cover.filename:
            os.makedirs(COVER_DIR, exist_ok=True)
            raw = os.path.join(COVER_DIR, f"season-{int(time.time())}-{_safe_name(cover.filename)}")
            cover.save(raw)
            path = media.normalize_image(raw, os.path.join(COVER_DIR, f"season-{int(time.time())}.jpg")) or raw
        else:
            path = season.default_cover(title, os.path.join(store.DATA_DIR, "season_covers"))
        item = season.create_season(cfg, title, desc, path)
        store.add_log(None, f"新建合集「{title}」")
        return jsonify({"ok": True, "item": item})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


@app.get("/api/accounts")
def accounts_list():
    return jsonify({"ok": True, "items": store.list_accounts(),
                    "active": store.active_account_id()})


@app.post("/api/accounts/switch")
def accounts_switch():
    body = request.get_json(force=True, silent=True) or {}
    aid = (body.get("id") or "").strip()
    try:
        store.set_active_account(aid)
    except KeyError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    _account_cache["at"] = 0.0
    acc = _account(force=True)
    if acc.get("logged_in"):
        store.upsert_account(store.load_config().get("cookie") or {},
                             {"mid": acc.get("mid"), "uname": acc.get("uname"), "face": acc.get("face")})
        store.set_active_account(aid)   # upsert 会置为 active，这里确保仍是目标账号
    warm_account_faces_async()
    store.add_global_log("切换投稿账号：" + (acc.get("uname") or aid))
    return jsonify({"ok": True, "account": acc, "accounts": store.list_accounts(),
                    "active": store.active_account_id(), "quota": worker.refresh_quota()})


@app.post("/api/accounts/remove")
def accounts_remove():
    body = request.get_json(force=True, silent=True) or {}
    aid = (body.get("id") or "").strip()
    if not aid:
        return jsonify({"ok": False, "error": "缺少账号 id"}), 400
    if len(store.list_accounts()) <= 1:
        return jsonify({"ok": False, "error": "至少保留一个账号；要换号请先添加新账号"}), 400
    try:
        store.remove_account(aid)
    except KeyError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    # 顺手清掉这个账号的投稿账本，避免同 id 重建后继承旧记录、把额度算高
    try:
        ledger.drop(aid)
    except Exception:   # noqa: BLE001
        pass
    _account_cache["at"] = 0.0
    worker.refresh_quota()
    return jsonify({"ok": True, "accounts": store.list_accounts(),
                    "active": store.active_account_id(), "account": _account(force=True)})


@app.get("/api/accounts/<aid>/quota")
def account_quota(aid: str):
    """单独查某个账号的 24h 投稿数，不切换当前账号。"""
    return jsonify({"ok": True, "quota": worker.cached_quota(account_id=aid)})


@app.get("/api/account")
def account():
    return jsonify(_account(force=request.args.get("force") == "1"))


# ------------------------------------------------------------------ 任务

@app.after_request
def _no_cache_static(resp):
    """页面与前端静态文件不做浏览器缓存：改版后不会再看到旧版本号/旧 JS。"""
    try:
        p = request.path
        if p == "/" or p.endswith(".html") or p.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store, max-age=0"
            resp.headers["Pragma"] = "no-cache"
    except Exception:
        pass
    return resp


@app.post("/api/publish")
def publish():
    """接收视频文件与投稿参数；mode=single 时每个文件一个稿件，multi 时合并为多分P。"""
    meta_raw = request.form.get("meta") or "{}"
    try:
        meta = json.loads(meta_raw)
    except Exception:
        return jsonify({"ok": False, "error": "meta 不是合法 JSON"}), 400

    uploaded = request.files.getlist("files")
    cover_file = request.files.get("cover")
    mode = (meta.get("mode") or "single").lower()
    cfg = store.load_config()
    defaults = cfg.get("defaults") or {}

    # —— 24h 额度：只做统计预热，绝不拦截入队 ——
    # 「入队排队」和「真正投稿」是两件事：额度是投稿那一刻的关口，不是排队的关口。
    # 旧版在这里额度一满就 409，导致超出上限后连稿件都排不进队列；现在一律照常
    # 入库为 queued，由 worker 取任务时复查额度：满了自动暂停排队、窗口滑出后自动续投。
    # 这里仍查一次额度，是为了预热缓存让 worker 的 fresh_quota 复用，省一轮接口请求。
    account_id = meta.get("account_id") or cfg.get("active_account") or ""
    # 逐个文件提交时，第一个文件真实查一次额度，后面的复用同一轮结果（被投稿完成清缓存后仍会重查）
    if meta.get("quota_live", True) is False:
        q = worker.cached_quota(account_id=account_id or None)
    else:
        q = worker.refresh_quota(account_id=account_id or None)
    quota_full = bool(q.get("logged_in") and not q.get("ok"))

    # —— 校验全部前置：不合法的请求，一个字节都不往磁盘上写 ——
    max_bytes = _check_videos(uploaded, cfg)
    _check_image(cover_file)
    # 合集属于账号：套用了别的账号的模板时，ID 可能不属于当前账号，
    # 现在拦住比投出去再挂载失败更好（省一次整包上传）。取不到合集列表时不拦，免得网络抖动误杀。
    try:
        want_season = int(meta.get("season_id") or 0)
    except (TypeError, ValueError):
        want_season = 0
    if want_season:
        try:
            account_seasons = {s["id"] for s in season.list_seasons(cfg)}
        except Exception:
            account_seasons = None
        if account_seasons is not None and want_season not in account_seasons:
            raise UploadRejected(f"合集 {want_season} 不在当前账号的合集列表里"
                                 f"（刚新建的合集需等B站审核通过后才会出现，请审核后再投）")
    try:
        task_tid = int(meta.get("tid") or defaults.get("tid") or 21)
    except (TypeError, ValueError):
        raise UploadRejected("分区 tid 不是合法数字")
    if task_tid not in set(partitions.ids(cfg)):
        raise UploadRejected(f"分区 {task_tid} 不是可提交的二级分区，请在分区下拉里选具体的二级分区")
    task_tags = [str(t).strip() for t in (meta.get("tags") or defaults.get("tags") or []) if str(t).strip()]
    if not task_tags:
        # B站要求至少一个标签，没填就用分区名兜底
        task_tags = [next((n.split("/")[-1] for t, n in partitions.load(cfg) if t == task_tid), "日常")]

    created: List[Dict[str, Any]] = []

    def build_files(files, task_id: str, titles: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        dest_dir = _task_dir(task_id)
        saved = []
        for idx, f in enumerate(files):
            name = _safe_name(f.filename)
            path = os.path.join(dest_dir, f"{idx + 1:02d}_{name}")
            f.save(path)
            size = os.path.getsize(path)
            if size > max_bytes:
                # 整包体积只能粗判（浏览器可能不给 Content-Length），单文件超限要落盘后才知道
                _remove_tree(path)
                raise UploadRejected(f"《{name}》实际 {media.human_size(size)}，"
                                     f"超过单文件上限 {max_bytes // (1024 * 1024)} MB（设置里可调）")
            saved.append((idx, name, path, size))
        # 探测彼此独立，并行跑；probe_many 保序，out 顺序仍与 idx 一致
        infos = media.probe_many([x[2] for x in saved])
        for (idx, name, path, size), info in zip(saved, infos):
            out.append({
                "path": path,
                "name": name,
                "size": size or info.get("size") or 0,
                "duration": info.get("duration") or 0,
                "part_title": titles.get(idx) or titles.get(str(idx))
                or media.video_title_from_filename(name),
            })
        return out

    cover_path_uploaded = ""
    if cover_file and cover_file.filename:
        tmp_dir = _task_dir("covers-" + uuid.uuid4().hex[:6])
        tmp_path = os.path.join(tmp_dir, _safe_name(cover_file.filename))
        cover_file.save(tmp_path)
        cover_path_uploaded = media.normalize_image(tmp_path, os.path.join(tmp_dir, "cover.png")) or tmp_path

    common = {
        "desc": meta.get("desc") or defaults.get("desc") or "",
        "tags": task_tags,
        "tid": task_tid,
        "copyright": int(meta.get("copyright") or defaults.get("copyright") or 1),
        "no_reprint": bool(meta.get("no_reprint", defaults.get("no_reprint"))),
        "dynamic": meta.get("dynamic") or "",
        "channel": meta.get("channel") or cfg.get("channel") or "auto",
        "season_id": int(meta.get("season_id") or defaults.get("season_id") or 0),
        "topic_id": int(meta.get("topic_id") or defaults.get("topic_id") or 0),
        "topic_name": meta.get("topic_name") or defaults.get("topic_name") or "",
        "mission_id": int(meta.get("mission_id") or defaults.get("mission_id") or 0),
        "no_disturbance": 1 if meta.get("no_disturbance") else 0,
        "is_only_self": 1 if meta.get("is_only_self") else 0,
        "max_attempts": int(meta.get("max_attempts") or cfg.get("max_attempts") or 2),
        "publish_at": float(meta["publish_at"]) if meta.get("publish_at") else None,
        # local=程序本地候着到点自动上传；bili=交给B站定时队列（默认，兼容旧行为）
        "schedule_mode": (meta.get("schedule_mode") or "bili") if meta.get("publish_at") else "bili",
        # 多账号：默认用当前生效账号；前端可显式传 account_id 给某个账号投
        "account_id": (meta.get("account_id") or cfg.get("active_account") or ""),
    }

    if mode == "multi":
        task_id = uuid.uuid4().hex[:12]
        titles = meta.get("part_titles") or {}
        files = build_files(uploaded, task_id, titles)
        cover = cover_path_uploaded
        if not cover and files:
            os.makedirs(COVER_DIR, exist_ok=True)
            cover = media.extract_cover(files[0]["path"], os.path.join(COVER_DIR, f"{task_id}.jpg"),
                                        duration=files[0].get("duration") or 0) or ""
        task = store.create_task({**common, "id": task_id, "title": meta.get("title") or files[0]["part_title"],
                                  "files": files, "cover_path": cover})
        created.append(_public_task(task))
    else:
        pending = []
        for f in uploaded:
            task_id = uuid.uuid4().hex[:12]
            files = build_files([f], task_id, {})
            title = meta.get("title") or files[0]["part_title"]
            if len(uploaded) > 1 and meta.get("title"):
                title = f"{meta['title']} {files[0]['part_title']}"
            pending.append((task_id, files, title))

        # 一次拖入多个视频时，抽帧（ffmpeg）串行会把提交时间线性拉长；
        # 并行跑并限制并发数，且复用落盘时已经探测好的 duration，不再重复 ffprobe
        covers: Dict[str, str] = {}
        if not cover_path_uploaded and pending:
            os.makedirs(COVER_DIR, exist_ok=True)

            def _make_cover(item):
                tid, fl = item[0], item[1]
                return media.extract_cover(
                    fl[0]["path"], os.path.join(COVER_DIR, f"{tid}.jpg"),
                    duration=fl[0].get("duration") or 0) or ""

            if len(pending) == 1:
                covers[pending[0][0]] = _make_cover(pending[0])
            else:
                with ThreadPoolExecutor(max_workers=min(3, len(pending))) as pool:
                    for (tid, _, _), c in zip(pending, pool.map(_make_cover, pending)):
                        covers[tid] = c

        for task_id, files, title in pending:
            cover = cover_path_uploaded or covers.get(task_id, "")
            task = store.create_task({**common, "id": task_id, "title": title,
                                      "files": files, "cover_path": cover})
            created.append(_public_task(task))

    store.add_log(None, f"新建 {len(created)} 个投稿任务" + ("（当前 24h 额度已满，稿件排队等待额度恢复）" if quota_full else ""))
    worker.wake()
    # 投稿入队后稿件数必然要变：作废稿件计数缓存，下次看「全部稿件」立刻重取
    _stats_invalidate("archive")
    return jsonify({"ok": True, "tasks": created,
                    "quota": q, "quota_full": quota_full})


@app.get("/api/tasks")
def tasks():
    q = worker.cached_quota()
    ru, rw = worker.rate_until(), worker.rate_wait()
    pr = worker.pause_reason()
    ra = worker.quota_resume_at()
    cfg = store.load_config()
    ban = float(cfg.get("quota_ban_until") or 0.0)
    ar = cfg.get("auto_resume", True) is not False
    fp = "%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s" % (store.tasks_fingerprint(),
                          "P" if worker.is_paused() else "-",
                          worker.current_task_id(), q.get("checked_at", ""),
                          int(ru), int(rw), pr, int(ra), int(ban), "A" if ar else "-",
                          # 额度数字也算指纹：只有账本变了（新占位 / 新确认）而任务状态没变时，
                          # 也要重建响应，否则界面上的「已投 N/上限」会停在旧数字
                          q.get("count", ""), q.get("pending", ""))
    return _cached_json("tasks", fp, lambda: {
        "tasks": [_public_task(t) for t in store.list_tasks()],
        "queue": {"paused": worker.is_paused(), "current": worker.current_task_id(),
                  "rate_until": ru, "rate_wait": rw,
                  "pause_reason": pr, "quota_resume_at": ra,
                  "quota_ban_until": ban, "auto_resume": ar},
        "counts": store.count_status(),
        "quota": q,
    })


@app.post("/api/tasks/<task_id>/action")
def task_action(task_id: str):
    body = request.get_json(force=True, silent=True) or {}
    action = (body.get("action") or "").strip()
    task = store.get_task(task_id)
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"}), 404

    if action == "cancel":
        if task["status"] in ("success", "failed", "cancelled", "interrupted"):
            return jsonify({"ok": False, "error": "该任务已结束"}), 400
        worker.request_cancel(task_id)
        store.update_task(task_id, status="cancelled", message="已取消")
        store.add_log(task_id, "任务已取消", "warn")
    elif action == "upload_now":
        worker.clear_cancel(task_id)
        # 定时稿提前上传：清掉定时时间，立即按普通稿件投
        if task["status"] != "waiting_schedule":
            return jsonify({"ok": False, "error": "仅等待中的定时任务可以提前上传"}), 400
        store.update_task(task_id, status="queued", publish_at=None, error="",
                          message="提前上传，立即投稿", stage="", progress=0.0,
                          finished_at=None, speed=0)
        store.add_log(task_id, "定时稿件提前上传", "warn")
    elif action == "retry":
        worker.clear_cancel(task_id)
        store.update_task(task_id, status="queued", attempts=0, error="", message="重新排队",
                          progress=0.0, stage="", finished_at=None, speed=0)
        store.add_log(task_id, "重新排队")
    elif action == "delete":
        worker.request_cancel(task_id)
        worker.clear_cancel(task_id)
        store.delete_task(task_id)
        # 额度占位收尾：任务都删了，还没落地的 inflight 占位一起撤掉，
        # 否则它会白占 24h 额度（已确认/结果未知的记录保留，那是真投出去的稿）
        try:
            ledger.release(str((task or {}).get("account_id") or store.active_account_id() or ""),
                           task_id)
        except Exception:   # noqa: BLE001  账本异常不阻断删除
            pass
        d = os.path.join(UPLOAD_DIR, task_id)
        if os.path.isdir(d):
            for f in os.listdir(d):
                try:
                    os.unlink(os.path.join(d, f))
                except Exception:
                    pass
        cover = task.get("cover_path") or ""
        if cover and os.path.isfile(cover) and os.path.abspath(cover).startswith(os.path.abspath(COVER_DIR)):
            try:
                os.unlink(cover)
            except Exception:
                pass
        return jsonify({"ok": True, "task": None})
    elif action == "update":
        worker.clear_cancel(task_id)
        fields = {}
        for k in ("title", "desc", "dynamic", "tid", "copyright", "no_reprint", "channel"):
            if k in body:
                fields[k] = body[k]
        if "tags" in body:
            fields["tags"] = body["tags"]
        if "publish_at" in body:
            fields["publish_at"] = float(body["publish_at"]) if body["publish_at"] else None
            fields["status"] = "queued"
            fields["message"] = ""
        if fields:
            store.update_task(task_id, **fields)
    else:
        return jsonify({"ok": False, "error": f"未知操作 {action}"}), 400

    worker.wake()
    return jsonify({"ok": True, "task": _public_task(store.get_task(task_id))})


@app.post("/api/tasks/clear_done")
def clear_done_tasks():
    """批量清掉已结束的任务（含任务记录与本地视频副本），一次请求代替逐条点删除。"""
    done = {"success", "failed", "cancelled", "blocked", "interrupted"}
    todos = [t for t in store.list_tasks() if (t.get("status") or "") in done]
    removed = 0
    for t in todos:
        tid = t["id"]
        worker.request_cancel(tid)
        worker.clear_cancel(tid)
        store.delete_task(tid)
        shutil.rmtree(os.path.join(UPLOAD_DIR, tid), ignore_errors=True)
        cover = t.get("cover_path") or ""
        if cover and os.path.isfile(cover) and os.path.abspath(cover).startswith(os.path.abspath(COVER_DIR)):
            try:
                os.unlink(cover)
            except OSError:
                pass
        removed += 1
    if removed:
        _json_cache.pop("tasks", None)
        # 任务被清掉后，账本里对应的「未落地占位」也要跟着清，免得白占额度
        try:
            ledger.reconcile([t["id"] for t in store.list_tasks()])
        except Exception:   # noqa: BLE001  账本异常不阻断清理
            pass
        store.add_log(None, f"批量清理已结束任务 {removed} 个")
        worker.wake()
    return jsonify({"ok": True, "removed": removed})


@app.get("/api/tasks/<task_id>/logs")
def task_logs(task_id: str):
    return jsonify({"logs": store.get_logs(task_id)})


@app.post("/api/tasks/<task_id>/cover")
def replace_cover(task_id: str):
    task = store.get_task(task_id)
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    f = request.files.get("cover")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "没有封面文件"}), 400
    _check_image(f)
    os.makedirs(COVER_DIR, exist_ok=True)
    tmp = os.path.join(COVER_DIR, f"raw-{task_id}-{_safe_name(f.filename)}")
    f.save(tmp)
    out = media.normalize_image(tmp, os.path.join(COVER_DIR, f"{task_id}.jpg")) or tmp
    store.update_task(task_id, cover_path=out)
    store.bump_cover_rev(task_id)
    # normalize 成功后原图就是冗余文件，旧实现会一直堆在 covers/ 里
    if out != tmp:
        try:
            os.remove(tmp)
        except OSError:
            pass
    # 已投出的稿件：再把封面同步到B站，否则只换了本地图和队列里的重试用图
    pushed, push_err = False, ""
    if task.get("status") == "success":
        try:
            aid = int(task.get("aid") or 0) or season.aid_from_bvid(task.get("bvid") or "")
            if aid:
                editor.set_cover(store.account_cfg(task.get("account_id")), aid, out)
                pushed = True
                store.add_global_log(f"稿件 {task.get('bvid') or aid} 已更换封面")
            else:
                push_err = "任务没有 BV 号，无法同步到B站"
        except Exception as exc:
            push_err = f"{type(exc).__name__}: {exc}"
    return jsonify({"ok": True, "pushed": pushed, "push_error": push_err,
                    "cover": f"/api/tasks/{task_id}/cover?v={int(store.get_task(task_id).get('cover_rev') or 0)}"})


@app.get("/api/tasks/<task_id>/cover")
def get_cover(task_id: str):
    task = store.get_task(task_id)
    if not task or not task.get("cover_path") or not os.path.isfile(task["cover_path"]):
        return ("", 404)
    return send_from_directory(os.path.dirname(task["cover_path"]), os.path.basename(task["cover_path"]),
                               max_age=0)


@app.post("/api/queue/pause")
def queue_pause():
    body = request.get_json(force=True, silent=True) or {}
    paused = bool(body.get("paused"))
    if not paused:
        # 人工点「继续队列」= 强制放行：清掉连续限流计数与冷却，
        # 否则刚点继续就又被 _blocked_accounts 挡回去。
        worker.reset_rate_hits()
    worker.pause(paused)
    worker.wake()
    store.add_log(None, "队列已暂停" if paused else "队列已恢复")
    return jsonify({"ok": True, "paused": worker.is_paused()})


@app.post("/api/storage/cleanup")
def storage_cleanup():
    """一键清理成功任务的落盘副本与孤儿文件（任务记录和 BV 号保留）。"""
    r = worker.cleanup_now()
    store.add_log(None, f"已清理磁盘：释放 {r['bytes_freed']} 字节，"
                       f"任务文件 {r['files']} 个，孤儿文件 {r['orphan_files']} 个")
    return jsonify({"ok": True, **r})


COVER_HOSTS = ("hdslb.com", "biliimg.com", "bilivideo.com")


def _image_cache_dir() -> str:
    d = os.path.join(store.DATA_DIR, "cache", "covers")
    os.makedirs(d, exist_ok=True)
    return d


def _image_cache_path(u: str) -> str:
    import hashlib
    return os.path.join(_image_cache_dir(), hashlib.sha1(u.encode()).hexdigest() + ".img")


def _prune_image_cache(d: str, max_files: int = 800, max_age: float = 30 * 86400) -> None:
    """图床缓存按 URL 哈希命名、永不清理会无限增长；超过阈值时删最老的一批。"""
    try:
        entries = []
        with os.scandir(d) as it:
            for e in it:
                if e.is_file():
                    entries.append((e.stat().st_mtime, e.path))
        now = time.time()
        stale = [p for m, p in entries if now - m > max_age]
        if len(entries) > max_files:
            entries.sort()
            stale += [p for _, p in entries[:len(entries) - max_files]]
        for p in set(stale):
            try:
                os.remove(p)
                os.remove(p + ".type")
            except OSError:
                try:
                    os.remove(p)
                except OSError:
                    pass
    except OSError:
        pass


def cache_image(u: str) -> str:
    """把B站图床上的图（封面/头像）抓下来存本地，返回本地缓存路径；失败抛异常。"""
    path = _image_cache_path(u)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    _prune_image_cache(os.path.dirname(path))
    req = urllib.request.Request(u, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        body = resp.read()
        ctype = resp.headers.get("Content-Type") or "image/jpeg"
    with open(path, "wb") as fh:
        fh.write(body)
    with open(path + ".type", "w") as fh:
        fh.write(ctype)
    return path


_face_warm_lock = threading.Lock()
_face_warm_running = False


def warm_account_faces_async() -> None:
    # 去重：上一轮预热还没跑完就不再起新线程，避免每次 bootstrap 重复抓
    global _face_warm_running
    with _face_warm_lock:
        if _face_warm_running:
            return
        _face_warm_running = True

    def _run():
        global _face_warm_running
        try:
            warm_account_faces()
        finally:
            with _face_warm_lock:
                _face_warm_running = False

    threading.Thread(target=_run, daemon=True).start()


def warm_account_faces() -> None:
    """预缓存所有账号头像，界面就不用每次现抓（失败静默，不影响启动）。"""
    try:
        for a in store.list_accounts():
            if a.get("face"):
                try:
                    cache_image(a["face"])
                except Exception:
                    pass
    except Exception:
        pass


@app.get("/api/proxy/cover")
def proxy_cover():
    """B站图床防盗链：服务端带 B站 Referer 取图并缓存，浏览器直接引用本站地址。"""
    u = (request.args.get("u") or "").strip()
    if not u.startswith(("http://", "https://")):
        return "", 400
    host = urllib.parse.urlparse(u).hostname or ""
    if not any(host == h or host.endswith("." + h) for h in COVER_HOSTS):
        return "", 403
    try:
        cache_image(u)
    except Exception as exc:
        return str(exc), 502
    path = _image_cache_path(u)
    ctype = "image/jpeg"
    if os.path.isfile(path + ".type"):
        ctype = open(path + ".type").read().strip() or ctype
    resp = send_file(path, mimetype=ctype, max_age=86400)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/api/archives")
def api_archives():
    """账号下全部B站稿件（含审核中/未通过），并标出哪些是本站投的。

    ?status=pubed|is_pubing|not_pubed 可按审核状态筛选（概览条点击下钻用）。
    """
    cfg = store.load_config()
    pn = max(1, int(request.args.get("page") or 1))
    ps = min(30, max(5, int(request.args.get("ps") or 10)))
    status = (request.args.get("status") or "").strip()
    if status and status not in quota.ARCHIVE_STATUS:
        return jsonify({"ok": False, "error": "status 参数不合法", "items": []}), 400
    try:
        data = quota.list_archives(cfg, pn=pn, ps=ps, status=status or None)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    mine = {t.get("bvid"): t for t in store.list_tasks() if t.get("bvid")}
    for it in data["items"]:
        t = mine.get(it.get("bvid"))
        it["from_app"] = bool(t)
        it["task_id"] = t.get("id") if t else ""
    return jsonify({"ok": True, **data})


@app.get("/api/archive/<int:aid>")
def api_archive_detail(aid: int):
    """稿件详情（编辑表单用）。"""
    cfg = store.load_config()
    try:
        return jsonify({"ok": True, "archive": editor.detail(cfg, aid)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.post("/api/archive/<int:aid>/edit")
def api_archive_edit(aid: int):
    """改标题/简介/标签/分区/可见性。"""
    cfg = store.load_config()
    body = request.get_json(force=True, silent=True) or {}
    fields = {}
    if "title" in body:
        fields["title"] = (body.get("title") or "").strip()[:80]
        if not fields["title"]:
            return jsonify({"ok": False, "error": "标题不能为空"}), 400
    if "desc" in body:
        fields["desc"] = body.get("desc") or ""
    if "tag" in body:
        tags = [str(t).strip() for t in (body.get("tag") or []) if str(t).strip()][:10]
        if not tags:
            return jsonify({"ok": False, "error": "至少保留一个标签"}), 400
        fields["tag"] = ",".join(tags)
    if body.get("tid"):
        fields["tid"] = int(body["tid"])
        if fields["tid"] not in set(partitions.ids(cfg)):
            return jsonify({"ok": False, "error": "分区不是可提交的二级分区"}), 400
    if "is_only_self" in body:
        fields["is_only_self"] = 1 if body.get("is_only_self") else 0
    if not fields:
        return jsonify({"ok": False, "error": "没有要保存的改动"}), 400
    try:
        after = editor.update(cfg, aid, **fields)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    try:
        store.add_global_log(f"稿件 {after['bvid'] or aid} 已更新：{'、'.join(fields.keys())}")
    except Exception:
        pass
    return jsonify({"ok": True, "archive": after})


@app.post("/api/archive/<int:aid>/cover")
def api_archive_cover(aid: int):
    """换封面：先上传图到B站图床，再用 edit 落库。"""
    cfg = store.load_config()
    f = request.files.get("cover")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "没有收到封面文件"}), 400
    _check_image(f)
    tmp = os.path.join(tempfile.gettempdir(), f"cover-{uuid.uuid4().hex[:8]}.png")
    f.save(tmp)
    try:
        normalized = media.normalize_image(tmp, tmp) or tmp
        after = editor.set_cover(cfg, aid, normalized)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    finally:
        for p in (tmp,):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
    store.add_global_log(f"稿件 {after['bvid'] or aid} 已更换封面")
    return jsonify({"ok": True, "archive": after})


@app.post("/api/archive/<int:aid>/video")
def api_archive_video(aid: int):
    """更换视频（replace）或追加分P（append）。"""
    cfg = store.load_config()
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "没有收到视频文件"}), 400
    _check_videos([f], cfg)   # 换视频/追加分P 走同一套扩展名、体积、磁盘校验
    mode = (request.form.get("mode") or "append").lower()
    title = (request.form.get("title") or "").strip()
    tmp = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex[:8]}_{_safe_name(f.filename)}")
    f.save(tmp)
    try:
        result = editor.change_video(cfg, aid, tmp, mode, title)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    finally:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
    action = "更换视频" if mode == "replace" else "追加分P"
    store.add_global_log(f"稿件 {result['detail']['bvid'] or aid} 已{action}")
    return jsonify({"ok": True, **result})


@app.get("/api/logs")
def global_logs():
    return _cached_json("logs", store.logs_fingerprint(),
                        lambda: {"logs": store.get_all_logs(200)})


@app.delete("/api/logs")
def delete_global_logs():
    store.clear_logs()
    _json_cache.pop("logs", None)
    store.add_global_log("日志已手动清空")
    return jsonify({"ok": True, "logs": store.get_all_logs(200)})


# ------------------------------------------------------------------ 模板

@app.post("/api/templates")
def add_template():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    payload = body.get("payload") or {}
    if not name:
        return jsonify({"ok": False, "error": "模板需要名字"}), 400
    tpl = store.save_template(name[:40], payload)
    return jsonify({"ok": True, "template": tpl})


@app.put("/api/templates/<int:template_id>")
def update_template(template_id: int):
    """覆盖保存已有模板：参数整包替换，名字留空则沿用原名。"""
    body = request.get_json(force=True, silent=True) or {}
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "payload 需要是对象"}), 400
    try:
        tpl = store.update_template(template_id, (body.get("name") or "").strip()[:40], payload)
    except KeyError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    return jsonify({"ok": True, "template": tpl})


@app.delete("/api/templates/<int:template_id>")
def remove_template(template_id: int):
    store.delete_template(template_id)
    return jsonify({"ok": True})


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "ts": time.time()})


def create_app() -> Flask:
    store.init_db()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(COVER_DIR, exist_ok=True)
    _stats_load()
    global _prefetch_started
    if not _prefetch_started:
        _prefetch_started = True
        t = threading.Thread(target=_stats_prefetch_loop, name="stats-prefetch", daemon=True)
        t.start()
    worker.start()
    return app
