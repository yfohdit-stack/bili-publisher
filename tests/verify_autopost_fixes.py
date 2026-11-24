"""录播自动投稿异常路径回归：重复入队、事务回滚、忽略记录、归档和标题。

全部使用临时目录与本地数据库，不访问网络。运行：python3 tests/verify_autopost_fixes.py
"""
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

TMP = tempfile.mkdtemp(prefix="bili-autopost-fixes-")
store = importlib.import_module("app.store")
store.DATA_DIR = TMP
store.DB_PATH = os.path.join(TMP, "publish.db")
store.CONFIG_PATH = os.path.join(TMP, "config.json")
store.init_db()
media = importlib.import_module("app.media")
media.probe = lambda path: {"duration": 10.0, "size": os.path.getsize(path), "error": ""}
media.have_ffmpeg = lambda: True
worker = importlib.import_module("app.worker")
worker.HISTORY_FILE = os.path.join(TMP, "upload_history.json")
autopost = importlib.import_module("app.autopost")

T0 = 1_800_000_000.0
SEQ = [0]
PASS = []
FAIL = []


def check(name, condition, extra=""):
    (PASS if condition else FAIL).append(name)
    print(("PASS " if condition else "FAIL ") + name + (f" [{extra}]" if extra and not condition else ""))


def reset():
    conn = store.connect()
    conn.execute("DELETE FROM tasks")
    conn.execute("DELETE FROM autopost_seen")
    conn.execute("DELETE FROM logs")
    conn.commit()
    autopost._sig.clear()
    autopost._warned.clear()
    autopost._status["ingested"] = 0


def setup_file(tag="single"):
    SEQ[0] += 1
    root = os.path.join(TMP, f"watch-{SEQ[0]}-{tag}")
    os.makedirs(root)
    path = os.path.join(root, "a.flv")
    with open(path, "wb") as f:
        f.write(b"aa")
    os.utime(path, (T0 - 1000, T0 - 1000))
    rule = dict(autopost.RULE_DEFAULTS)
    rule.update(id=f"r{SEQ[0]}", watch_dir=root, stable_secs=30, min_size_mb=0)
    return root, path, {"enabled": True, "scan_secs": 30, "rules": [rule]}


def ap_tasks(watch=None):
    tasks = [t for t in store.list_tasks() if t.get("autopost_meta")]
    if watch is None:
        return tasks
    return [t for t in tasks if json.loads(t["autopost_meta"]).get("watch_dir") == watch]


print("\n[1] 活动任务的源文件变化不能另建任务")
reset()
root, path, cfg = setup_file("changed")
autopost.scan_once(cfg, T0)
autopost.scan_once(cfg, T0 + 40)
with open(path, "ab") as f:
    f.write(b"bb")
os.utime(path, (T0 + 100, T0 + 100))
autopost.scan_once(cfg, T0 + 110)
autopost.scan_once(cfg, T0 + 141)
check("排队期间续写仍只有原任务", len(ap_tasks(root)) == 1, str(len(ap_tasks(root))))
task = ap_tasks(root)[0]
check("worker 将变化任务退回重判定", worker._autopost_sources_ok(task["id"], store.get_task(task["id"])) is False)
autopost.scan_once(cfg, T0 + 150, scan_new=False)
autopost.scan_once(cfg, T0 + 181, scan_new=False)
task = store.get_task(task["id"])
check("总开关关闭的新文件扫描外仍可原地完成重判定",
      len(ap_tasks(root)) == 1 and task["message"] == ""
      and task["files"][0]["src_size"] == os.path.getsize(path))
check("scan_new=False 不发现其它新文件", autopost.scan_once(cfg, T0 + 200, scan_new=False) == 0)

print("\n[2] 任务与 seen 台账必须一起提交或回滚")
reset()
root, path, cfg = setup_file("atomic")


class FailOnSeenConnection:
    def __init__(self, real):
        self.real = real

    def execute(self, sql, *args):
        if "INSERT OR REPLACE INTO autopost_seen" in sql:
            raise RuntimeError("injected seen failure")
        return self.real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.real, name)


with mock.patch("app.store.connect", side_effect=lambda: FailOnSeenConnection(store._conn)):
    autopost.scan_once(cfg, T0)
    autopost.scan_once(cfg, T0 + 40)
check("seen 写入失败后任务也回滚", not ap_tasks(root))
check("seen 写入失败后不留防重残行", store.autopost_seen_count() == 0)
autopost.scan_once(cfg, T0 + 80)
check("故障恢复后可正常入队一次", len(ap_tasks(root)) == 1)
try:
    store.create_autopost_task(
        {"title": "duplicate"},
        [{"path": path, "size": os.path.getsize(path), "mtime": os.path.getmtime(path)}],
        "duplicate")
    duplicate_blocked = False
except FileExistsError:
    duplicate_blocked = True
check("数据库事务内也会拒绝同路径第二个活动任务", duplicate_blocked and len(ap_tasks(root)) == 1)

print("\n[3] 删除任务的忽略记录不因定期清理而失效")
reset()
root, path, cfg = setup_file("prune")
autopost.scan_once(cfg, T0)
autopost.scan_once(cfg, T0 + 40)
store.delete_task(ap_tasks(root)[0]["id"])
conn = store.connect()
conn.execute("UPDATE autopost_seen SET created_at = ?", (time.time() - 31 * 86400,))
conn.commit()
check("原路径仍存在时旧忽略记录保留", store.autopost_prune(30) == 0)
check("超过 30 天也不会复活已删除任务", autopost.scan_once(cfg, T0 + 80) == 0 and not ap_tasks(root))
os.remove(path)
check("原路径消失后无用旧记录可清理", store.autopost_prune(30) == 1)

print("\n[4] 归档同名文件不覆盖，并逐文件保存路径")
reset()
root = os.path.join(TMP, "archive")
os.makedirs(os.path.join(root, autopost.ARCHIVE_DIR))
with open(os.path.join(root, autopost.ARCHIVE_DIR, "a.flv"), "w", encoding="utf-8") as f:
    f.write("old")
with mock.patch("app.autopost.time.time", return_value=1000):
    for value in ("new-1", "new-2", "new-3"):
        src = os.path.join(root, "a.flv")
        with open(src, "w", encoding="utf-8") as f:
            f.write(value)
        task = store.create_task({"title": value, "files": [{"path": src}],
                                  "autopost_meta": json.dumps({"watch_dir": root})})
        autopost.archive_originals(store.get_task(task["id"]))
archive_dir = os.path.join(root, autopost.ARCHIVE_DIR)
contents = []
for name in os.listdir(archive_dir):
    with open(os.path.join(archive_dir, name), encoding="utf-8") as f:
        contents.append(f.read())
check("同秒三次同名归档保留全部四个版本",
      len(contents) == 4 and set(contents) == {"old", "new-1", "new-2", "new-3"}, repr(contents))

root = os.path.join(TMP, "archive-partial")
os.makedirs(root)
paths = []
for name in ("one.flv", "two.flv"):
    path = os.path.join(root, name)
    with open(path, "wb") as f:
        f.write(name.encode())
    paths.append(path)
task = store.create_task({"title": "partial", "files": [{"path": p} for p in paths],
                          "autopost_meta": json.dumps({"watch_dir": root})})
real_makedirs = os.makedirs
calls = [0]


def fail_second_makedirs(*args, **kwargs):
    calls[0] += 1
    if calls[0] == 2:
        raise OSError("injected mkdir failure")
    return real_makedirs(*args, **kwargs)


with mock.patch("app.autopost.os.makedirs", side_effect=fail_second_makedirs):
    moved = autopost.archive_originals(store.get_task(task["id"]))
saved = store.get_task(task["id"])
check("第二个文件归档失败不影响第一个落库",
      moved == 1 and saved["files"][0]["path"] != paths[0]
      and os.path.isfile(saved["files"][0]["path"]))
check("失败文件仍保留原路径和原件",
      saved["files"][1]["path"] == paths[1] and os.path.isfile(paths[1]))

print("\n[5] 完整父目录分组与 80 字标题上限")
a = {"rel": "主播/游戏A/主播_2026-11-20.flv", "name": "主播_2026-11-20.flv", "mtime": 0}
b = {"rel": "主播/游戏B/主播_2026-11-20.flv", "name": "主播_2026-11-20.flv", "mtime": 1}
check("不同嵌套子目录不再合并分P", autopost._group_of(a)[0] != autopost._group_of(b)[0])
reset()
root = os.path.join(TMP, "titles")
os.makedirs(root)
rule = dict(autopost.RULE_DEFAULTS)
rule.update(id="titles", watch_dir=root, mode="multi", stable_secs=30,
            group_idle_secs=60, session_gap_secs=600, min_size_mb=0, title_tpl="题" * 100)
for name, age in (("主播_2026-11-20_10-00.flv", 5000), ("主播_2026-11-20_20-00.flv", 1000)):
    path = os.path.join(root, name)
    with open(path, "wb") as f:
        f.write(b"video")
    os.utime(path, (T0 - age, T0 - age))
cfg = {"enabled": True, "scan_secs": 30, "rules": [rule]}
autopost.scan_once(cfg, T0)
autopost.scan_once(cfg, T0 + 40)
titles = [t["title"] for t in ap_tasks(root)]
check("两场均生成带序号标题且都不超过 80 字",
      len(titles) == 2 and all(len(t) <= 80 for t in titles)
      and any(t.endswith("（2）") for t in titles), repr([len(t) for t in titles]))

print("\n[6] ffprobe 缺失时明确提示降级")
with mock.patch("app.autopost.media.have_ffmpeg", return_value=False), \
        mock.patch("app.autopost.store.add_global_log") as add_log:
    ready, _ = autopost._probe_ready({"path": path, "name": "a.flv"}, T0 + 10000)
check("缺少 ffprobe 仍可按静默期继续，但会记录明显警告",
      ready and add_log.called and "仅按静置期" in add_log.call_args.args[0])

print("\n" + "=" * 60)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + "、".join(FAIL))
    sys.exit(1)
print("自动投稿异常路径回归全部通过")
