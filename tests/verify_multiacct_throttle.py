"""v75 专项验证：节流窗口 / 风控冷却按账号隔离，队列跳过被节流账号。

离线运行，不访问B站接口：
    python3 tests/verify_multiacct_throttle.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import store, worker  # noqa: E402

TMP = tempfile.mkdtemp(prefix="v75-multi-")
store.DATA_DIR = TMP
store.DB_PATH = os.path.join(TMP, "publish.db")
store.CONFIG_PATH = os.path.join(TMP, "config.json")
worker.HISTORY_FILE = os.path.join(TMP, "upload_history.json")
store._dirs_ready_for = None
store.ensure_dirs()
store.init_db()

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


def reset():
    worker._risk_until.clear()
    worker._account_wait.clear()
    if os.path.exists(worker.HISTORY_FILE):
        os.remove(worker.HISTORY_FILE)


A, B, C = "acc-A", "acc-B", "acc-C"
now = time.time()

print("\n[1] 节流历史按账号分桶")
reset()
worker._record_videos(10, A)
by = worker._load_history()
check("写入后只有 A 一个桶", set(by.keys()) == {A}, str(list(by.keys())))
check("A 桶内 10 条", len(by[A]) == 10, str(len(by[A])))
check("A 自己要等", worker._wait_seconds(1, now, A) > 0)
check("B 完全不受影响 → 放行", worker._wait_seconds(1, now, B) == 0,
      str(worker._wait_seconds(1, now, B)))
check("C 完全不受影响 → 放行", worker._wait_seconds(1, now, C) == 0)

worker._record_videos(3, B)
check("B 记 3 条后仍放行", worker._wait_seconds(1, now, B) == 0)
check("A 仍是 10 条未被覆盖", len(worker._history(now, A)) == 10)
check("B 是 3 条", len(worker._history(now, B)) == 3)

print("\n[2] 旧版纯数组格式兼容（升级不丢记录）")
reset()
with open(worker.HISTORY_FILE, "w", encoding="utf-8") as f:
    json.dump([now - 30] * 10, f)
check("旧数组归入默认账号桶", worker._wait_seconds(1, now) > 0)
check("具名账号 B 不受旧数据影响", worker._wait_seconds(1, now, B) == 0)
worker._record_videos(1, A)
check("写入新格式后旧记录仍在默认桶", worker._wait_seconds(1, now) > 0)
check("文件已升级为 v2 分桶格式", json.load(open(worker.HISTORY_FILE))["v"] == 2)

print("\n[3] 风控冷却按账号隔离")
reset()
worker._risk_until[A] = now + 120
check("A 在冷却中", worker._risk_cooling(A) > 0)
check("B 不在冷却中", worker._risk_cooling(B) == 0)
blocked = worker._blocked_accounts(now)
check("_blocked_accounts 只列 A", set(blocked) == {A}, str(set(blocked)))
worker._risk_until[A] = now - 1
check("A 冷却过期后自动清掉", A not in worker._blocked_accounts(now))

print("\n[4] 主循环跳过被节流账号，取下一个账号的任务")
reset()
store._lock.acquire()
try:
    conn = store.connect()
    conn.execute("DELETE FROM tasks")
    conn.commit()
finally:
    store._lock.release()

for i, acc in enumerate([A, A, B, C]):
    store.create_task({
        "id": f"t{i}", "title": f"稿件{i}", "account_id": acc,
        "files": [{"path": "x.mp4", "part_title": "p1"}],
        "created_at": now + i,
    })

check("无阻塞时取最早任务（t0/A）", store.next_pending()["id"] == "t0")

# A 占满窗口
worker._record_videos(10, A)
blocked = worker._blocked_accounts(now)
check("A 被判定为阻塞账号", A in blocked, str(set(blocked)))
check("B / C 未被阻塞", B not in blocked and C not in blocked)

task = worker._pick_pending(set(blocked))
check("跳过 A 后取到 B 的任务 t2", task and task["id"] == "t2", str(task and task["id"]))
check("取到的任务确实是 B 账号", task and task["account_id"] == B)

# 再模拟 B 也占满
worker._record_videos(10, B)
blocked2 = worker._blocked_accounts(now)
task2 = worker._pick_pending(set(blocked2))
check("A、B 都阻塞时取到 C 的任务 t3", task2 and task2["id"] == "t3",
      str(task2 and task2["id"]))

# 全部账号阻塞
worker._record_videos(10, C)
blocked3 = worker._blocked_accounts(now)
check("三个账号全部被阻塞", set(blocked3) == {A, B, C}, str(set(blocked3)))
check("全阻塞时取不到任务", worker._pick_pending(set(blocked3)) is None)
check("解除时刻按窗口滑出计算（有界）", all(0 < v - now <= 601 for v in blocked3.values()),
      str({k: round(v - now) for k, v in blocked3.items()}))

print("\n[5] 窗口滑出后该账号自动恢复")
reset()
worker._record_videos(10, A)
check("A 满窗口时被阻塞", A in worker._blocked_accounts(now))
with open(worker.HISTORY_FILE, "w", encoding="utf-8") as f:
    json.dump({"v": 2, "by": {A: [now - 700] * 10}}, f)
check("记录全部滑出后 A 不再阻塞", A not in worker._blocked_accounts(now))
check("A 恢复放行", worker._wait_seconds(1, now, A) == 0)

print("\n[6] 定时任务同样遵守账号跳过")
reset()
store._lock.acquire()
try:
    conn = store.connect()
    conn.execute("DELETE FROM tasks")
    conn.commit()
finally:
    store._lock.release()
store.create_task({
    "id": "sched-A", "title": "A定时稿", "account_id": A,
    "status": "waiting_schedule", "publish_at": now + 60,
    "files": [{"path": "x.mp4"}], "created_at": now + 1,
})
store.create_task({
    "id": "sched-B", "title": "B定时稿", "account_id": B,
    "status": "waiting_schedule", "publish_at": now + 60,
    "files": [{"path": "y.mp4"}], "created_at": now + 2,
})
check("未阻塞时取最早的定时稿 A", store.next_pending()["id"] == "sched-A")
worker._account_wait[A] = now + 300
t = worker._pick_pending(set(worker._blocked_accounts(now)))
check("A 节流时定时稿也跳到 B", t and t["id"] == "sched-B", str(t and t["id"]))

print("\n[7] 单账号（旧用法）行为完全不变")
reset()
worker._save_history([now - 30] * 10)
check("默认账号满窗口仍节流", worker._wait_seconds(1, now) > 0)
check("_save_history(list) 兼容", len(worker._history(now)) == 10)
worker._save_history([])
check("清空后放行", worker._wait_seconds(1, now) == 0)

print("\n" + "=" * 56)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 56)
sys.exit(1 if FAIL else 0)
