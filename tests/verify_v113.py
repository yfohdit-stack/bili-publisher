"""v113 回归：自动投稿六项修复。

1 Windows 跨盘 commonpath ValueError 不再炸清理；2 自动投稿迁到左侧独立视图；
3 删除任务不再解除 seen（删了不会被投回）；4 入队签名+上传前复核+扫描重判定；
5 分P按场次间隔切段；6 seen 台账定期清理。全部离线。运行：python3 tests/verify_v113.py
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

TMP = tempfile.mkdtemp(prefix="bili-v113-")
store = importlib.import_module("app.store")
store.DATA_DIR = TMP
store.DB_PATH = os.path.join(TMP, "publish.db")
store.CONFIG_PATH = os.path.join(TMP, "config.json")
store.init_db()

media = importlib.import_module("app.media")
worker = importlib.import_module("app.worker")
worker.HISTORY_FILE = os.path.join(TMP, "upload_history.json")
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


def new_watch(tag):
    _SEQ[0] += 1
    d = os.path.join(TMP, f"w{_SEQ[0]}-{tag}")
    os.makedirs(d, exist_ok=True)
    return d


def mkfile(root, rel, mb=2, age=1000):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * int(mb * 1024 * 1024))
    os.utime(path, (T0 - age, T0 - age))
    return path


def rule(watch, **kw):
    r = dict(autopost.RULE_DEFAULTS)
    r.update({"id": "r_" + str(_SEQ[0]), "name": "测试规则", "watch_dir": watch,
              "min_size_mb": 0, "stable_secs": 30})
    r.update(kw)
    return r


def conf(rules):
    return {"enabled": True, "scan_secs": 30, "rules": rules}


def ap_tasks():
    return [t for t in store.list_tasks() if t.get("autopost_meta")]


def tasks_of(watch):
    return [t for t in ap_tasks()
            if (json.loads(t["autopost_meta"]) or {}).get("watch_dir") == watch]


# --------------------------------------------------------- 1. 跨盘清理护栏
print("\n[1] Windows 跨盘：commonpath 抛 ValueError 时清理不炸、不删原件")
W = new_watch("crossdrive")
p = mkfile(W, "主播A/录播.flv")
t = store.create_task({"title": "跨盘稿", "files": [{"path": p, "size": os.path.getsize(p)}],
                       "autopost_meta": json.dumps({"watch_dir": W})})
with mock.patch("os.path.commonpath", side_effect=ValueError("Paths don't have the same drive")):
    try:
        n = worker._cleanup_files(store.get_task(t["id"]))
        ok = (n == 0 and os.path.isfile(p))
    except ValueError:
        ok = False
check("跨盘 ValueError 被吞掉、原件保留、返回 0", ok)

# --------------------------------------------------------- 2. 删除语义
print("\n[2] 删除任务=放弃该稿：seen 保留，文件不被自动投回")
W = new_watch("delete")
p = mkfile(W, "主播A/录播.flv")
c = conf([rule(W)])
autopost.scan_once(c, now=T0)
autopost.scan_once(c, now=T0 + 40)
ts = tasks_of(W)
check("文件正常入队", len(ts) == 1)
store.delete_task(ts[0]["id"])
autopost.scan_once(c, now=T0 + 100)
n = autopost.scan_once(c, now=T0 + 200)
check("删除任务后扫描不再入队（删得掉）", n == 0 and not tasks_of(W), str(n))
src_server = open(os.path.join(BASE, "app", "server.py"), encoding="utf-8").read()
check("delete/clear_done 不再调用 autopost_unmark", "autopost_unmark" not in src_server)

# --------------------------------------------------------- 3. 源文件签名复核
print("\n[3] 入队签名 + 上传前复核 + 扫描重判定")
W = new_watch("sig")
p = mkfile(W, "主播A/录播.flv")
c = conf([rule(W)])
autopost.scan_once(c, now=T0)
autopost.scan_once(c, now=T0 + 40)
t = tasks_of(W)[0]
f0 = t["files"][0]
check("入队时写入 src_size/src_mtime 签名",
      f0.get("src_size") == os.path.getsize(p) and abs(f0.get("src_mtime", 0) - (T0 - 1000)) < 1)
check("签名一致时复核通过", worker._autopost_sources_ok(t["id"], store.get_task(t["id"])) is True)

with open(p, "ab") as f:                      # 模拟录播软件续写
    f.write(b"\0" * 4096)
os.utime(p, (T0 + 2000, T0 + 2000))
check("源文件变化时复核拦下", worker._autopost_sources_ok(t["id"], store.get_task(t["id"])) is False)
t = store.get_task(t["id"])
check("退回 queued 并标「源文件已变化，等待重新判定」",
      t["status"] == "queued" and "源文件已变化" in t["message"], t["status"] + "/" + t["message"])

autopost.scan_once(c, now=T0 + 2100)          # 重判定第一轮：登记新签名
t = store.get_task(t["id"])
check("仍在静置期时不更新签名", "源文件已变化" in t["message"])
autopost.scan_once(c, now=T0 + 2131)          # 静默期 30s 过 → 原地更新
t = store.get_task(t["id"])
check("录完后原地更新签名、清空提示（不产生重复任务）",
      t["message"] == "" and t["files"][0]["src_size"] == os.path.getsize(p)
      and len(tasks_of(W)) == 1, t["message"])
check("更新后复核通过可继续投", worker._autopost_sources_ok(t["id"], t) is True)

os.remove(p)
check("源文件被删 → 判失败而非硬传",
      worker._autopost_sources_ok(t["id"], store.get_task(t["id"])) is False
      and store.get_task(t["id"])["status"] == "failed")

# --------------------------------------------------------- 4. 场次切段
print("\n[4] 分P按场次间隔切段：下午场+晚场不硬合并")
W = new_watch("session")
c = conf([rule(W, id="r_s", mode="multi", session_gap_secs=7200, group_idle_secs=600)])
mkfile(W, "主播X/主播X_2026-11-19_14-00.flv", age=20000)   # 下午场
mkfile(W, "主播X/主播X_2026-11-19_20-00.flv", age=10000)   # 晚场第一段（差 10000s > 7200）
mkfile(W, "主播X/主播X_2026-11-19_20-30.flv", age=9000)    # 晚场断播第二段（差 1000s）
autopost.scan_once(c, now=T0)
n = autopost.scan_once(c, now=T0 + 40)
sts = [t for t in ap_tasks() if (json.loads(t["autopost_meta"]) or {}).get("rule_id") == "r_s"]
check("同天两场切成 2 稿", n == 2 and len(sts) == 2, str(n))
sizes = sorted(len(t["files"]) for t in sts)
check("下午场 1P、晚场 2P（断播分段仍合并）", sizes == [1, 2], str(sizes))
check("第二场标题带序号（2）", any(t["title"].endswith("（2）") for t in sts))

W = new_watch("session-merge")
c = conf([rule(W, id="r_s2", mode="multi", session_gap_secs=7200)])
mkfile(W, "主播Y/主播Y_2026-11-19_19-00.flv", age=5000)
mkfile(W, "主播Y/主播Y_2026-11-19_20-00.flv", age=1400)    # 差 3600s < 7200
autopost.scan_once(c, now=T0)
n = autopost.scan_once(c, now=T0 + 800)
sts = [t for t in ap_tasks() if (json.loads(t["autopost_meta"]) or {}).get("rule_id") == "r_s2"]
check("间隔未超阈值仍合并为一稿 2P", n == 1 and len(sts) == 1 and len(sts[0]["files"]) == 2, str(n))

# --------------------------------------------------------- 5. seen 台账清理
print("\n[5] seen 台账：任务已不存在且超 30 天的行被清理")
now = time.time()
conn = store.connect()
conn.execute("INSERT INTO autopost_seen (path,size,mtime,task_id,group_key,title,created_at) "
             "VALUES ('/gone/old.flv',1,0,'ghost_old','', '', ?)", (now - 40 * 86400,))
conn.execute("INSERT INTO autopost_seen (path,size,mtime,task_id,group_key,title,created_at) "
             "VALUES ('/gone/new.flv',1,0,'ghost_new','', '', ?)", (now - 1 * 86400,))
keep = store.create_task({"title": "老任务还在"})
conn.execute("INSERT INTO autopost_seen (path,size,mtime,task_id,group_key,title,created_at) "
             "VALUES ('/keep/old.flv',1,0,?,'', '', ?)", (keep["id"], now - 40 * 86400))
conn.commit()
removed = store.autopost_prune(30)
left = {r["path"] for r in conn.execute("SELECT path FROM autopost_seen").fetchall()}
check("只清「任务不存在且超 30 天」的行", removed == 1 and "/gone/old.flv" not in left)
check("新行与任务仍在的老行保留", "/gone/new.flv" in left and "/keep/old.flv" in left)
src_ap = open(os.path.join(BASE, "app", "autopost.py"), encoding="utf-8").read()
check("扫描线程每小时触发清理", "autopost_prune(30)" in src_ap and "_last_prune" in src_ap)

# --------------------------------------------------------- 6. 左侧独立视图
print("\n[6] 自动投稿迁到左侧导航独立视图")
html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
js = open(os.path.join(BASE, "web", "app.js"), encoding="utf-8").read()
css = open(os.path.join(BASE, "web", "style.css"), encoding="utf-8").read()
check("左侧导航有「自动投稿」入口", 'data-view="autopost"' in html and "自动投稿</span>" in html)
check("独立视图区块 viewAutopost 存在", 'id="viewAutopost"' in html)
check("设置抽屉不再塞自动投稿（apRules 全页仅一处）", html.count('id="apRules"') == 1)
check("STUDIO_VIEWS/META 注册 autopost", '"autopost"' in js and "AutoPost" in js)
check("视图切换显隐与数据加载接线", 'view !== "autopost"' in js and 'view === "autopost"' in js)
check("CSS 隐藏其它视图主体", 'body[data-view="autopost"] .shell > .wb-grid' in css)
check("规则卡片含场次间隔字段", "ap-r-gap" in js and "session_gap_secs" in js)

# --------------------------------------------------------- 7. API 与版本
print("\n[7] API 校验与版本号")
server = importlib.import_module("app.server")
partitions = importlib.import_module("app.partitions")
server._account = lambda force=False: {"logged_in": False, "mid": 0, "uname": ""}
server.warm_account_faces_async = lambda: None
partitions.load = lambda cfg: [(21, "日常/日常")]
client = server.app.test_client()
W = new_watch("api")
r = client.post("/api/autopost", json={"enabled": True, "rules": [
    {"name": "x", "watch_dir": W, "session_gap_secs": 100}]})
check("场次间隔越界 → 400", r.status_code == 400)
r = client.post("/api/autopost", json={"enabled": True, "rules": [
    {"name": "x", "watch_dir": W, "session_gap_secs": 3600}]})
check("合法场次间隔保存 → 200", r.status_code == 200
      and r.get_json()["autopost"]["config"]["rules"][0]["session_gap_secs"] == 3600)
check("APP_VERSION 不低于 v114", int(server.APP_VERSION.lstrip("v")) >= 114, server.APP_VERSION)
check("静态资源 ?v= 与 APP_VERSION 同步",
      f"style.css?v={int(server.APP_VERSION.lstrip('v'))}" in html
      and f"app.js?v={int(server.APP_VERSION.lstrip('v'))}" in html)

print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + "、".join(FAIL))
    sys.exit(1)
print("v113 回归全部通过")