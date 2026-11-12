#!/usr/bin/env python3.11
"""v103 验证：Webhook 通知 / Web 访问鉴权 / 文案与定时阈值。

单独进程运行；配置与数据库都重定向到临时目录，绝不碰线上 data/。
    python3.11 tests/verify_v103.py
"""
from __future__ import annotations

import base64
import http.server
import inspect
import json
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import notify, server, store, worker  # noqa: E402

ok = 0
fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print("  PASS", name)
    else:
        fail += 1
        print("  FAIL", name, extra)


# ---------------------------------------------------------------- 通知模块
print("\n[1] 通知模块：识别 / payload / 不触发条件")
n = notify.normalize({})
check("默认 url 为空", n["url"] == "")
check("默认四类事件全开", all(n["events"].values()), str(n["events"]))
check("脏值 timeout 落回默认", notify.normalize({"notify": {"timeout": "abc"}})["timeout"] == 6.0)

for url, want in [
    ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", "wecom"),
    ("https://oapi.dingtalk.com/robot/send?access_token=x", "dingtalk"),
    ("https://open.feishu.cn/open-apis/bot/v2/hook/x", "feishu"),
    ("https://sctapi.ftqq.com/KEY.send", "serverchan"),
    ("https://api.telegram.org/botTOKEN/sendMessage", "telegram"),
    ("https://example.com/hook", "raw"),
]:
    check("自动识别 %s" % want, notify.detect_kind(url) == want)
check("手动指定优先", notify.detect_kind("https://example.com/h", "feishu") == "feishu")

opt = notify.normalize({"notify": {"url": "https://qyapi.weixin.qq.com/x", "chat_id": "-100"}})
k, p = notify.compose(opt, "task", "标题", "正文", "error")
check("企业微信/钉钉 payload", k == "wecom" and p["msgtype"] == "text"
      and "正文" in p["text"]["content"] and p["text"]["content"].startswith("❌"))
k, p = notify.compose({**opt, "kind": "feishu"}, "rate", "限流", "正文", "warn")
check("飞书 payload", k == "feishu" and p["msg_type"] == "text" and "正文" in p["content"]["text"])
k, p = notify.compose({**opt, "kind": "serverchan"}, "quota", "额度", "正文", "warn")
check("Server酱 payload", k == "serverchan" and p["title"] and "正文" in p["desp"])
k, p = notify.compose({**opt, "kind": "telegram"}, "resume", "恢复", "正文", "success")
check("Telegram payload", k == "telegram" and p["chat_id"] == "-100" and "正文" in p["text"])
k, p = notify.compose({**opt, "kind": "raw"}, "task", "标题", "正文", "info")
check("通用 JSON 结构化字段", k == "raw" and p["event"] == "task" and p["level"] == "info"
      and p["title"] == "标题" and p["text"] == "正文" and "timestamp" in p)

sent, why = notify.send_sync("file:///etc/passwd", {}, 2)
check("非法 scheme 被拒且不抛", not sent and "http" in why, why)
r = notify.dispatch({"notify": {}}, "task", "t", "x")
check("空地址完全不触发", r["sent"] is False and r["reason"] == "未配置 Webhook 地址")
r = notify.dispatch({"notify": {"url": "https://example.com/h", "enabled": False}}, "task", "t", "x")
check("总开关关闭不触发", r["sent"] is False and r["reason"] == "状态通知已关闭")
r = notify.dispatch({"notify": {"url": "https://example.com/h",
                                "events": {"task": False}}}, "task", "t", "x")
check("关闭的事件不触发", r["sent"] is False and "事件" in r["reason"])


# ---------------------------------------------------------------- 端到端发送
print("\n[2] 端到端：真实 HTTP POST / 异步不阻塞 / 失败不抛")
received = []


class Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        try:
            received.append(json.loads(body.decode("utf-8")))
        except Exception:
            received.append({"_raw": body.decode("utf-8", "replace")})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"errcode":0}')

    def log_message(self, *args):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), Hook)
threading.Thread(target=srv.serve_forever, daemon=True).start()
hook_url = "http://127.0.0.1:%d/hook" % srv.server_address[1]

# 配置 / 数据库全部隔离到临时目录
_tmp = tempfile.mkdtemp(prefix="v103-verify-")
_orig = (store.DATA_DIR, store.CONFIG_PATH, store.DB_PATH, store._dirs_ready_for)
store.DATA_DIR = _tmp
store.CONFIG_PATH = os.path.join(_tmp, "config.json")
store.DB_PATH = os.path.join(_tmp, "publish.db")
store._dirs_ready_for = None
store._cfg_cache.update({"sig": None, "data": None})
store.init_db()

# 避免鉴权测试里的 /api/config 打到 B站
_orig_account, _orig_quota = server._account, server.worker.refresh_quota
server._account = lambda force=False: {"logged_in": False}
server.worker.refresh_quota = lambda *a, **k: None


def set_notify(**kw):
    store.patch_config({"notify": dict(kw)})


def wait_for(n, secs=3.0):
    end = time.time() + secs
    while time.time() < end and len(received) < n:
        time.sleep(0.02)
    return len(received) >= n


set_notify(enabled=True, url=hook_url, kind="raw")
worker._summary({"title": "测试稿件"}, "投稿成功", "success")
check("投稿结果真实推送", wait_for(1) and received[0].get("event") == "task"
      and received[0].get("text") == "投稿成功", str(received[:1]))

worker._notify("rate", "投稿被 B站 接口限流", "连续 1 次触发限流", "warn")
check("限流事件真实推送", wait_for(2) and received[1].get("event") == "rate")

# 去重：同一事件文案 20 秒内不重复刷屏
before = len(received)
worker._notify("rate", "投稿被 B站 接口限流", "连续 1 次触发限流", "warn")
time.sleep(0.3)
check("相同事件短期去重", len(received) == before, str(len(received)))

set_notify(enabled=True, url="", kind="raw")
before = len(received)
worker._summary({"title": "另一篇"}, "投稿失败", "error")
time.sleep(0.3)
check("清空地址后完全不触发", len(received) == before)

set_notify(enabled=True, url=hook_url, kind="raw")
before = len(received)
worker._summary({"title": "第三篇"}, "限流冷却", "warn", push=False)
time.sleep(0.3)
check("_summary(push=False) 不重复推送", len(received) == before)

set_notify(enabled=True, url="http://127.0.0.1:1/nope", kind="raw")
t0 = time.time()
worker._summary({"title": "第四篇"}, "投稿失败", "error")
check("坏地址不阻塞主流程", time.time() - t0 < 0.5, "%.3fs" % (time.time() - t0))
try:
    worker._notify_send(store.load_config(), "task", "t", "x", "error")
    check("_notify_send 异常不外抛", True)
except Exception as e:  # noqa: BLE001
    check("_notify_send 异常不外抛", False, repr(e))

for fn in (worker._summary, worker.pause_for_quota,
           worker._maybe_resume_quota, worker._rate_limit_backoff):
    src = inspect.getsource(fn)
    check("%s 内含 _notify 调用点" % fn.__name__, "_notify(" in src)


# ---------------------------------------------------------------- 访问鉴权
print("\n[3] Web 访问鉴权：默认关闭 / token / basic / 脱敏")
store.patch_config({"web_auth": {"enabled": False, "mode": "token", "token": ""}})
with server.app.test_client() as c:
    check("未启用时页面 200", c.get("/").status_code == 200)
    check("health 始终豁免", c.get("/api/health").status_code == 200)

store.patch_config({"web_auth": {"enabled": True, "mode": "token", "token": "s3cr3t"}})
with server.app.test_client() as c:
    r = c.get("/")
    check("token 模式无凭证 401", r.status_code == 401)
    check("401 页面带输入框", b'name="token"' in r.data)
    check("错误 token 401", c.get("/?token=wrong").status_code == 401)
    r = c.get("/?token=s3cr3t")
    check("正确 token 200", r.status_code == 200)
    check("首次通过写入 Cookie", "wb_auth" in r.headers.get("Set-Cookie", ""))
    check("Cookie 后续免带", c.get("/").status_code == 200)
    check("API 无凭证 401", server.app.test_client().get("/api/logs").status_code == 401)
    check("API 带 token 200", c.get("/api/logs?token=s3cr3t").status_code == 200)
    check("启用鉴权时 health 仍豁免", c.get("/api/health").status_code == 200)
with server.app.test_client() as c:
    check("X-Auth-Token 放行", c.get("/", headers={"X-Auth-Token": "s3cr3t"}).status_code == 200)
with server.app.test_client() as c:
    check("Bearer 放行", c.get("/", headers={"Authorization": "Bearer s3cr3t"}).status_code == 200)

store.patch_config({"web_auth": {"enabled": True, "mode": "basic",
                                 "username": "admin", "password": "pw123"}})
with server.app.test_client() as c:
    r = c.get("/")
    check("basic 无凭证 401", r.status_code == 401)
    check("WWW-Authenticate 正确", "Basic" in r.headers.get("WWW-Authenticate", ""))
    good = base64.b64encode(b"admin:pw123").decode()
    bad = base64.b64encode(b"admin:wrong").decode()
    check("basic 正确凭据 200", c.get("/", headers={"Authorization": "Basic " + good}).status_code == 200)
    check("basic 错误凭据 401", c.get("/", headers={"Authorization": "Basic " + bad}).status_code == 401)

store.patch_config({"web_auth": {"enabled": True, "mode": "basic",
                                 "username": "", "password": ""}})
with server.app.test_client() as c:
    check("凭据没配齐不拦截", c.get("/").status_code == 200)

store.patch_config({"web_auth": {"enabled": True, "mode": "token",
                                 "token": "supersecret", "password": "pwsecret"},
                    "notify": {"enabled": True, "url": "", "kind": "auto",
                               "chat_id": "", "events": {e: True for e in notify.EVENTS}}})
pc = server._public_config(store.load_config())
blob = json.dumps(pc, ensure_ascii=False)
check("token 不下发明文", "supersecret" not in blob and "***" in pc["web_auth"]["token"])
check("password 不下发明文", "pwsecret" not in blob and "***" in pc["web_auth"]["password"])
check("notify 正常下发", pc["notify"]["kind"] == "auto")

with server.app.test_client() as c:
    check("建立已鉴权会话", c.get("/?token=supersecret").status_code == 200)
    r = c.post("/api/config", json={"web_auth": {"enabled": True, "mode": "token",
                                                 "token": "abcd***xyz"}})
    check("保存设置成功", r.status_code == 200)
    check("掩码不覆盖真 token", store.load_config()["web_auth"]["token"] == "supersecret")
    check("保存后顺带种 Cookie", "wb_auth" in r.headers.get("Set-Cookie", ""))


# ---------------------------------------------------------------- 文案与定时
print("\n[4] 说明删除 / 过期文案 / B站定时 10 分钟")
html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
wsrc = open(os.path.join(ROOT, "app", "worker.py"), encoding="utf-8").read()
readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
check("已删除上回加回的说明", "达到上限 +1 会触发 B站 24h 投稿封禁" not in html)
check("过期文案已改本地定时", "本地定时稿件过期超过" in html)
check("旧过期文案已不存在", ">定时稿件过期超过" not in html)
check("前端版本号 v103", "app.js?v=103" in html and "style.css?v=103" in html)
check("worker 过期日志同步", "本地定时稿件已过期超过" in wsrc)
check("SCHEDULE_MIN_AHEAD = 600", "SCHEDULE_MIN_AHEAD = 10 * 60" in wsrc)
check("旧 4h30m 常量已移除", "4 * 3600 + 600" not in wsrc)
check("README 去掉 4 小时 30 分", "4 小时 30 分" not in readme)
check("README 写明 10 分钟", "≥ 10 分钟" in readme)
check("README 有通知说明", "状态通知（Webhook）" in readme)
check("README 有鉴权说明", "Web 访问鉴权" in readme)

now = time.time()
check("B站定时 2 小时交B站", worker._schedule_action(
    {"publish_at": now + 2 * 3600, "schedule_mode": "bili"}) == "go")
check("B站定时 5 分钟本地候到点", worker._schedule_action(
    {"publish_at": now + 5 * 60, "schedule_mode": "bili"}) == "wait")
check("B站定时 5 小时交B站", worker._schedule_action(
    {"publish_at": now + 5 * 3600, "schedule_mode": "bili"}) == "go")
check("本地定时 3 天仍本地等", worker._schedule_action(
    {"publish_at": now + 3 * 86400, "schedule_mode": "local"}) == "wait")
check("B站定时超 15 天失败", worker._schedule_action(
    {"publish_at": now + 16 * 86400, "schedule_mode": "bili"}) == "fail")


# ---------------------------------------------------------------- 收尾
srv.shutdown()
server._account, server.worker.refresh_quota = _orig_account, _orig_quota
(store.DATA_DIR, store.CONFIG_PATH, store.DB_PATH, store._dirs_ready_for) = _orig
store._cfg_cache.update({"sig": None, "data": None})

print("\n===== 结果：%d 通过 / %d 失败 =====" % (ok, fail))
sys.exit(1 if fail else 0)
