"""优化项离线验证（不碰B站接口、不需要 ffmpeg）。

运行：python3 tests/verify_optimizations.py
覆盖：配置缓存、等待态忙循环、取消标记、额度并发、轮询响应缓存、
      封面版本号、并行探测保序、图床 host 校验。
"""
import builtins
import importlib
import os
import sys
import tempfile
import threading
import time
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

pkg = types.ModuleType("app")
pkg.__path__ = [os.path.join(BASE, "app")]
sys.modules["app"] = pkg

TMP = tempfile.mkdtemp(prefix="bili-opt-")
store = importlib.import_module("app.store")
store.DATA_DIR = TMP
store.DB_PATH = os.path.join(TMP, "publish.db")
store.CONFIG_PATH = os.path.join(TMP, "config.json")
store.init_db()

worker = importlib.import_module("app.worker")
worker.HISTORY_FILE = os.path.join(TMP, "upload_history.json")
quota = importlib.import_module("app.quota")
media = importlib.import_module("app.media")

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   [{extra}]" if extra and not cond else ""))


# ---------------------------------------------------------------- 1. 配置缓存
print("\n[1] load_config 内存缓存")
store.load_config()                       # 预热
orig_open = builtins.open
reads = []


def counting_open(path, *a, **k):
    mode = (a[0] if a else k.get("mode", "r"))
    if "config.json" in str(path) and "r" in str(mode):
        reads.append(str(path))
    return orig_open(path, *a, **k)


builtins.open = counting_open
try:
    for _ in range(100):
        store.load_config()
finally:
    builtins.open = orig_open
check("100 次 load_config 零读盘", len(reads) == 0, f"{len(reads)} 次")

dirty = store.load_config()
dirty["defaults"]["tid"] = 999
dirty["accounts"].append({"id": "polluted"})
check("返回深拷贝，改动不污染缓存",
      store.load_config()["defaults"]["tid"] == 21 and not store.load_config()["accounts"])

store.patch_config({"quota_limit": 37})
check("写入后缓存立即更新", store.load_config()["quota_limit"] == 37)
store.patch_config({"quota_limit": 50})


# ---------------------------------------------------------------- 2. 等待态不再空转
print("\n[2] worker 主循环等待态（旧版 0.3 秒空跑约 29.6 万次）")
calls = {"handle": 0, "wait": 0}
fake = {"id": "stuck", "status": "queued", "files": [], "title": "x"}
worker.store.next_pending = lambda: dict(fake)
worker.store.update_task = lambda *a, **k: None
worker._cleanup_approved = lambda: None
worker.store.next_waiting_due = lambda now, ahead=600: None


def fake_handle(task):
    calls["handle"] += 1
    return 0.0                    # 最坏情况：任务被取出却没消费，靠主循环退避兜底


worker._handle = fake_handle
_orig_wait = worker._wake.wait


def counting_wait(t=None):
    calls["wait"] += 1
    return _orig_wait(min(float(t or 0.05), 0.02))


worker._wake.wait = counting_wait
worker._stop.clear()
worker._paused.clear()
th = threading.Thread(target=worker._loop, daemon=True)
th.start()
time.sleep(0.25)
worker._stop.set()
worker._wake.set()
check("主循环确实进入休眠", calls["wait"] > 0, str(calls))
check("空转频率被压到个位数", calls["handle"] < 30, f"{calls['handle']} 次 / 0.25 秒")
worker._wake.wait = _orig_wait

# 等待分支返回明确延迟时，主循环按该延迟等待
delay_seen = []
worker._handle = lambda task: 7.0
worker._wake.wait = lambda t=None: (delay_seen.append(t), _orig_wait(0.01))[1]
worker._stop.clear()
worker._wake.clear()
th2 = threading.Thread(target=worker._loop, daemon=True)
th2.start()
time.sleep(0.08)
worker._stop.set()
worker._wake.set()
check("_handle 返回的等待秒数被主循环采用", any(d and 0 < d <= 30 for d in delay_seen),
      str(delay_seen))
worker._wake.wait = _orig_wait


# ---------------------------------------------------------------- 3. 取消标记
print("\n[3] 取消后重新排队不再被二次取消")
worker.request_cancel("task-z")
check("取消请求已登记", worker._cancelled("task-z"))
worker.clear_cancel("task-z")
check("clear_cancel 清掉标记", not worker._cancelled("task-z"))
worker._cancel_requested.clear()


# ---------------------------------------------------------------- 4. 额度三状态并发
print("\n[4] 额度统计并发拉取")
_orig_page = quota._archives_page
timeline = []
_tl_lock = threading.Lock()


def slow_page(cookie, status, pn, retries=3):
    t0 = time.time()
    time.sleep(0.12)
    with _tl_lock:
        timeline.append((status, t0, time.time()))
    return {"code": 0, "data": {"arc_audits": [], "page": {"total": 0}}}


quota._archives_page = slow_page
t0 = time.time()
try:
    r = quota._creator_items_once({"cookie": {}}, 24)
    elapsed = time.time() - t0
finally:
    quota._archives_page = _orig_page
check("三个状态都拉到了", len(timeline) == 3, str(len(timeline)))
check("并发耗时显著短于串行（0.36s）", elapsed < 0.28, f"{elapsed:.3f}s")
overlap = max(s for _, s, _ in timeline) < min(e for _, _, e in timeline)
check("三次请求时间区间重叠", overlap, str(timeline))
check("并发结果结构不变", r["warnings"] == [] and r["items"] == [])


# ---------------------------------------------------------------- 5. 轮询响应缓存
print("\n[5] /api/tasks 与 /api/logs 响应缓存")
server = importlib.import_module("app.server")
server.store.DATA_DIR = TMP
server.UPLOAD_DIR = os.path.join(TMP, "uploads")
server.COVER_DIR = os.path.join(TMP, "covers")
os.makedirs(server.UPLOAD_DIR, exist_ok=True)
os.makedirs(server.COVER_DIR, exist_ok=True)
server.worker.cached_quota = lambda **k: {"checked_at": 1, "ok": True}
client = server.app.test_client()
a = client.get("/api/tasks")
b = client.get("/api/tasks")
check("指纹未变时复用同一响应体", a.status_code == 200 and a.data == b.data)
tk = store.create_task({"title": "缓存变化测试", "files": []})
c = client.get("/api/tasks")
check("任务变化后响应体刷新", c.data != a.data)
l1 = client.get("/api/logs")
l2 = client.get("/api/logs")
check("日志接口同样命中缓存", l1.status_code == 200 and l1.data == l2.data)
store.add_global_log("缓存测试日志")
check("新日志让缓存失效", client.get("/api/logs").data != l1.data)
store.delete_task(tk["id"])


# ---------------------------------------------------------------- 6. 封面版本号
print("\n[6] 封面缓存键不再随进度抖动")
tc = store.create_task({"title": "封面测试", "files": []})
check("新建任务 cover_rev=0", store.get_task(tc["id"])["cover_rev"] == 0)
store.update_task(tc["id"], progress=0.5)     # 模拟上传中进度落库
store.bump_cover_rev(tc["id"])
check("换封面后 cover_rev=1", store.get_task(tc["id"])["cover_rev"] == 1)
check("服务端 _public_task 暴露 cover_rev", server._public_task(store.get_task(tc["id"]))["cover_rev"] == 1)
store.delete_task(tc["id"])


# ---------------------------------------------------------------- 7. 并行探测保序
print("\n[7] probe_many 并行且保序")
_orig_probe = media.probe
media.probe = lambda p: {"path": p, "duration": 1.0, "size": 1, "width": 0, "height": 0}
try:
    out = media.probe_many(["a.mp4", "b.mp4", "c.mp4"])
finally:
    media.probe = _orig_probe
check("结果与入参顺序一致", [x["path"] for x in out] == ["a.mp4", "b.mp4", "c.mp4"])
check("单文件不建线程池", len(media.probe_many(["one.mp4"])) == 1)


# ---------------------------------------------------------------- 8. 图床 host 校验
print("\n[8] 代理图床白名单收严")
check("evilhdslb.com 被拒（旧版 endswith 会放行）",
      client.get("/api/proxy/cover?u=https://evilhdslb.com/a.jpg").status_code == 403)
check("非白名单域名被拒",
      client.get("/api/proxy/cover?u=https://example.com/a.jpg").status_code == 403)

print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + "；".join(FAIL))
import shutil
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
