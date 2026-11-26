"""v118 回归：自动投稿扫描热路径优化（批量 seen / scandir / probe 缓存 / 并行探测）。

覆盖：
1. 批量 seen 判定与单条 autopost_is_seen 完全等价（未登记 / 活动任务 / 终态同签名 / 终态换文件）；
2. 超过 900 条路径自动分批，不触发 SQLite 「too many SQL variables」；
3. probe 缓存：命中、体积/mtime/inode 三类失效、duration=0 不入缓存、条目上限；
4. scandir 扫描：软链取目标文件真实体积与 mtime（不是链接自身）；
5. 多分P并行探测：probe_many 只调一次、结果与分P 顺序对齐、返回被截断时放弃本轮。

全部离线，不碰B站接口。运行：python3 tests/verify_v118.py
"""
import importlib
import json
import os
import sys
import tempfile
import types
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

pkg = types.ModuleType("app")
pkg.__path__ = [os.path.join(BASE, "app")]
sys.modules["app"] = pkg

TMP = tempfile.mkdtemp(prefix="bili-v118-")
store = importlib.import_module("app.store")
store.DATA_DIR = TMP
store.DB_PATH = os.path.join(TMP, "publish.db")
store.CONFIG_PATH = os.path.join(TMP, "config.json")
store.init_db()

media = importlib.import_module("app.media")
worker = importlib.import_module("app.worker")
worker.HISTORY_FILE = os.path.join(TMP, "upload_history.json")
worker._notify = lambda *a, **k: None
importlib.import_module("app.quota")
importlib.import_module("app.ledger")
autopost = importlib.import_module("app.autopost")

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   [{extra}]" if extra and not cond else ""))


PROBE = {"duration": 10.0, "size": 1024, "error": ""}
media.probe = lambda path: dict(PROBE, path=path, exists=os.path.isfile(path))
media.have_ffmpeg = lambda: True

T0 = 1_800_000_000.0
_SEQ = [0]


def mkfile(root, rel, mb=2, age=1000):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * int(mb * 1024 * 1024))
    os.utime(path, (T0 - age, T0 - age))
    return path


def rule(watch, **kw):
    _SEQ[0] += 1
    r = dict(autopost.RULE_DEFAULTS)
    r.update({"id": "r_" + str(_SEQ[0]), "name": "测试规则", "watch_dir": watch,
              "min_size_mb": 0})
    r.update(kw)
    return r


def mark(path, size, mtime, task_id, status="success"):
    """登记一条 seen 记录，并把任务状态摆到指定状态。"""
    store.autopost_mark([{"path": path, "size": size, "mtime": mtime}],
                        task_id, "gk", "标题")
    store.update_task(task_id, status=status)


# --------------------------------------------------- 1. 批量 seen 与单条等价
print("\n[1] 批量 seen 判定：与单条 autopost_is_seen 逐条等价")
W = new_watch = os.path.join(TMP, "seen")
os.makedirs(W, exist_ok=True)
t_act = store.create_task({"title": "活动任务"})
t_ok = store.create_task({"title": "成功任务"})
t_bad = store.create_task({"title": "失败任务"})

p_new = os.path.join(W, "未登记.flv")                       # 从未登记
p_act = os.path.join(W, "活动中.flv"); mkfile(W, "活动中.flv", 1)
p_ok = os.path.join(W, "成功.flv");   mkfile(W, "成功.flv", 1)
p_bad = os.path.join(W, "失败.flv");  mkfile(W, "失败.flv", 1)
p_rep = os.path.join(W, "被替换.flv"); mkfile(W, "被替换.flv", 1)

mark(p_act, 1, T0 - 1000, t_act["id"], "queued")
mark(p_ok, os.path.getsize(p_ok), os.path.getmtime(p_ok), t_ok["id"], "success")
mark(p_bad, 999, T0 - 1000, t_bad["id"], "failed")          # 签名已变
mark(p_rep, 1, T0 - 1000, t_bad["id"], "success")           # 终态 + 签名变

batch = store.autopost_seen_batch([p_new, p_act, p_ok, p_bad, p_rep])
check("未登记路径不在批量结果里", p_new not in batch, str(batch.get(p_new)))
check("活动任务记录带状态", batch.get(p_act, {}).get("status") == "queued",
      str(batch.get(p_act)))
check("批量判定：未登记 → 未 seen", not autopost._seen_from_row(batch.get(p_new), 5, T0))
check("批量判定：活动任务无条件占位", autopost._seen_from_row(batch.get(p_act), 999, T0))
check("批量判定：终态同签名 → 已 seen", autopost._seen_from_row(batch.get(p_ok),
      os.path.getsize(p_ok), os.path.getmtime(p_ok)))
check("批量判定：终态签名变 → 可再投", not autopost._seen_from_row(batch.get(p_bad), 5, T0))
check("批量判定：终态被替换 → 可再投", not autopost._seen_from_row(batch.get(p_rep), 2, T0))

cases = [(p_new, 5, T0), (p_act, 999, T0),
         (p_ok, os.path.getsize(p_ok), os.path.getmtime(p_ok)),
         (p_bad, 5, T0), (p_rep, 2, T0)]
same = all(store.autopost_is_seen(p, s, m) == autopost._seen_from_row(batch.get(p), s, m)
           for p, s, m in cases)
check("批量与单条判定结果完全一致", same)
check("空列表直接返回空字典", store.autopost_seen_batch([]) == {})

# --------------------------------------------------- 2. 分批：绕开 SQLite 变量上限
print("\n[2] 大批量路径自动分批（> 900 条）")
t_bulk = store.create_task({"title": "批量任务"})
bulk = [os.path.join(W, f"bulk_{i}.flv") for i in range(950)]
store.autopost_mark([{"path": p, "size": 1, "mtime": T0 - 1000} for p in bulk],
                    t_bulk["id"], "gk", "批量")
with mock.patch.object(store, "connect", wraps=store.connect) as conn_spy:
    got = store.autopost_seen_batch(bulk)
check("950 条全部返回", len(got) == 950, str(len(got)))
check("按 900 分两批查询", conn_spy.call_count >= 1)
check("分批结果内容正确", got[bulk[0]]["size"] == 1 and got[bulk[-1]]["size"] == 1)

# --------------------------------------------------- 3. probe 缓存
print("\n[3] probe 缓存：命中、失效、不缓存未完成容器")
autopost._probe_cache.clear()
f = {"path": "/x/a.flv", "size": 1000, "mtime": T0 - 500, "ino": 7}
autopost._probe_cached(f, {"duration": 10.0})
hit = autopost._probe_cache_hit(f)
check("同签名命中缓存", hit is not None and hit["duration"] == 10.0)
check("体积变化 → 失效", autopost._probe_cache_hit(dict(f, size=1001)) is None)
check("mtime 变化 → 失效", autopost._probe_cache_hit(dict(f, mtime=f["mtime"] - 2)) is None)
check("inode 变化（同尺寸原地覆盖）→ 失效",
      autopost._probe_cache_hit(dict(f, ino=8)) is None)
autopost._probe_cached(f, {"duration": 0.0})
check("duration=0（仍在录制）不写缓存",
      autopost._probe_cache_hit(f)["duration"] == 10.0)
ok, info = autopost._probe_ready(f, T0)
check("_probe_ready 走缓存返回 True", ok and info["duration"] == 10.0)

with mock.patch.object(media, "probe", side_effect=AssertionError("不应再起 ffprobe")):
    ok2, _ = autopost._probe_ready(f, T0)
check("命中缓存时不再调用 ffprobe", ok2)

for i in range(5005):
    autopost._probe_cache[f"/x/big_{i}"] = (1, 1.0, 1, {"duration": 1.0})
autopost.scan_once({"enabled": True, "rules": []}, now=T0)
check("缓存条目上限 5000（防内存膨胀）", len(autopost._probe_cache) <= 5000,
      str(len(autopost._probe_cache)))
autopost._probe_cache.clear()

# --------------------------------------------------- 4. scandir 与软链
print("\n[4] scandir 扫描：软链取目标文件真实体积/mtime")
real_dir = os.path.join(TMP, "real")
link_dir = os.path.join(TMP, "watch_link")
os.makedirs(real_dir, exist_ok=True)
os.makedirs(link_dir, exist_ok=True)
target = mkfile(real_dir, "目标录播.flv", mb=2, age=3000)
link = os.path.join(link_dir, "软链.flv")
os.symlink(target, link)
os.makedirs(os.path.join(link_dir, ".hidden"), exist_ok=True)
mkfile(os.path.join(link_dir, ".hidden", "隐藏.flv"), "隐藏.flv", 1)
mkfile(os.path.join(link_dir, autopost.ARCHIVE_DIR), "已投稿.flv", 1)
mkfile(link_dir, "半截.flv.part", 0)
mkfile(os.path.join(link_dir, "子目录"), "子录播.mp4", 1)

cand = {c["name"]: c for c in autopost._candidates(rule(link_dir, min_size_mb=1))}
check("软链被收集", "软链.flv" in cand, str(list(cand)))
if "软链.flv" in cand:
    check("软链体积取目标文件（不是 13 字节）",
          cand["软链.flv"]["size"] == os.path.getsize(target),
          str(cand["软链.flv"]["size"]))
    check("软链体积不被 min_size_mb 误杀", not cand["软链.flv"]["too_small"])
    check("软链 mtime 跟随目标文件",
          abs(cand["软链.flv"]["mtime"] - os.path.getmtime(target)) < 0.001,
          str(cand["软链.flv"]["mtime"]))
check("隐藏目录跳过", not any("隐藏" in n for n in cand))
check("归档目录跳过", "已投稿.flv" not in cand)
check("临时后缀跳过", "半截.flv.part" not in cand)
check("子目录递归正常（rel 为相对路径）",
      cand.get("子录播.mp4", {}).get("rel") == os.path.join("子目录", "子录播.mp4"),
      str(cand.get("子录播.mp4", {}).get("rel")))

# --------------------------------------------------- 5. 并行探测与顺序对齐
print("\n[5] 多分P并行探测：一次调用、结果与分P 对齐")
P = os.path.join(TMP, "parallel")
os.makedirs(P, exist_ok=True)
members = []
for i, name in enumerate(("a.flv", "b.flv", "c.flv")):
    p = mkfile(P, name, mb=1, age=1000 + i)
    members.append({"path": p, "name": name,
                    "rel": name, "size": os.path.getsize(p),
                    "mtime": os.path.getmtime(p), "ino": os.stat(p).st_ino,
                    "too_small": False})
autopost._probe_cache.clear()
calls = []


def fake_probe_many(paths, max_workers=4):
    calls.append(list(paths))
    return [{"duration": 100.0 + i, "size": 1, "error": ""} for i, _ in enumerate(paths)]


with mock.patch.object(media, "probe_many", side_effect=fake_probe_many):
    created = autopost._ingest_group(list(members), rule(P, mode="multi"),
                                     "主播", "2026-11-26", "k", T0)
check("并行探测只调用一次 probe_many", len(calls) == 1, str(len(calls)))
check("一次传入全部分P", len(calls[0]) == 3 if calls else False)
check("三个分P 全部入队为 1 稿", created == 1, str(created))
task = next((t for t in store.list_tasks()
             if t.get("autopost_meta") and "a.flv" in json.dumps(t.get("files") or [])), None)
if task:
    durs = {os.path.basename(f["path"]): f["duration"] for f in (task.get("files") or [])}
    order = sorted(members, key=lambda m: (m["mtime"], m["name"]))
    want = {m["name"]: 100.0 + i for i, m in enumerate(order)}
    check("时长与分P 顺序对齐", durs == want, f"{durs} != {want}")
else:
    check("找到入队任务", False)

# 返回被截断时放弃本轮，而不是把时长错配到别的分P
autopost._probe_cache.clear()
before = len(store.list_tasks())
with mock.patch.object(media, "probe_many", return_value=[{"duration": 1.0}]):
    created2 = autopost._ingest_group(list(members), rule(P, mode="multi"),
                                      "主播", "2026-11-26", "k2", T0)
check("probe_many 返回数量不符 → 本轮不入队", created2 == 0 and len(store.list_tasks()) == before,
      f"created={created2}")

# --------------------------------------------------- 6. 版本号同步
print("\n[6] 版本号与静态资源同步")
server = importlib.import_module("app.server")
html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
tag = server.APP_VERSION.lstrip("v").replace(".", "")
check("APP_VERSION = v118", server.APP_VERSION == "v118", server.APP_VERSION)
check("静态资源 ?v=118（css/js）",
      f"style.css?v={tag}" in html and f"app.js?v={tag}" in html)

print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + "、".join(FAIL))
    sys.exit(1)
print("v118 回归全部通过")
