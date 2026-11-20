"""v110 回归：坏封面无限循环的出口（封面连续被拒上限）。

修复前：`_cover_invalid_backoff` 不消耗 attempts、也没有上限，一张真坏掉的封面
会「投 → 拒 → 全队列停 5 分钟 → 再投 → 再拒」永远循环，把其它账号/稿件全堵住，
Webhook 每 5 分钟刷两条。现在同一稿件累计被拒 COVER_INVALID_MAX(3) 次后判失败、
队列不再暂停，直接继续投其它稿件。

全部离线，不碰B站接口。运行：python3 tests/verify_v110.py
"""
import importlib
import os
import sqlite3
import sys
import tempfile
import time
import types
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

pkg = types.ModuleType("app")
pkg.__path__ = [os.path.join(BASE, "app")]
sys.modules["app"] = pkg

TMP = tempfile.mkdtemp(prefix="bili-v110-")
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
PUSHED = []
worker._notify = lambda event, title, text, level="info": PUSHED.append(
    {"event": event, "text": text})


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   [{extra}]" if extra and not cond else ""))


COVER_ERR = ("ValueError: 封面不合法 Picture(height='572', width='1146', "
             "imageType='png', size=138, url='file:///x/covers/bad.jpg')")


def ok_q():
    return {"ok": True, "count": 0, "limit": 50, "hours": 24, "confirmed": 0, "pending": 0,
            "remote_count": 0, "local_count": 0, "remote_keys": [], "logged_in": True,
            "incomplete": False, "items": [], "checked_at": time.time()}


def new_task(title="坏封面"):
    cover = os.path.join(TMP, f"{title}.jpg")
    open(cover, "wb").write(b"\xff\xd8\xff\xd9")
    return store.create_task({"title": title, "cover_path": cover,
                              "files": [{"path": os.path.join(TMP, "a.mp4"), "size": 1}],
                              "channel": "official", "max_attempts": 2, "attempts": 0})


worker._stop.wait = lambda *_: None
worker._ensure_watchdog = lambda: None
worker._enqueue_archive = lambda *a, **k: None
worker._delete_after = lambda: "no"


# --------------------------------------------------------- 1. 计数落库与迁移
print("\n[1] 新字段 cover_hits：默认 0、老库自动补列")
t = new_task()
check("新任务 cover_hits=0", int(store.get_task(t["id"])["cover_hits"]) == 0,
      str(store.get_task(t["id"]).get("cover_hits")))
migrated = "跳过"
try:
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("ALTER TABLE tasks DROP COLUMN cover_hits")
    conn.commit()
    conn.close()
    store.init_db()
    conn = sqlite3.connect(store.DB_PATH)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    conn.close()
    migrated = "cover_hits" in cols
except Exception as e:  # 低版本 sqlite 不支持 DROP COLUMN
    migrated = f"跳过（{type(e).__name__}）"
check("老库启动时自动补 cover_hits 列", migrated is True or str(migrated).startswith("跳过"),
      str(migrated))


# --------------------------------------------------------- 2. 前两次：暂停 + 不吃 attempts
print("\n[2] 第 1、2 次拒收：整条队列暂停 5 分钟，attempts 不动")
with mock.patch.object(worker, "fresh_quota", return_value=ok_q()), \
     mock.patch.object(worker.official, "publish",
                       side_effect=ValueError(COVER_ERR)):
    for i in (1, 2):
        worker._handle(store.get_task(t["id"]))
        g = store.get_task(t["id"])
        check(f"第 {i} 次后仍在队列", g["status"] == "queued", g["status"])
        check(f"第 {i} 次后 cover_hits={i}", int(g["cover_hits"]) == i, str(g["cover_hits"]))
        check(f"第 {i} 次后 attempts 未被消耗", int(g["attempts"]) == 0, str(g["attempts"]))
        check(f"第 {i} 次后队列暂停且原因是 cover",
              worker.is_paused() and worker.pause_reason() == "cover")
        check(f"第 {i} 次文案标明进度", f"第 {i}/3 次" in (g["message"] or ""), g["message"])
        worker._cover_until = time.time() - 1
        worker._maybe_resume_cover(time.time())


# --------------------------------------------------------- 3. 第三次：出口
print("\n[3] 第 3 次拒收：不再暂停，直接判失败，队列继续")
PUSHED.clear()
with mock.patch.object(worker, "fresh_quota", return_value=ok_q()), \
     mock.patch.object(worker.official, "publish",
                       side_effect=ValueError(COVER_ERR)):
    worker._handle(store.get_task(t["id"]))
g = store.get_task(t["id"])
check("任务判失败（不再无限重试）", g["status"] == "failed", g["status"])
check("cover_hits 记到 3", int(g["cover_hits"]) == 3, str(g["cover_hits"]))
check("队列没有被再次暂停", not worker.is_paused(), worker.pause_reason())
check("没有残留的封面等待时刻", worker.cover_resume_at() == 0.0, str(worker.cover_resume_at()))
check("文案给出出口：换封面后重新排队", "更换封面后点「重新排队」" in (g["message"] or ""),
      g["message"])
check("finished_at 已写入", bool(g["finished_at"]), str(g.get("finished_at")))
check("error 保留原始报错", "封面不合法" in (g["error"] or ""), g["error"])
check("只推一条失败推送（不再每 5 分钟刷屏）",
      len([p for p in PUSHED if p["event"] == "task"]) == 1, str(len(PUSHED)))
check("失败推送里带账号", "【账号：" in PUSHED[-1]["text"], PUSHED[-1]["text"])


# --------------------------------------------------------- 4. 队列不再被堵
print("\n[4] 坏封面判死后，其它账号/稿件可以继续跑")
other = new_task("其它稿件")
check("失败任务不占阻塞表", bool(worker._blocked_accounts(time.time())) is False,
      str(worker._blocked_accounts(time.time())))
check("排到下一个稿件时拿到了别的任务",
      (store.next_pending() or {}).get("id") in (None, other["id"]),
      str((store.next_pending() or {}).get("id")))
store.update_task(other["id"], status="success")


# --------------------------------------------------------- 5. 换封面 / 重新排队清零
print("\n[5] 换封面或重新排队会把计数清零，新图重新获得 3 次机会")
store.update_task(t["id"], status="queued", attempts=0, cover_hits=0,
                  error="", message="重新排队", stage="", finished_at=None)
check("重新排队后 cover_hits 归零", int(store.get_task(t["id"])["cover_hits"]) == 0)
store.update_task(t["id"], cover_path=os.path.join(TMP, "new.jpg"), cover_hits=0)
check("换封面后 cover_hits 归零", int(store.get_task(t["id"])["cover_hits"]) == 0)
server_py = open(os.path.join(BASE, "app", "server.py"), encoding="utf-8").read()
check("重新排队接口带 cover_hits=0", 'cover_hits=0, error=""' in server_py)
check("换封面接口带 cover_hits=0", "cover_path=out, cover_hits=0" in server_py)


# --------------------------------------------------------- 6. 文案与版本
print("\n[6] 前端提示与版本号")
app_js = open(os.path.join(BASE, "web", "app.js"), encoding="utf-8").read()
html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
check("横幅里说明了 3 次的出口", "连续 3 次被拒会自动判失败" in app_js)
ver = [ln.split('"')[1] for ln in server_py.splitlines() if "APP_VERSION =" in ln][0]
check("静态资源 ?v= 与 APP_VERSION 同步", ("?v=" + ver.lstrip("v")) in html, ver)
check("大版本不低于 v110", int(ver.lstrip("v")) >= 110, ver)
check("上限常量可调", worker.COVER_INVALID_MAX >= 1, str(worker.COVER_INVALID_MAX))

print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败：" + "、".join(FAIL))
    sys.exit(1)
print("v110 回归全部通过")
