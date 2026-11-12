"""离线验证脚本：覆盖中断恢复、上传校验、额度严格模式、节流窗口、进度节流、定时提前量。

不碰B站接口，全部用假数据。运行：python3 tests/verify_fixes.py
（需要 flask / segno，装完 requirements.txt 即可）
"""
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根目录
sys.path.insert(0, BASE)

# 用假的 app 包，绕开 app/__init__.py（它会 import server → flask）
pkg = types.ModuleType("app")
pkg.__path__ = [os.path.join(BASE, "app")]
sys.modules["app"] = pkg

TMP = tempfile.mkdtemp(prefix="bili-test-")
store = importlib.import_module("app.store")
store.DATA_DIR = TMP
store.DB_PATH = os.path.join(TMP, "publish.db")
store.CONFIG_PATH = os.path.join(TMP, "config.json")
store.init_db()

worker = importlib.import_module("app.worker")
worker.HISTORY_FILE = os.path.join(TMP, "upload_history.json")
quota = importlib.import_module("app.quota")

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   [{extra}]" if extra and not cond else ""))


# ---------------------------------------------------------------- 3. 中断恢复
print("\n[3] 进程被杀后的任务恢复")
t1 = store.create_task({"title": "上传中被强杀", "files": [{"path": "/tmp/x.mp4", "size": 1}]})
store.update_task(t1["id"], status="uploading", progress=0.4, stage="上传 40%")
t2 = store.create_task({"title": "正常排队", "files": []})
n = worker.recover_interrupted()
got = store.get_task(t1["id"])
check("uploading → interrupted", got["status"] == "interrupted", got["status"])
check("不自动重排（错误码 interrupted）", got["error"] == "interrupted")
check("提示里要求先核对创作中心", "创作中心" in got["message"])
check("queued 任务不受影响", store.get_task(t2["id"])["status"] == "queued")
check("recover 返回标记数量", n == 1, str(n))
check("next_queued 取不到中断任务", (store.next_queued() or {}).get("id") == t2["id"])

# 看门狗：updated_at 很久以前的 uploading 才判卡死
t3 = store.create_task({"title": "卡死的上传", "files": []})
store.update_task(t3["id"], status="uploading")
worker._watchdog()
check("刚更新的 uploading 不被误判", store.get_task(t3["id"])["status"] == "uploading")
# update_task 会强制刷新 updated_at，这里直接改库模拟「很久没动静」
# 持久共享连接：测试里直接用，不能 close（close 后整个进程的连接都失效）
_conn = store.connect()
_conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?",
              (time.time() - worker.STUCK_TIMEOUT - 5, t3["id"]))
_conn.commit()
worker._watchdog()
st = store.get_task(t3["id"])
check("超时 uploading 判卡死", st["status"] == "failed" and st["error"] == "stuck", st["status"])
check("卡死提示要求先核对", "创作中心" in st["message"], st["message"])

# ---------------------------------------------------------------- 7. 节流窗口
print("\n[7] 10 分钟窗口节流（多分P 不再绕过）")
now = time.time()
worker._save_history([])
check("空窗口立即放行", worker._wait_seconds(1, now) == 0)
check("空窗口 + 12 分P 也放行", worker._wait_seconds(12, now) == 0)

worker._save_history([now - 30] * 9)
check("窗口内 9 个 + 单P → 放行", worker._wait_seconds(1, now) == 0)
w = worker._wait_seconds(2, now)
check("窗口内 9 个 + 2 分P → 要等", w > 0, str(w))
check("等待时长 ≈ 570 秒", 500 < w <= 600, str(w))

worker._save_history([now - 30] * 10)
w10 = worker._wait_seconds(1, now)
check("窗口内 10 个 + 单P → 要等", w10 > 0, str(w10))
worker._save_history([now - 30] * 12)
w12 = worker._wait_seconds(1, now)
check("窗口内 12 个 → 等最老 3 条滑出", w12 > 0, str(w12))
check("12 条时等待时长与 10 条一致（都等最老那条）", abs(w12 - w10) <= 1, f"{w10} vs {w12}")

worker._save_history([now - 700] * 20)
check("全部滑出窗口 → 放行", worker._wait_seconds(5, now) == 0)
worker._save_history([now - 30] * 3)
worker._record_videos(4)
h = worker._history(now)
check("_record_videos 按分P数记账", len(h) == 7, str(len(h)))
check("窗口外记录被清掉", all(t > now - worker.WINDOW_SECS for t in h))

# ---------------------------------------------------------------- 9. 定时（v103：B站定时最小提前量 10 分钟）
print("\n[9] 定时发布：B站定时最小提前量 10 分钟（平台实际只要求 5 分钟）")
check("B站定时最小提前量为 10 分钟", worker.SCHEDULE_MIN_AHEAD == 10 * 60,
      str(worker.SCHEDULE_MIN_AHEAD))

# 本地到点自动上传（不交给B站定时）
now = time.time()
local_far = {"publish_at": now + 3 * 86400, "schedule_mode": "local"}
local_huge = {"publish_at": now + 20 * 86400, "schedule_mode": "local"}
local_past = {"publish_at": now - 5, "schedule_mode": "local"}
bili_huge = {"publish_at": now + 20 * 86400, "schedule_mode": "bili"}
bili_mid = {"publish_at": now + 5 * 3600, "schedule_mode": "bili"}
bili_near = {"publish_at": now + 2 * 3600, "schedule_mode": "bili"}
bili_soon = {"publish_at": now + 5 * 60, "schedule_mode": "bili"}
check("本地定时 3 天后仍等待", worker._schedule_action(local_far) == "wait")
check("本地定时不受 15 天限制", worker._schedule_action(local_huge) == "wait")
check("本地定时到点即放行", worker._schedule_action(local_past) == "go")
check("B站定时超 15 天判失败", worker._schedule_action(bili_huge) == "fail")
check("B站定时 5 小时交B站", worker._schedule_action(bili_mid) == "go")
check("B站定时 2 小时也交B站（提前量 > 10 分钟）", worker._schedule_action(bili_near) == "go")
check("B站定时 5 分钟本地候到点", worker._schedule_action(bili_soon) == "wait")
check("剩余时间格式化", "天" in worker._fmt_remain(3 * 86400 + 20)
      and "小时" in worker._fmt_remain(5400), worker._fmt_remain(5400))

# 远的定时稿不会被提前取走，到点前 10 分钟才进入候选（直接验证 waiting_schedule 选取规则）
def _waiting_due(secs):
    r = store.connect().execute(
        "SELECT id FROM tasks WHERE status='waiting_schedule' AND publish_at <= ? "
        "ORDER BY publish_at ASC LIMIT 1", (time.time() + secs,)).fetchone()
    return r["id"] if r else None

tw = store.create_task({"title": "本地定时", "files": [], "schedule_mode": "local"})
store.update_task(tw["id"], status="waiting_schedule", publish_at=now + 3 * 86400)
check("远任务不被提前取走", _waiting_due(600) != tw["id"])
store.update_task(tw["id"], publish_at=now + 300)
check("临近 10 分钟被取走", _waiting_due(600) == tw["id"])
store.delete_task(tw["id"])

# 过期保护：重启后过期超过阈值不自动投
store.patch_config({"overdue_guard": False, "overdue_minutes": 30})
check("保护关闭时过期照投",
      worker._schedule_action({"publish_at": now - 40 * 60, "schedule_mode": "local"}) == "go")
store.patch_config({"overdue_guard": True})
check("过期 10 分钟未超阈值放行",
      worker._schedule_action({"publish_at": now - 10 * 60, "schedule_mode": "local"}) == "go")
check("过期 40 分钟超阈值拦下",
      worker._schedule_action({"publish_at": now - 40 * 60, "schedule_mode": "local"}) == "block")
check("B站定时模式同样受保护",
      worker._schedule_action({"publish_at": now - 40 * 60, "schedule_mode": "bili"}) == "block")
store.patch_config({"overdue_guard": False})


check("体积估算提前量已移除", not hasattr(worker, "_schedule_lead"))
check("取消偏差告知常量", not hasattr(worker, "DRIFT_WARN"))
check("store 恢复 next_pending()", hasattr(store, "next_pending"))

# ---------------------------------------------------------------- 8. 进度节流
print("\n[8] 进度落库 / 日志节流")
writes, logs = [], []
_orig_upd, _orig_log = store.update_task, store.add_log
store.update_task = lambda tid, **f: writes.append(f)
store.add_log = lambda tid, msg, level="info": logs.append(msg)
try:
    worker._reset_stage("taskA")
    for i in range(2000):                     # 模拟一个大视频的 2000 次分块回调
        worker._stage("taskA", f"上传 {i * 0.05:.1f}% · 3.2 MB/s", 0.1 + i * 0.00045, 3.2)
    n_write, n_log = len(writes), len(logs)
    check("2000 次回调 → 落库约 100 次（1% 一档）", n_write <= 120, f"{n_write} 次")
    check("落库次数下降一个数量级", n_write * 10 < 2000, f"{n_write}/2000")
    check("同一阶段只写一条日志", n_log == 1, f"{n_log} 条")
    writes.clear(); logs.clear()
    worker._stage("taskA", "上传封面", 0.05, force=False)
    check("阶段切换立即落库并留痕（进度回退也认）",
          len(writes) == 1 and len(logs) == 1, f"{writes}/{logs}")
    writes.clear(); logs.clear()
    worker._stage("taskA", "稿件已提交，等待审核", 0.98)
    worker._stage("taskA", "完成", 1.0)
    check("progress=1.0 强制落库", any(w.get("progress") == 1.0 for w in writes), str(writes))
    writes.clear(); logs.clear()
    worker._stage("taskA", "加入合集", 0.99, force=True)
    check("force=True 一定落库 + 留痕", len(writes) == 1 and len(logs) == 1)
finally:
    store.update_task, store.add_log = _orig_upd, _orig_log

# ---------------------------------------------------------------- 6. 额度严格模式
print("\n[6] 额度统计不全时不再 fail-open")
_orig_once = quota._creator_items_once
_orig_fetch = quota.fetch_account
quota.fetch_account = lambda cfg: {"logged_in": True, "mid": 1, "uname": "u", "face": "",
                                   "vip": False, "level": 5, "error": ""}
calls = {"n": 0}


def fake_incomplete(cfg, hours):
    calls["n"] += 1
    return {"items": [{"title": "a", "bvid": "BV1", "created": time.time(),
                       "length": "", "status": "已发布", "state": 0}],
            "warnings": ["审核中列表没取到（超时）"]}


quota._creator_items_once = fake_incomplete
try:
    r = quota.check_quota({"quota_limit": 50, "quota_hours": 24, "quota_strict": True})
    check("严格模式：数据不全 → 拦下", r["ok"] is False, json.dumps(r, ensure_ascii=False)[:120])
    check("严格模式：标 incomplete", r.get("incomplete") is True)
    check("严格模式：错误里给了出口", "刷新额度" in r["error"] and "严格模式" in r["error"], r["error"])
    check("抓不全时会重试一轮", calls["n"] == 2, str(calls["n"]))
    r2 = quota.check_quota({"quota_limit": 50, "quota_hours": 24, "quota_strict": False})
    check("宽松模式：只提示、照常放行", r2["ok"] is True and r2.get("incomplete") is True)
    r3 = quota.check_quota({"quota_limit": 1, "quota_hours": 24, "quota_strict": True})
    check("真超限时仍按超限报错", r3["ok"] is False and "达到上限" in r3["error"], r3["error"])
finally:
    quota._creator_items_once = _orig_once
    quota.fetch_account = _orig_fetch

# ---------------------------------------------------------------- 5. 上传校验
print("\n[5] 上传前置校验")
server = importlib.import_module("app.server")
server.store.DATA_DIR = TMP
server.UPLOAD_DIR = os.path.join(TMP, "uploads")
server.COVER_DIR = os.path.join(TMP, "covers")
os.makedirs(server.UPLOAD_DIR, exist_ok=True)
os.makedirs(server.COVER_DIR, exist_ok=True)
server.store.init_db = lambda: None
server.worker.start = lambda: None
server.partitions.ids = lambda cfg: [21, 17]
server.partitions.load = lambda cfg, force=False: [(21, "生活/日常"), (17, "游戏/单机游戏")]

client = server.app.test_client()
cfg = {"max_file_mb": 1, "max_parts": 2}


class FakeFile:
    def __init__(self, filename, size):
        self.filename = filename
        self._size = size

    def save(self, path):
        with open(path, "wb") as f:
            f.write(b"\0" * self._size)


class FakeReq:
    content_length = 0


def with_req(fn, files, content_length=0, cfg=cfg):
    orig = server.request
    server.request = FakeReq()
    server.request.content_length = content_length
    try:
        return fn(files, cfg)
    finally:
        server.request = orig


# 扩展名
try:
    with_req(server._check_videos, [FakeFile("剧本.txt", 10)])
    check("非视频扩展名被拒", False)
except server.UploadRejected as e:
    check("非视频扩展名被拒", "不是受支持的视频格式" in str(e), str(e))
# 分P 数量
try:
    with_req(server._check_videos, [FakeFile(f"{i}.mp4", 10) for i in range(3)])
    check("分P 超上限被拒", False)
except server.UploadRejected as e:
    check("分P 超上限被拒", "最多 2 个分P" in str(e), str(e))
# 整包体积
try:
    with_req(server._check_videos, [FakeFile("a.mp4", 10)], content_length=50 * 1024 * 1024)
    check("整包超上限被拒", False)
except server.UploadRejected as e:
    check("整包超上限被拒", "上传体积过大" in str(e), str(e))
# 默认 8GB 上限下，正常大小的稿件不受影响（余量改成按比例后不能被误伤）
check("默认上限下 50MB 稿件照常通过",
      with_req(server._check_videos, [FakeFile("a.mp4", 10)],
               content_length=50 * 1024 * 1024,
               cfg={"max_file_mb": 8192, "max_parts": 20}) == 8192 * 1024 * 1024)
# 磁盘余量（上限放宽，确保是磁盘检查先触发）
_orig_free = server._disk_free
server._disk_free = lambda: 10 * 1024 * 1024
try:
    with_req(server._check_videos, [FakeFile("a.mp4", 10)], content_length=5 * 1024 * 1024,
             cfg={"max_file_mb": 8192, "max_parts": 20})
    check("磁盘不足被拒", False)
except server.UploadRejected as e:
    check("磁盘不足被拒", "磁盘空间不够" in str(e), str(e))
finally:
    server._disk_free = _orig_free
# 正常放行
check("合法请求通过校验", with_req(server._check_videos, [FakeFile("v.mp4", 10)]) == 1024 * 1024)
# 图片校验
try:
    server._check_image(FakeFile("cover.exe", 10))
    check("非图片封面被拒", False)
except server.UploadRejected:
    check("非图片封面被拒", True)

# 端到端：/api/publish 传一个 .txt → 400 且不留残留目录
before = set(os.listdir(server.UPLOAD_DIR))
resp = client.post("/api/publish", data={
    "meta": json.dumps({"mode": "single", "tid": 21, "tags": ["日常"]}),
    "files": (io.BytesIO(b"hello"), "note.txt"),
}, content_type="multipart/form-data")
body = resp.get_json()
after = set(os.listdir(server.UPLOAD_DIR))
check("POST /api/publish 拒绝 .txt", resp.status_code == 400 and body.get("ok") is False,
      f"{resp.status_code} {body}")
check("错误信息能直接给用户看", "视频格式" in (body or {}).get("error", ""), str(body))
check("被拒后没有残留文件", before == after, str(after - before))

# 端到端：合法 mp4 → 建任务成功
resp2 = client.post("/api/publish", data={
    "meta": json.dumps({"mode": "single", "tid": 21, "tags": ["日常"]}),
    "files": (io.BytesIO(b"\0" * 2048), "第一集.mp4"),
}, content_type="multipart/form-data")
body2 = resp2.get_json()
check("合法 mp4 正常建任务", resp2.status_code == 200 and body2.get("ok") is True,
      f"{resp2.status_code} {str(body2)[:150]}")
if body2.get("ok"):
    check("文件名中文名保留", "第一集" in body2["tasks"][0]["files"][0]["name"],
          str(body2["tasks"][0]["files"]))

# 端到端：整包检查放过、但单文件落盘后才发现超限 → 拒绝并清理残留
_orig_cv = server._check_videos
server._check_videos = lambda files, cfg: 1024 * 1024     # 假装单文件上限是 1MB
try:
    before2 = set(os.listdir(server.UPLOAD_DIR))
    r = client.post("/api/publish", data={
        "meta": json.dumps({"mode": "single", "tid": 21, "tags": ["日常"]}),
        "files": (io.BytesIO(b"\0" * (2 * 1024 * 1024)), "big.mp4"),
    }, content_type="multipart/form-data")
    b = r.get_json()
    after2 = set(os.listdir(server.UPLOAD_DIR))
    check("单文件落盘后超限被拒",
          r.status_code == 400 and "超过单文件上限" in (b or {}).get("error", ""), str(b))
    check("超限文件已清理，不留残留", before2 == after2, str(after2 - before2))
finally:
    server._check_videos = _orig_cv

# ---------------------------------------------------------------- 一键磁盘清理
print("\n[清理] 成功任务副本 + 孤儿文件")
up_dir = os.path.join(TMP, "uploads")
os.makedirs(os.path.join(up_dir, "ok"), exist_ok=True)
ok_file = os.path.join(up_dir, "ok", "v.mp4")
with open(ok_file, "wb") as f:
    f.write(b"\0" * 4096)
tk = store.create_task({"title": "已成功", "files": [{"path": ok_file, "name": "v.mp4", "size": 4096}]})
store.update_task(tk["id"], status="success", bvid="BV1TEST")

# 排队中任务的文件必须保留
os.makedirs(os.path.join(up_dir, "wait"), exist_ok=True)
wait_file = os.path.join(up_dir, "wait", "q.mp4")
with open(wait_file, "wb") as f:
    f.write(b"\0" * 2048)
tq = store.create_task({"title": "排队中", "files": [{"path": wait_file, "name": "q.mp4", "size": 2048}]})

# 孤儿文件：没有任何任务引用
orphan = os.path.join(up_dir, "ghost.flv")
with open(orphan, "wb") as f:
    f.write(b"\0" * 1024)

r = worker.cleanup_now()
check("成功任务文件已删除", not os.path.exists(ok_file))
check("成功任务标记 cleanup=done", store.get_task(tk["id"])["cleanup"] == "done")
check("孤儿文件已删除", not os.path.exists(orphan))
check("排队任务文件保留", os.path.exists(wait_file))
check("释放字节数正确", r["bytes_freed"] == 5120, str(r["bytes_freed"]))
check("重复清理不误删", worker.cleanup_now()["bytes_freed"] == 0)
store.delete_task(tk["id"]); store.delete_task(tq["id"])

# 提前上传：waiting_schedule 任务立即变 queued 且清掉定时时间
_tu = store.create_task({"title": "提前上传", "files": [], "schedule_mode": "local"})
store.update_task(_tu["id"], status="waiting_schedule", publish_at=time.time() + 3 * 86400)
_r = client.post("/api/tasks/" + _tu["id"] + "/action", json={"action": "upload_now"})
_after = store.get_task(_tu["id"])
check("提前上传接口成功", _r.status_code == 200 and _r.get_json()["ok"])
check("提前上传后变 queued 且无定时", _after["status"] == "queued" and not _after["publish_at"])
check("非等待任务不能提前上传",
      client.post("/api/tasks/" + _tu["id"] + "/action", json={"action": "upload_now"}).status_code == 400)
store.delete_task(_tu["id"])

# 事件驱动：wake 唤醒标记
worker._wake.clear()
check("初始唤醒标记为空", not worker._wake.is_set())
worker.wake()
check("wake 后标记置位", worker._wake.is_set())
worker._wake.clear()

# 空闲等待时长：临近定时任务应给出短超时，无任务时封顶 30 秒
_tw2 = store.create_task({"title": "空闲超时", "files": [], "schedule_mode": "local"})
store.update_task(_tw2["id"], status="waiting_schedule", publish_at=time.time() + 120)
check("临近任务给出短超时", worker._idle_timeout(0) <= 15, str(worker._idle_timeout(0)))
check("空闲超时不超过 30 秒", worker._idle_timeout(-100) <= 30)
store.delete_task(_tw2["id"])

# 看门狗按需启动：默认无看门狗线程，ensure 后才拉起
worker._watchdog_thread = None
check("空闲时无看门狗线程", worker._watchdog_thread is None)
worker._ensure_watchdog()
check("ensure 后看门狗在跑",
      worker._watchdog_thread is not None and worker._watchdog_thread.is_alive())

# set_message 不刷 updated_at
_tm = store.create_task({"title": "免刷时间", "files": []})
_before = store.get_task(_tm["id"])["updated_at"]
time.sleep(0.02)
store.set_message(_tm["id"], "只改文案")
_after = store.get_task(_tm["id"])
check("set_message 不刷 updated_at", _after["updated_at"] == _before and _after["message"] == "只改文案")
store.delete_task(_tm["id"])

# ---------------------------------------------------------------- 账号切换立即换 cookie
print("\n[账号] 切换生效账号时顶层 cookie 同步交换")
_c1 = {"sessdata": "sess1", "bili_jct": "jct1", "buvid3": "buv1", "dedeuserid": "111"}
_c2 = {"sessdata": "sess2", "bili_jct": "jct2", "buvid3": "buv2", "dedeuserid": "222"}
_acc_cfg = store._merge(store.load_config(), {
    "accounts": [
        {"id": "acc_111", "mid": 111, "uname": "甲", "face": "",
         "cookie": dict(_c1), "added_at": time.time()},
        {"id": "acc_222", "mid": 222, "uname": "乙", "face": "",
         "cookie": dict(_c2), "added_at": time.time()},
    ],
    "active_account": "acc_111", "cookie": dict(_c1),
})
store.save_config(_acc_cfg)
store.set_active_account("acc_222")
_sw = store.load_config()      # 命中内存缓存
check("切换后 active_account 更新", _sw["active_account"] == "acc_222")
check("切换后顶层 cookie 立即交换",
      _sw["cookie"]["dedeuserid"] == "222" and _sw["cookie"]["sessdata"] == "sess2")
check("切换后 account 信息同步",
      _sw["account"]["mid"] == 222 and _sw["account"]["uname"] == "乙")
store.set_active_account("acc_111")
check("切回后顶层 cookie 还原", store.load_config()["cookie"]["dedeuserid"] == "111")
try:
    store.set_active_account("acc_nope")
    check("未知账号抛 KeyError", False)
except KeyError:
    check("未知账号抛 KeyError", True)

# ---------------------------------------------------------------- 收尾
print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + "；".join(FAIL))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
