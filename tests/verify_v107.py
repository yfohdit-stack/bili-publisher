"""v107 回归：封面被B站拒收时暂停队列 5 分钟，到点自动重试。

全部离线，不碰B站接口。运行：python3 tests/verify_v107.py
"""
import importlib
import os
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

TMP = tempfile.mkdtemp(prefix="bili-v107-")
store = importlib.import_module("app.store")
store.DATA_DIR = TMP
store.DB_PATH = os.path.join(TMP, "publish.db")
store.CONFIG_PATH = os.path.join(TMP, "config.json")
store.init_db()
store.patch_config({"quota_limit": 50, "quota_hours": 24})

worker = importlib.import_module("app.worker")
worker.HISTORY_FILE = os.path.join(TMP, "upload_history.json")
quota = importlib.import_module("app.quota")
ledger = importlib.import_module("app.ledger")

PASS, FAIL = [], []

COVER_ERR = ("ValueError: 封面不合法 Picture(height='572', width='1146', "
             "imageType='png', size=138, "
             "url='file:///home/yfohdit/bili-publisher-deploy/bili-publisher/"
             "data/covers/3fcb6a8b27d4.jpg')")


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   [{extra}]" if extra and not cond else ""))


def new_task(**kw):
    cover = os.path.join(TMP, "cover.jpg")
    with open(cover, "wb") as f:
        f.write(b"\xff\xd8\xff\xd9")
    payload = {"title": "测试稿", "files": [{"path": os.path.join(TMP, "x.mp4"), "size": 1}],
               "cover_path": cover, "max_attempts": 2}
    payload.update(kw)
    return store.create_task(payload)


def ok_q():
    return {"ok": True, "count": 10, "limit": 50, "hours": 24, "confirmed": 10, "pending": 0,
            "remote_count": 10, "local_count": 0, "remote_keys": [], "logged_in": True,
            "incomplete": False, "items": [], "checked_at": time.time()}


# --------------------------------------------------------- 1. 识别
print("\n[1] 封面不合法的识别，不能误伤限流/普通错误")
check("真实报错被识别", worker._cover_invalid(COVER_ERR))
check("只有 Picture(...) 也被识别", worker._cover_invalid(
    "ValueError: Picture(height='572', imageType='png')"))
check("406 限流不算封面问题", not worker._cover_invalid("NetworkException: 406"))
check("-702 封面频率不算封面不合法", not worker._cover_invalid("接口返回 -702：请求频率过高"))
check("普通错误不算", not worker._cover_invalid("TimeoutError: read timeout"))


# --------------------------------------------------------- 2. 失败分流
print("\n[2] 第 1 次封面不合法：整条队列暂停 5 分钟，任务留队、不吃 attempts")
worker._stop.wait = lambda *_: None
worker._ensure_watchdog = lambda: None
worker._enqueue_archive = lambda *a, **k: None
worker._delete_after = lambda: "no"
worker.pause(False)


def run(publish, task=None):
    t = task or new_task(attempts=0)
    with mock.patch.object(worker, "fresh_quota", return_value=ok_q()), \
         mock.patch.object(worker.official, "publish", side_effect=publish):
        worker._handle(t)
    return store.get_task(t["id"]), t


def raise_cover(*a, **k):
    raise ValueError(COVER_ERR)


g, t = run(raise_cover)
check("任务留在 queued（不会标红成失败）", g["status"] == "queued", g["status"])
check("阶段标为封面校验", g["stage"] == "封面校验", g["stage"])
check("不消耗重试次数（attempts 回到进入本轮前的值）",
      int(g["attempts"]) == 0, str(g["attempts"]))
check("队列已暂停", worker.is_paused())
check("暂停原因是 cover", worker.pause_reason() == "cover", worker.pause_reason())
left = worker.cover_resume_at() - time.time()
check("等待约 300 秒", 285 <= left <= 305, "%.1f" % left)
check("暂停原因与等待时刻已落盘（重启不丢）",
      store.load_config().get("paused_reason") == "cover"
      and float(store.load_config().get("cover_resume_at") or 0) > time.time())
check("封面拒收不写额度账本（本轮没提交）",
      ledger.count("") == 0, str(ledger.recent("", 24)))
check("提示文案含「5 分钟后自动重试」", "5 分钟后自动重试" in (g["message"] or ""),
      g["message"])
check("总日志里能看到这条暂停", any("封面被B站拒收" in (r.get("msg") or "")
                               for r in store.get_all_logs(50)))


# --------------------------------------------------------- 3. 自动/人工恢复
print("\n[3] 到点自动继续；人工点「继续队列」也能立刻解除")
check("未到点不恢复", not worker._maybe_resume_cover(time.time()))
worker._cover_until = time.time() - 1
check("到点自动恢复", worker._maybe_resume_cover(time.time()) and not worker.is_paused())
check("恢复后清掉等待时刻", worker.cover_resume_at() == 0.0
      and float(store.load_config().get("cover_resume_at") or 0) == 0.0)

worker._cover_until = time.time() + 300
worker.pause(True, "cover")
worker.pause(False)      # HTTP /api/queue/pause 走的就是这条路
check("人工继续会清掉封面等待时刻",
      not worker.is_paused() and worker.cover_resume_at() == 0.0)


# --------------------------------------------------------- 4. 重启恢复
print("\n[4] 重启后仍按封面等待计时")
worker.pause(True, "cover")
store.patch_config({"paused_queue": True, "paused_reason": "cover",
                    "cover_resume_at": time.time() + 300})
worker._paused.clear()
with mock.patch.object(worker.threading, "Thread") as th:
    th.return_value.is_alive.return_value = False
    worker.start()
check("重启后保持暂停", worker.is_paused())
check("重启后恢复等待时刻", worker.pause_reason() == "cover"
      and worker.cover_resume_at() > time.time())
worker._paused.clear()
store.patch_config({"paused_queue": False, "paused_reason": "", "cover_resume_at": 0.0})


# --------------------------------------------------------- 5. 接口与前端
print("\n[5] 接口下发等待时刻、前端有对应横幅")
server_py = open(os.path.join(BASE, "app", "server.py"), encoding="utf-8").read()
app_js = open(os.path.join(BASE, "web", "app.js"), encoding="utf-8").read()
html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
check("/api/tasks 与 bootstrap 都下发 cover_resume_at",
      server_py.count("cover_resume_at") >= 2)
# v111 重排优先级后，cover 分支写成 if (reason === "cover")，判断落在暂停块内部
check("前端识别 cover 暂停原因并显示倒计时",
      'reason === "cover"' in app_js and "封面被 B站 拒收" in app_js)
check("前端保存 coverResumeAt", "coverResumeAt" in app_js)
ver = [ln.split('"')[1] for ln in server_py.splitlines() if "APP_VERSION =" in ln][0]
check("版本号与静态资源 ?v= 同步", ("?v=" + ver.lstrip("v").replace(".", "")) in html, ver)
check("大版本不低于 v107", int(ver.lstrip("v").split(".")[0]) >= 107, ver)

print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败：" + "、".join(FAIL))
    sys.exit(1)
print("v107 回归全部通过")
