"""v106 回归：额度即时占位、队列刷新不被上传短路。

全部离线，不碰B站接口。运行：python3 tests/verify_v106.py
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

TMP = tempfile.mkdtemp(prefix="bili-v106-")
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


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   [{extra}]" if extra and not cond else ""))


def reset_ledger():
    for k in list(ledger._load().keys()):
        ledger.drop(k)


def new_task(**kw):
    cover = os.path.join(TMP, "cover.jpg")
    with open(cover, "wb") as f:
        f.write(b"\xff\xd8\xff\xd9")
    payload = {"title": "测试稿", "files": [{"path": os.path.join(TMP, "x.mp4"), "size": 1}],
               "cover_path": cover, "channel": "official", "max_attempts": 2}
    payload.update(kw)
    return store.create_task(payload)


ACCT = ""


def fake_account(cfg):
    return {"logged_in": True, "mid": 1, "uname": "u", "face": "", "vip": False,
            "level": 6, "error": ""}


# --------------------------------------------------------- 1. 账本状态机
print("\n[1] 账本：投稿一开始就占额度，成功补身份键，失败可撤回")
reset_ledger()
ledger.reserve(ACCT, "t1")
rows = ledger.recent(ACCT, 24)
check("reserve 立刻产生 1 条占位", len(rows) == 1 and rows[0]["state"] == ledger.STATE_INFLIGHT,
      str(rows))
check("占位即计入额度（没有 BV/aid 也算 1 篇）", ledger.count(ACCT) == 1)

ledger.confirm(ACCT, "t1", bvid="BV106A", aid=106)
rows = ledger.recent(ACCT, 24)
check("confirm 后仍是 1 条（占位被补成确认，不重复计数）", len(rows) == 1, str(rows))
check("confirm 带上 BV/aid", rows[0]["bvid"] == "BV106A" and rows[0]["aid"] == 106, str(rows[0]))
check("confirm 后状态为 confirmed", rows[0]["state"] == ledger.STATE_CONFIRMED)

ledger.reserve(ACCT, "t1")
rows = ledger.recent(ACCT, 24)
check("已确认的任务再占位不改状态/时间", len(rows) == 1
      and rows[0]["state"] == ledger.STATE_CONFIRMED)

ledger.record(ACCT, task_id="t2", bvid="BV106A", aid=0)
check("同 BV 的另一条记录与确认行合并", len(ledger.recent(ACCT, 24)) == 1,
      str(ledger.recent(ACCT, 24)))

ledger.reserve(ACCT, "t3")
ledger.release(ACCT, "t3")
check("release 撤掉未落地占位", ledger.count(ACCT) == 1, str(ledger.recent(ACCT, 24)))

ledger.reserve(ACCT, "t4")
ledger.mark_uncertain(ACCT, "t4")
rows = ledger.recent(ACCT, 24)
check("uncertain 保留并继续计入额度", ledger.count(ACCT) == 2
      and any(r["state"] == ledger.STATE_UNCERTAIN for r in rows))

ledger.release(ACCT, "t4")
check("release 不会回收 uncertain（可能已投出）", ledger.count(ACCT) == 2)

ledger.reserve(ACCT, "ghost")
removed = ledger.reconcile([t["id"] for t in store.list_tasks()])
check("reconcile 清掉已删除任务的孤儿占位", removed == 1 and ledger.count(ACCT) == 2,
      f"removed={removed} count={ledger.count(ACCT)}")
reset_ledger()


# --------------------------------------------------------- 2. B站入库延迟下不越界
print("\n[2] 模拟B站入库延迟：额度从「开始上传」就算，投放数量正好卡在上限")
submitted = []      # (提交时刻, bvid, 列表可见时刻)


def fake_creator_items(cfg, hours):
    now = time.time()
    return {"items": [{"bvid": b, "aid": 0, "created": ts, "status": "已发布"}
                      for ts, b, vis in submitted if vis <= now],
            "warnings": []}


limit = 50
attempts = 0
blocked_at = None
with mock.patch.object(quota, "_creator_items", fake_creator_items), \
     mock.patch.object(quota, "fetch_account", fake_account):
    for i in range(1, 60):
        q = worker.fresh_quota()
        if not q.get("ok"):
            blocked_at = i
            break
        # 模拟 worker 的一轮投稿：先占位，再上传/提交，最后确认
        tid = "lag%d" % i
        ledger.reserve(ACCT, tid)
        bv = "BV%04d" % i
        ledger.confirm(ACCT, tid, bvid=bv, aid=1000 + i)
        submitted.append((time.time(), bv, time.time() + 180))   # 3 分钟后才出现在列表
        attempts += 1
check("投放数不超过上限", attempts == limit, str(attempts))
check("第 51 篇被额度拦下", blocked_at == limit + 1, str(blocked_at))
reset_ledger()
submitted.clear()


# --------------------------------------------------------- 3. 账号键归一
print("\n[3] 额度的「未确认」读的必须是记录写的那只桶")
reset_ledger()
store.patch_config({"active_account": "accX"})
ledger.reserve("accX", "bt1")
ledger.confirm("accX", "bt1", bvid="BVBUCKET", aid=1)
with mock.patch.object(quota, "_creator_items",
                       lambda cfg, hours: {"items": [], "warnings": []}), \
     mock.patch.object(quota, "fetch_account", fake_account):
    q_none = quota.check_quota(store.load_config(), account_key=None)
    q_empty = quota.check_quota(store.load_config(), account_key="")
check("account_key=None 读到生效账号的账本", q_none["pending"] == 1, str(q_none["pending"]))
check("account_key='' 也归一到生效账号（旧版读空桶会永远为 0）",
      q_empty["pending"] == 1, str(q_empty["pending"]))
reset_ledger()
store.patch_config({"active_account": ""})


# --------------------------------------------------------- 4. 缓存路径立刻 +1
print("\n[4] 额度缓存里也要立刻看到占位，界面不会停在旧数字")
worker._quota_cache.clear()
remote48 = [{"bvid": "BV9%03d" % i, "aid": 19000 + i, "created": time.time(),
             "status": "已发布"} for i in range(48)]
with mock.patch.object(quota, "_creator_items",
                       lambda cfg, hours: {"items": list(remote48), "warnings": []}), \
     mock.patch.object(quota, "fetch_account", fake_account):
    first = worker.cached_quota(force=True)
    check("基线 48/50 可投", first["ok"] and first["count"] == 48, str(first["count"]))
    ledger.reserve(ACCT, "t9")
    again = worker.cached_quota()          # 吃缓存，但账本必须现读
    check("占位后立刻变 49/50（未跑真实上传）", again["count"] == 49,
          str(again["count"]))
    ledger.confirm(ACCT, "t9", bvid="BV106B", aid=2)
    final = worker.cached_quota()
    check("确认后仍是 49（占位换身份键，不重复加）", final["count"] == 49, str(final["count"]))
reset_ledger()
worker._quota_cache.clear()


# --------------------------------------------------------- 5. worker 收尾
print("\n[5] worker：成功=确认，提交前失败=撤回，提交阶段后失败=保留占位")
worker._stop.wait = lambda *_: None
worker._ensure_watchdog = lambda: None
worker._enqueue_archive = lambda *a, **k: None
worker._delete_after = lambda: "no"
worker.pause(False)


def ok_q():
    return {"ok": True, "count": 10, "limit": 50, "hours": 24, "confirmed": 10, "pending": 0,
            "remote_count": 10, "local_count": 0, "remote_keys": [], "logged_in": True,
            "incomplete": False, "items": [], "checked_at": time.time()}


def run(publish, task=None):
    t = task or new_task(attempts=1)
    with mock.patch.object(worker, "fresh_quota", return_value=ok_q()), \
         mock.patch.object(worker.official, "publish", side_effect=publish):
        worker._handle(t)
    return store.get_task(t["id"]), t


reset_ledger()
g, t = run(lambda *a, **k: {"bvid": "BV106C", "aid": 3})
rows = ledger.recent(ACCT, 24)
check("成功投稿：账本确认且带 BV", g["status"] == "success"
      and rows and rows[0]["state"] == ledger.STATE_CONFIRMED
      and rows[0]["bvid"] == "BV106C", str(rows))

reset_ledger()
g, t = run(RuntimeError("NetworkException: 406"))
check("提交前失败（406）：占位被撤回，不白占额度",
      g["status"] == "queued" and ledger.count(ACCT) == 0, str(ledger.recent(ACCT, 24)))

reset_ledger()


def submit_then_fail(task, cfg, log, stage):
    stage("提交稿件", 0.92)
    raise TimeoutError("提交响应超时")


g, t = run(submit_then_fail)
rows = ledger.recent(ACCT, 24)
check("提交阶段后失败：占位保留为 uncertain（稿件可能已发出）",
      len(rows) == 1 and rows[0]["state"] == ledger.STATE_UNCERTAIN, str(rows))
check("提交阶段后失败：仍计入额度", ledger.count(ACCT) == 1)
reset_ledger()


# --------------------------------------------------------- 6. 前端刷新
print("\n[6] 前端：上传期间强制刷新、入队后立刻可见")
app_js = open(os.path.join(BASE, "web", "app.js"), encoding="utf-8").read()
html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
server_py = open(os.path.join(BASE, "app", "server.py"), encoding="utf-8").read()
check("pollTasks 支持 force 绕过上传守卫", "async function pollTasks(force)" in app_js
      and "if (state.uploading && !force) return;" in app_js)
check("新任务入队后本地立刻插队显示", "function prependTasks(list)" in app_js
      and "prependTasks(res.r && res.r.tasks)" in app_js)
fin = app_js[app_js.index("state.uploading = false;"):]
check("finally 里先放掉上传标记再强制刷新",
      "state.uploading = false;" in app_js and "await pollTasks(true);" in fin)
check("队列有任务时不退到 20 秒轮询",
      't.status === "uploading" || t.status === "queued"' in app_js)
check("额度数字进入 /api/tasks 指纹（账本变了也会重建响应）",
      'q.get("count", "")' in server_py and 'q.get("pending", "")' in server_py)
ver = [ln.split('"')[1] for ln in server_py.splitlines() if "APP_VERSION =" in ln][0]
sr = html.splitlines()[7]
check("APP_VERSION 与静态资源 ?v= 同步", ver == "v106" and "?v=106" in "".join(html), ver)

print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败：" + "、".join(FAIL))
    sys.exit(1)
print("v106 回归全部通过")
