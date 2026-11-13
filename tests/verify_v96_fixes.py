"""v96 回归：额度分支、接口限流分流、暂停态持久化、BV/aid 对账。

全部离线，不碰B站接口。运行：python3 tests/verify_v96_fixes.py
"""
import ast
import importlib
import json
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

TMP = tempfile.mkdtemp(prefix="bili-v96-")
store = importlib.import_module("app.store")
store.DATA_DIR = TMP
store.DB_PATH = os.path.join(TMP, "publish.db")
store.CONFIG_PATH = os.path.join(TMP, "config.json")
store.init_db()

worker = importlib.import_module("app.worker")
worker.HISTORY_FILE = os.path.join(TMP, "upload_history.json")
quota = importlib.import_module("app.quota")
ledger = importlib.import_module("app.ledger")
official = importlib.import_module("app.channels.official")

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   [{extra}]" if extra and not cond else ""))


def quota_result(count=0, limit=50, ok=True, error="", **kw):
    d = {"count": count, "limit": limit, "hours": 24, "ok": ok, "error": error,
         "logged_in": True, "incomplete": False, "confirmed": count, "pending": 0,
         "remote_count": count, "local_count": 0, "checked_at": time.time()}
    d.update(kw)
    return d


def new_task(**kw):
    cover = os.path.join(TMP, "cover.jpg")
    with open(cover, "wb") as f:
        f.write(b"\xff\xd8\xff\xd9")
    payload = {"title": "测试稿", "files": [{"path": os.path.join(TMP, "x.mp4"), "size": 1}],
               "cover_path": cover, "channel": "official", "max_attempts": 2}
    payload.update(kw)
    return store.create_task(payload)


# ---------------------------------------------------------------- 1. 限流识别
print("\n[1] 真实错误码能否被识别成限流")
check("NetworkException: 406 → upload", worker._rate_kind("NetworkException: 406") == "upload")
check("HTTP 429 → upload", worker._rate_kind("HTTP 429 Too Many Requests") == "upload")
check("-702 封面频率 → cover", worker._rate_kind("接口返回 -702：请求频率过高") == "cover")
check("too many requests → upload", worker._rate_kind("Too Many Requests") == "upload")
check("普通稿件错误不是限流", worker._rate_kind("ValueError: 封面不合法") == "")
check("BV 号里的 406 不误判", worker._rate_kind("稿件 BV1GeWT6iE406 解析失败") == "")
check("风控验证码不误判为限流", worker._rate_kind("-352 风控校验失败") == "")


# ---------------------------------------------------------------- 2. 额度并集去重
print("\n[2] 额度 = B站列表 ∪ 本机账本（双键 + 本地重复去重）")
items = [{"bvid": "BV%04d" % i, "aid": 1000 + i} for i in range(50)]
rows = [
    {"ts": time.time(), "task_id": "a", "bvid": "BV0000", "aid": 1000},  # 与列表重复
    {"ts": time.time(), "task_id": "b", "bvid": "BV9001", "aid": 0},     # 只给 BV
    {"ts": time.time(), "task_id": "c", "bvid": "", "aid": 9001},        # 新稿
]
q = quota.split_quota(items, rows)
check("50 已确认 + 2 未确认 = 52", q["count"] == 52, str(q))
check("重复的一条只算一次", q["intersect"] == 1, str(q["intersect"]))
check("不做交集不足回退", q["fallback"] is False)
check("keys 含 BV 键与 aid 键", "b:BV0000" in q["keys"] and "a:1000" in q["keys"])

rows2 = [
    {"ts": time.time(), "task_id": "d", "bvid": "BV7777", "aid": 7777},  # 两个键
    {"ts": time.time(), "task_id": "e", "bvid": "", "aid": 7777},        # 共享 a:7777
    {"ts": time.time(), "task_id": "g", "bvid": "BV7777", "aid": 0},     # 共享 b:BV7777
    {"ts": time.time(), "task_id": "f", "bvid": "", "aid": 0},           # 无键，保守单算
]
q2 = quota.split_quota([], rows2)
check("同一稿的三种写法经共享键并成 1 篇", q2["pending"] == 2, str(q2))
check("无身份键记录各算一篇（不静默丢弃）", q2["count"] == 2, str(q2))


# ---------------------------------------------------------------- 3. 账本
print("\n[3] 账本去重与写入失败必须抛错")
ledger.record("accA", task_id="t1", bvid="BVAAA", aid=111)
ledger.record("accA", task_id="t1", bvid="BVAAA", aid=111)
ledger.record("accA", task_id="t2", bvid="", aid=111)   # 同 aid，视为同一稿
check("同任务重复写只记 1 条", len(ledger.recent("accA", 24)) == 1,
      str(len(ledger.recent("accA", 24))))
ledger.record("accB", task_id="t3", bvid="BVBBB", aid=222)
check("账号之间隔离", len(ledger.recent("accB", 24)) == 1)
with mock.patch("builtins.open", side_effect=OSError("disk full")):
    try:
        ledger.record("accC", task_id="t4", bvid="BVCCC", aid=333)
        check("_save 失败不再被静默吞掉", False, "未抛异常")
    except OSError:
        check("_save 失败不再被静默吞掉", True)


# ---------------------------------------------------------------- 4. 官方通道 aid
print("\n[4] 投稿返回值的 aid/bvid 解析")
src = open(os.path.join(BASE, "app", "channels", "official.py"), encoding="utf-8").read()
tree = ast.parse(src)
bad = [n.lineno for n in ast.walk(tree)
       if isinstance(n, ast.Assign)
       and any(isinstance(t, ast.Name) and t.id in ("aid", "bvid") for t in n.targets)
       and isinstance(n.value, ast.IfExp)]
check("aid/bvid 不再使用优先级出错的三元表达式", not bad, str(bad))
check("改为先规范 data 再取值", 'data = result.get("data")' in src)


# ---------------------------------------------------------------- 5. 限流状态机
print("\n[5] 限流分流 60s / 300s，不吃 attempts")
worker._rate_state.clear(); worker._rate_until.clear(); worker._rate_wait.clear()
worker.pause(False)
t = new_task()
before = store.get_task(t["id"])["attempts"]
worker._rate_limit_backoff(t["id"], t, "NetworkException: 406")
got = store.get_task(t["id"])
check("406 冷却 300 秒", worker.rate_wait() == 300, str(worker.rate_wait()))
check("406 不消耗 attempts（保持进入本轮前的值）", got["attempts"] == before,
      f"{before} -> {got['attempts']}")
check("任务留在 queued", got["status"] == "queued", got["status"])
check("单次限流不暂停队列", not worker.is_paused())
check("阶段显示限流冷却", got["stage"] == "限流冷却", got["stage"])

worker._rate_state.clear(); worker._rate_until.clear(); worker._rate_wait.clear()
t2 = new_task(attempts=1)
worker._rate_limit_backoff(t2["id"], t2, "-702 请求频率过高")
check("-702 冷却 60 秒", worker.rate_wait() == 60, str(worker.rate_wait()))

worker._rate_state.clear(); worker._rate_until.clear(); worker._rate_wait.clear()
t3 = new_task(attempts=0)
for _ in range(3):
    worker._rate_limit_backoff(t3["id"], t3, "NetworkException: 406")
check("额度档连撞 3 次暂停队列", worker.is_paused())
worker.reset_rate_hits()
check("人工恢复清掉冷却", worker.rate_until() == 0.0 and worker.rate_wait() == 0.0)
worker.pause(False)


# ---------------------------------------------------------------- 6. 暂停态落盘
print("\n[6] 暂停态跨重启保留")
worker.pause(True)
check("pause(True) 写入 config.paused_queue",
      store.load_config().get("paused_queue") is True)
worker.pause(False)
check("pause(False) 清掉 config.paused_queue",
      store.load_config().get("paused_queue") is False)
store.patch_config({"paused_queue": True})
worker._paused.clear()
# 只验证启动分支读取配置的判定，不真的起工作线程
with mock.patch.object(worker.threading, "Thread") as th:
    th.return_value.is_alive.return_value = False
    worker.start()
    check("start() 依据配置恢复暂停", worker.is_paused())
worker._paused.clear()
store.patch_config({"paused_queue": False})


# ---------------------------------------------------------------- 7. _handle 分支
print("\n[7] _handle：额度满 / 统计不全 / 未登录 / 限流 / 成功")
worker._stop.wait = lambda *_: None          # 加速 finally
worker._ensure_watchdog = lambda: None
worker._enqueue_archive = lambda *a, **k: None
worker._delete_after = lambda: "no"


def run_handle(q, publish=None):
    worker.pause(False)
    worker._rate_state.clear(); worker._rate_until.clear(); worker._rate_wait.clear()
    task = new_task(attempts=1)
    with mock.patch.object(worker, "fresh_quota", return_value=q), \
         mock.patch.object(worker.official, "publish",
                           side_effect=publish if publish else None,
                           return_value={"bvid": "BVNEW", "aid": 9999}):
        worker._handle(task)
    return store.get_task(task["id"]), task


full = quota_result(count=51, limit=50, ok=False, error="近 24 小时已投稿 51 个，达到上限 50")
g, _ = run_handle(full)
check("额度满：不再标 blocked 死稿", g["status"] != "blocked", g["status"])
check("额度满：任务留在 queued", g["status"] == "queued", g["status"])
check("额度满：阶段标额度已满", g["stage"] == "额度已满", g["stage"])
check("额度满：队列自动暂停", worker.is_paused())
check("额度满：文案带上 51/50", "51" in (g["message"] or "") and "50" in (g["message"] or ""),
      g["message"])

inc = quota_result(count=48, limit=50, ok=False, error="额度统计失败：接口超时",
                   incomplete=True)
g, _ = run_handle(inc)
check("统计不全：留 queued 而不是 blocked", g["status"] == "queued", g["status"])
check("统计不全：阶段标额度待核对", g["stage"] == "额度待核对", g["stage"])
check("统计不全：队列暂停", worker.is_paused())

no_login = quota_result(count=0, ok=False, error="账号未登录", logged_in=False)
g, _ = run_handle(no_login)
check("未登录：标 failed 且不暂停队列", g["status"] == "failed" and not worker.is_paused())

ok_q = quota_result(count=30, ok=True)
g, _ = run_handle(ok_q, publish=RuntimeError("NetworkException: 406"))
check("限流异常：任务留 queued", g["status"] == "queued", g["status"])
check("限流异常：attempts 不被 +1", g["attempts"] == 0, str(g["attempts"]))
check("限流异常：进入 300 秒冷却", worker.rate_wait() == 300, str(worker.rate_wait()))
check("限流异常：不暂停队列", not worker.is_paused())
# 对照放在最后：run_handle 每轮开头会清空限流状态，放前面会把上面的冷却抹掉
g2, _ = run_handle(ok_q, publish=RuntimeError("普通上传失败"))
check("对照：普通失败会 attempts+1", g2["attempts"] == 1, str(g2["attempts"]))

worker._rate_state[""] = {"kind": "cover", "n": 2}
worker._rate_until[""] = time.time() + 60
g, _ = run_handle(ok_q)
check("成功投稿：状态 success", g["status"] == "success", g["status"])
check("成功投稿：清掉连续限流计数", not worker._rate_state, str(worker._rate_state))
check("成功投稿：清掉限流冷却", worker.rate_until() == 0.0)
worker.pause(False)


# ---------------------------------------------------------------- 8. HTTP 接口
print("\n[8] /api/tasks 暴露冷却、/api/queue/pause 恢复时清冷却")
try:
    server = importlib.import_module("app.server")
    client = server.app.test_client()
    worker._rate_state.clear(); worker._rate_until.clear(); worker._rate_wait.clear()
    worker._rate_until["accX"] = time.time() + 300
    worker._rate_wait["accX"] = 300.0
    with mock.patch.object(server.worker, "cached_quota", return_value=quota_result(30)):
        r = client.get("/api/tasks").get_json()
    check("queue 带 rate_until", (r["queue"] or {}).get("rate_until", 0) > 0,
          str(r.get("queue")))
    check("queue 带 rate_wait=300", (r["queue"] or {}).get("rate_wait") == 300,
          str(r.get("queue")))
    client.post("/api/queue/pause", json={"paused": False})
    check("恢复队列清掉冷却", worker.rate_until() == 0.0 and not worker._rate_state)
    worker._rate_until.clear(); worker._rate_wait.clear()
except Exception as e:  # noqa: BLE001
    check("HTTP 接口可加载", False, f"{type(e).__name__}: {e}")


print("\n" + "=" * 56)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)
print("v96 回归全部通过")
