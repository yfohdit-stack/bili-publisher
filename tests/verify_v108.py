"""v108 回归：Webhook 推送正文带账号 + 任何原因的队列暂停都有横幅。

全部离线，不碰B站接口、不发真实 Webhook。运行：python3 tests/verify_v108.py
"""
import importlib
import os
import sys
import tempfile
import time
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

pkg = types.ModuleType("app")
pkg.__path__ = [os.path.join(BASE, "app")]
sys.modules["app"] = pkg

TMP = tempfile.mkdtemp(prefix="bili-v108-")
store = importlib.import_module("app.store")
store.DATA_DIR = TMP
store.DB_PATH = os.path.join(TMP, "publish.db")
store.CONFIG_PATH = os.path.join(TMP, "config.json")
store.init_db()
store.patch_config({"quota_limit": 50, "quota_hours": 24})

worker = importlib.import_module("app.worker")
worker.HISTORY_FILE = os.path.join(TMP, "upload_history.json")
importlib.import_module("app.quota")
importlib.import_module("app.ledger")

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   [{extra}]" if extra and not cond else ""))


PUSHED = []
worker._notify = lambda event, title, text, level="info": PUSHED.append(
    {"event": event, "title": title, "text": text, "level": level})


def new_task(**kw):
    payload = {"title": "测试稿", "files": [], "channel": "official"}
    payload.update(kw)
    return store.create_task(payload)


def push_text(event=None):
    return [p["text"] for p in PUSHED if event is None or p["event"] == event]


# --------------------------------------------------------- 1. 账号解析
print("\n[1] 账号显示名：任务账号优先 → 生效账号 → 默认账号")
store.patch_config({"accounts": [
    {"id": "acc_a", "mid": 11, "uname": "主号A", "cookie": {}},
    {"id": "acc_b", "mid": 22, "uname": "小号B", "cookie": {}},
], "active_account": "acc_a"})
check("任务绑定账号 → 用该账号名", worker._acct_display({"account_id": "acc_b"}) == "小号B")
check("任务未绑定 → 用当前生效账号", worker._acct_display({"account_id": ""}) == "主号A")
check("没有任务对象 → 用当前生效账号", worker._acct_display() == "主号A")
store.patch_config({"accounts": [], "active_account": ""})
check("一个账号都没有 → 默认账号", worker._acct_display() == "默认账号")
store.patch_config({"accounts": [
    {"id": "acc_a", "mid": 11, "uname": "主号A", "cookie": {}},
    {"id": "acc_b", "mid": 22, "uname": "小号B", "cookie": {}},
], "active_account": "acc_a"})


# --------------------------------------------------------- 2. task 推送带账号
print("\n[2] 稿件结果推送（task 事件）正文带【账号：…】")
PUSHED.clear()
t = new_task(account_id="acc_b")
worker._summary(t, "投稿成功：BV1xx411c7mD", "ok")
check("task 事件被推送", len(PUSHED) == 1 and PUSHED[0]["event"] == "task", str(PUSHED))
check("正文带对应账号（不是生效账号）", "【账号：小号B】" in push_text()[0], push_text()[0])
check("标题仍是稿件标题", PUSHED[0]["title"] == "《测试稿》", PUSHED[0]["title"])
check("等级不变", PUSHED[0]["level"] == "ok", PUSHED[0]["level"])

PUSHED.clear()
t2 = new_task(account_id="")
worker._summary(t2, "投稿失败：NetworkException 406", "err")
check("未绑定账号的稿件退回生效账号", "【账号：主号A】" in push_text()[0], push_text()[0])

PUSHED.clear()
store.patch_config({"accounts": [], "active_account": ""})
t3 = new_task()
worker._summary(t3, "投稿成功：BV1yy411c7mE", "ok")
check("无账号时正文写「默认账号」", "【账号：默认账号】" in push_text()[0], push_text()[0])
store.patch_config({"accounts": [
    {"id": "acc_a", "mid": 11, "uname": "主号A", "cookie": {}},
], "active_account": "acc_a"})

PUSHED.clear()
worker._summary(t3, "入队提示", "info", push=False)
check("push=False 不推但日志仍带账号",
      not PUSHED and any("【账号：主号A】" in (r.get("msg") or "")
                         for r in store.get_all_logs(50)))


# --------------------------------------------------------- 3. 额度/封面事件带账号
print("\n[3] 额度、封面、限流事件也带账号")
PUSHED.clear()
q = {"ok": False, "count": 50, "limit": 50, "next_release_at": time.time() + 3600,
     "error": "近 24h 投稿额度已满"}
worker.pause_for_quota(q)
quota_push = [p for p in PUSHED if p["event"] == "quota"]
check("额度暂停推送带账号", quota_push and "【账号：主号A】" in quota_push[0]["text"],
      str(quota_push))
worker.pause(False)

PUSHED.clear()
cover = os.path.join(TMP, "cover.jpg")
open(cover, "wb").write(b"\xff\xd8\xff\xd9")
tc = new_task(account_id="acc_a", cover_path=cover, attempts=0)
worker._cover_invalid_backoff(tc["id"], tc, "ValueError: 封面不合法 Picture(imageType='png')")
cover_push = [p for p in PUSHED if p["event"] == "task"]
check("封面暂停推送带账号", cover_push and "【账号：主号A】" in cover_push[0]["text"],
      str(cover_push))
check("封面暂停也说明 5 分钟后重试",
      cover_push and "5 分钟后自动重试" in cover_push[0]["text"], str(cover_push))
worker._cover_until = time.time() - 1
PUSHED.clear()
check("封面恢复放行", worker._maybe_resume_cover(time.time()))
resume_push = [p for p in PUSHED if p["event"] == "resume"]
check("封面恢复推送带账号", resume_push and "【账号：主号A】" in resume_push[0]["text"],
      str(resume_push))
if worker.is_paused():
    worker.pause(False)


# --------------------------------------------------------- 4. 前端横幅
print("\n[4] 任何原因的暂停都有横幅，样式区分手动/限流")
app_js = open(os.path.join(BASE, "web", "app.js"), encoding="utf-8").read()
css = open(os.path.join(BASE, "web", "style.css"), encoding="utf-8").read()
html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
server_py = open(os.path.join(BASE, "app", "server.py"), encoding="utf-8").read()
check("有兜底暂停分支（手动/重启）", "队列已暂停 · 稿件排队中" in app_js)
check("限流暂停有专门文案", "连续触发 B站 限流 · 队列已暂停" in app_js)
check("重启沿用暂停有提示", "队列在重启后保持暂停" in app_js)
check("手动暂停用中性灰样式", '[data-mode="manual"]' in css)
check("横幅默认隐藏、由状态驱动", 'id="rateBanner" hidden' in html)

ver = [ln.split('"')[1] for ln in server_py.splitlines() if "APP_VERSION =" in ln][0]
check("版本号与静态资源 ?v= 同步", ("?v=" + ver.lstrip("v")) in html, ver)
check("大版本不低于 v108", int(ver.lstrip("v")) >= 108, ver)

print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败：" + "、".join(FAIL))
    sys.exit(1)
print("v108 回归全部通过")
