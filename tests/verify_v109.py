"""v109 回归：多分P稿件（>10P）节流计算不再数组越界。

之前 `_wait_from()` 拿 `over - 1` 直接当列表下标，need > WINDOW_MAX 时
`over - 1 >= len(h)` → IndexError → 被当成普通投稿失败，连撞两次重试后判死。
全部离线。运行：python3 tests/verify_v109.py
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

TMP = tempfile.mkdtemp(prefix="bili-v109-")
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
worker._notify = lambda *a, **k: None

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   [{extra}]" if extra and not cond else ""))


def old_wait(h, need, now):
    """修复前的实现，用来对比 need<=WINDOW_MAX 时结果一字不差。"""
    if need <= 0 or not h:
        return 0
    over = len(h) + need - worker.WINDOW_MAX
    if over <= 0:
        return 0
    return max(1, int(h[over - 1] + worker.WINDOW_SECS - now) + 1)


NOW = time.time()
MAX = worker.WINDOW_MAX
WIN = worker.WINDOW_SECS


# --------------------------------------------------------- 1. 越界不再发生
print("\n[1] need 从 1 到 20、窗口 0~12 条，全组合都不许抛异常")
crash = []
for hlen in range(0, 13):
    h = [NOW - WIN + i for i in range(hlen)]
    for need in range(1, 21):
        try:
            w = worker._wait_from(h, need, NOW)
            if not isinstance(w, int) or w < 0:
                crash.append(f"hlen={hlen} need={need} 返回 {w!r}")
        except Exception as e:
            crash.append(f"hlen={hlen} need={need} {type(e).__name__}")
check("260 种组合无异常", not crash, "; ".join(crash[:3]))

print("\n[2] 修复前后的根因对照")
h1 = [NOW - 60]
try:
    old_wait(h1, 11, NOW)
    old_ok = True
except IndexError:
    old_ok = False
check("旧实现确实越界（need=11）", not old_ok)
check("新实现不再抛异常", isinstance(worker._wait_from(h1, 11, NOW), int))


# --------------------------------------------------------- 3. 语义：need<=MAX 结果不变
print("\n[3] need 不超过窗口容量时，结果与修复前逐项一致")
diff = []
for hlen in range(0, 11):
    h = [NOW - WIN + i for i in range(hlen)]
    for need in range(1, MAX + 1):
        if worker._wait_from(h, need, NOW) != old_wait(h, need, NOW):
            diff.append((hlen, need))
check("55 组对比全部一致", not diff, str(diff[:3]))

print("\n[4] need > 容量时按容量算：等窗口清空，不越过列表尾部")
h2 = [NOW - 400, NOW - 300, NOW - 200, NOW - 100]
w11 = worker._wait_from(h2, 11, NOW)
w10 = worker._wait_from(h2, MAX, NOW)
check("11P 与 10 的结果一致（都等窗口清空）", w11 == w10, f"{w11} vs {w10}")
check("11P 等待时间 = 最新一条滑出窗口 ≈ 500s", 495 <= w11 <= 505, str(w11))
check("空窗口时 11P 直接放行（首个稿件能投）", worker._wait_from([], 11, NOW) == 0)
check("窗口已满 12 条时 1P 也绝不越界", isinstance(
    worker._wait_from([NOW - WIN + i for i in range(12)], 1, NOW), int))
check("need=0 直接放行", worker._wait_from(h2, 0, NOW) == 0)


# --------------------------------------------------------- 5. _wait_seconds 链路
print("\n[5] _wait_seconds(pages=11) 走真实链路也正常")
with mock.patch.object(worker, "_history", lambda now=None, account_id="": [NOW - 60] * 11):
    w = worker._wait_seconds(11, account_id="")
check("11 个分P + 窗口 11 条 → 返回等待秒数（不是抛错）",
      isinstance(w, int) and w > 0, str(w))
with mock.patch.object(worker, "_history", lambda now=None, account_id="": []):
    check("空窗口 → 0", worker._wait_seconds(11, account_id="") == 0)


# --------------------------------------------------------- 6. 端到端：连续投 >10P 不再判死
print("\n[6] 端到端：窗口已有记录时投 11P 稿件，进入节流等待而不是失败")
worker._stop.wait = lambda *_: None
worker._ensure_watchdog = lambda: None
worker._enqueue_archive = lambda *a, **k: None
worker._delete_after = lambda: "no"
worker.pause(False)

files = [{"path": os.path.join(TMP, f"p{i}.mp4"), "size": 1} for i in range(11)]
cover = os.path.join(TMP, "cover.jpg")
open(cover, "wb").write(b"\xff\xd8\xff\xd9")
task = store.create_task({"title": "11P 连续投稿", "files": files,
                          "cover_path": cover, "max_attempts": 2,
                          "attempts": 0})
with mock.patch.object(worker, "_history", lambda now=None, account_id="": [NOW - 60] * 11), \
     mock.patch.object(worker.official, "publish") as pub:
    got = worker._handle(task)
g = store.get_task(task["id"])
check("没有抛异常、没有进失败分支", got == 0.0 and g["status"] == "queued", str(g["status"]))
check("阶段是「节流等待」", g["stage"] == "节流等待", g["stage"])
check("attempts 未被消耗", int(g["attempts"]) == 0, str(g["attempts"]))
check("没有真的去调用投稿", not pub.called)
check("提示里写清了本稿件占几个名额", "11 个名额" in (g.get("message") or ""), g.get("message"))
blocked = worker._blocked_accounts(NOW)
check("该账号被登记为稍后重试（不影响其它账号）",
      bool(blocked) and all(u > NOW for u in blocked.values()), str(blocked))

print("\n[7] 版本号同步")
server_py = open(os.path.join(BASE, "app", "server.py"), encoding="utf-8").read()
html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
ver = [ln.split('"')[1] for ln in server_py.splitlines() if "APP_VERSION =" in ln][0]
check("静态资源 ?v= 与 APP_VERSION 同步", ("?v=" + ver.lstrip("v")) in html, ver)
check("大版本不低于 v109", int(ver.lstrip("v")) >= 109, ver)

print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败：" + "、".join(FAIL))
    sys.exit(1)
print("v109 回归全部通过")
