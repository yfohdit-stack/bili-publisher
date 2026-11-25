"""v112 回归：录播自动投稿（多规则监视目录 + 分P/单独 + 模板 + 投成功归档）。

覆盖：录制完成判定（静默期+probe）、single/multi 分组、模板/账号套用、
原件引用与「已投稿」归档、清理路径护栏、seen 防重投与删除解除、多规则、
API 校验、版本号同步。全部离线，不碰B站接口。运行：python3 tests/verify_v112.py
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

TMP = tempfile.mkdtemp(prefix="bili-v112-")
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


# 离线 probe：默认容器完整、时长 10 秒
PROBE = {"duration": 10.0, "size": 1024, "error": ""}
media.probe = lambda path: dict(PROBE, path=path, exists=os.path.isfile(path))
media.have_ffmpeg = lambda: True

T0 = 1_800_000_000.0     # 固定时钟，全部用注入 now
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
              "min_size_mb": 0})
    r.update(kw)
    return r


def conf(rules, enabled=True):
    return {"enabled": enabled, "scan_secs": 30, "rules": rules}


def scan(c, now):
    return autopost.scan_once(c, now=now)


def ap_tasks():
    return [t for t in store.list_tasks() if t.get("autopost_meta")]


def rule_tasks(rid):
    return [t for t in ap_tasks() if json.loads(t["autopost_meta"])["rule_id"] == rid]


# --------------------------------------------------------- 1. 录制完成判定
print("\n[1] 录制完成判定：静默期 + probe，正在录制的绝不上传")
W = new_watch("stable")
p1 = mkfile(W, "主播A/主播A_2026-11-19_19-00-00.flv")
c = conf([rule(W, mode="single")])
check("首轮扫描不入队（静置计时刚开始）", scan(c, T0) == 0 and not ap_tasks())
check("静默期过后自动入队", scan(c, T0 + 121) == 1 and len(ap_tasks()) == 1)
t = ap_tasks()[0]
check("任务直接引用原件路径（零拷贝）", t["files"][0]["path"] == p1)
meta = json.loads(t["autopost_meta"])
check("autopost_meta 记录规则与监视目录", meta["watch_dir"] == W and meta["rule_id"] == c["rules"][0]["id"])
check("入队即登记 seen，重复扫描不再入队", scan(c, T0 + 200) == 0)

W = new_watch("recording")
mkfile(W, "主播A/正在录.flv", age=0)          # mtime=当前：还在写
c = conf([rule(W)])
scan(c, T0)
check("mtime 新鲜的文件不入队", scan(c, T0 + 50) == 0)
check("待投列表给出「仍在录制/静置中」", any("仍在录制" in x["reason"] for x in autopost.status()["pending"]))

W = new_watch("growing")
p3 = mkfile(W, "主播A/续写中.flv", age=500)
c = conf([rule(W)])
scan(c, T0)                                  # 登记签名
with open(p3, "ab") as f:                    # 模拟还在录：体积+mtime 变化
    f.write(b"\0" * 1024)
os.utime(p3, (T0 + 100, T0 + 100))
check("文件还在增长时静默计时重置、不入队", scan(c, T0 + 130) == 0)

W = new_watch("probe")
mkfile(W, "主播A/容器不完整.flv")
c = conf([rule(W)])
scan(c, T0)
with mock.patch.dict(PROBE, {"duration": 0.0}):
    check("probe 读不出时长（容器不完整）不入队", scan(c, T0 + 130) == 0)

W = new_watch("skip")
mkfile(W, "主播A/小文件.flv", mb=0.001)
mkfile(W, "主播A/下载中.flv.part")
c = conf([rule(W, min_size_mb=10)])
scan(c, T0)
check("过小文件与 .part 临时文件跳过", scan(c, T0 + 130) == 0)

# --------------------------------------------------------- 2. 分P分组
print("\n[2] 分P模式：同子文件夹+同一天合并，跨天分开，断播等齐")
W = new_watch("multi")
WM = W
rid = "r_m"
cm = conf([rule(W, id=rid, mode="multi", group_idle_secs=600)])
a1 = mkfile(W, "主播B/主播B_2026-11-19_19-00.flv", age=100)
a2 = mkfile(W, "主播B/主播B_2026-11-19_21-30.flv", age=100)   # 断播第二段，同天
mkfile(W, "主播B/主播B_2026-11-20_19-00.flv", age=100)        # 跨天
scan(cm, T0)
check("合并等待期内不入队", scan(cm, T0 + 121) == 0)
check("待投列表给出「分P合并等待中」", any("合并等待" in x["reason"] for x in autopost.status()["pending"]))
n = scan(cm, T0 + 800)                       # 静默期过 + 合并等待 600s 过
mts = rule_tasks(rid)
check("同天两段合并为一稿、跨天分开（共2稿）", n == 2 and len(mts) == 2, str(n))
day19 = next((t for t in mts if "2026-11-19" in t["title"]), None)
check("合并稿含 2 个分P且按时间排序", day19 is not None and len(day19["files"]) == 2
      and day19["files"][0]["path"] == a1 and day19["files"][1]["path"] == a2)
check("默认分P标题 = 主播 日期 录播", day19 is not None and day19["title"] == "主播B 2026-11-19 录播",
      day19["title"] if day19 else "无")

W = new_watch("multi-wait")
cm2 = conf([rule(W, id="r_m2", mode="multi")])
mkfile(W, "主播C/主播C_2026-11-19_19-00.flv")
rec = mkfile(W, "主播C/主播C_2026-11-19_20-00.flv", age=0)    # 还在录
scan(cm2, T0)
os.utime(rec, (T0 + 125, T0 + 125))                            # 持续写入
check("组内有文件未录完时整组不入队", scan(cm2, T0 + 130) == 0)

mkfile(WM, "主播B/主播B_2026-11-19_23-00.flv", age=900)        # 同组第二批（回到 multi 目录）
scan(cm, T0 + 900)
n = scan(cm, T0 + 1700)
seq = [t for t in ap_tasks() if t["title"].endswith("（2）")]
check("同组第二批稿件标题加序号（2）", n == 1 and len(seq) == 1, str(n))

store.patch_config({"max_parts": 1})
W = new_watch("split")
cm3 = conf([rule(W, id="r_m3", mode="multi")])
mkfile(W, "主播D/主播D_2026-11-19_19-00.flv")
mkfile(W, "主播D/主播D_2026-11-19_20-00.flv")
scan(cm3, T0)
check("超过 max_parts 自动拆成多个稿件", scan(cm3, T0 + 800) == 2)
store.patch_config({"max_parts": 20})

# --------------------------------------------------------- 3. 模板与账号
print("\n[3] 模板套用：标签/分区/简介/账号/标题占位符")
tpl = store.save_template("录播模板", {"tags": ["录播", "主播E"], "tid": 171,
                                      "desc": "自动投稿简介", "copyright": 1})
store.patch_config({"active_account": "acc_active"})
W = new_watch("tpl")
ct = conf([rule(W, id="r_t", mode="single", template_id=tpl["id"],
                account_id="acc_rule", title_tpl="【{主播}】{日期} {文件名}")])
mkfile(W, "主播E/主播E_2026-11-19_直播.flv")
scan(ct, T0)
check("模板规则入队成功", scan(ct, T0 + 121) == 1)
tt = next(iter(rule_tasks("r_t")), None)
check("模板标签/分区/简介套用", tt and tt["tags"] == ["录播", "主播E"] and tt["tid"] == 171
      and tt["desc"] == "自动投稿简介")
check("规则账号优先于生效账号", tt and tt["account_id"] == "acc_rule")
check("标题占位符渲染", tt and tt["title"].startswith("【主播E】2026-11-19"), tt["title"] if tt else "无")

ct2 = conf([rule(W, id="r_t2", mode="single")])
mkfile(W, "主播E/主播E_2026-11-20_直播.flv")
scan(ct2, T0 + 200)
scan(ct2, T0 + 400)
tt2 = next(iter(rule_tasks("r_t2")), None)
check("未配账号回落到当前生效账号", tt2 and tt2["account_id"] == "acc_active")
check("无模板时标签兜底「录播」", tt2 and tt2["tags"] == ["录播"])

# --------------------------------------------------------- 4. 多规则
print("\n[4] 多规则：独立目录/开关，一条失败不影响其它")
W2 = new_watch("multi-rule")
mkfile(W2, "主播F/主播F_2026-11-19.flv")
cr = conf([rule(W2, id="r_off", enabled=False),
           rule(os.path.join(TMP, "不存在"), id="r_bad"),
           rule(W2, id="r_ok")])
scan(cr, T0)
n = scan(cr, T0 + 121)
check("禁用规则跳过、坏目录报错不拦其它规则", n == 1 and len(rule_tasks("r_ok")) == 1, str(n))
check("坏目录错误记入状态", "不存在" in autopost.status()["last_error"])

# --------------------------------------------------------- 5. 投成功归档 + 清理护栏
print("\n[5] 投成功归档「已投稿」+ 上传后删除绝不碰原件")
arch_t = rule_tasks("r_ok")[0]
src = arch_t["files"][0]["path"]
moved = autopost.archive_originals(store.get_task(arch_t["id"]))
check("归档移动 1 个原件", moved == 1, str(moved))
check("原件不在原位置", not os.path.isfile(src))
newpath = store.get_task(arch_t["id"])["files"][0]["path"]
check("移入「已投稿」子文件夹且保留子目录结构",
      os.path.isfile(newpath) and os.path.join("已投稿", "主播F") in newpath, newpath)
check("任务 files 路径同步更新", newpath != src)
check("归档目录不再被扫描（重复扫描 0 入队）", scan(cr, T0 + 300) == 0)
check("重复归档返回 0", autopost.archive_originals(store.get_task(arch_t["id"])) == 0)

store.patch_config({"delete_after": "uploaded"})
before = tt2["files"][0]["path"]
removed = worker._cleanup_files(store.get_task(tt2["id"]))
check("「上传后删除」不删监视目录原件", removed == 0 and os.path.isfile(before), str(removed))
up_dir = os.path.join(TMP, "uploads", "manual_task")
os.makedirs(up_dir, exist_ok=True)
up_file = os.path.join(up_dir, "01_a.mp4")
open(up_file, "wb").write(b"\0" * 16)
manual = store.create_task({"title": "手动稿", "files": [{"path": up_file, "size": 16}]})
removed = worker._cleanup_files(store.get_task(manual["id"]))
check("程序 uploads 目录副本照常清理", removed == 1 and not os.path.isfile(up_file))
store.patch_config({"delete_after": "off"})

# --------------------------------------------------------- 6. seen 台账与删除语义
print("\n[6] seen 防重投：删除任务=放弃该稿，文件不被自动投回（v113 语义）")
store.delete_task(tt["id"])
scan(ct, T0 + 500)
check("删除任务后同文件不再自动入队", scan(ct, T0 + 700) == 0)
# p1 属于前面的稳定判定任务，不是刚删除的模板任务 tt；删除正确任务后再验证替换语义。
p1_task = next(t for t in ap_tasks() if any(f.get("path") == p1 for f in t.get("files") or []))
store.delete_task(p1_task["id"])
check("原件被替换（签名变化）视为新文件", store.autopost_is_seen(p1, 999, T0) is False)

# --------------------------------------------------------- 7. API 校验（flask test_client）
print("\n[7] API：多规则保存校验与状态接口")
server = importlib.import_module("app.server")
partitions = importlib.import_module("app.partitions")
server._account = lambda force=False: {"logged_in": False, "mid": 0, "uname": ""}
server.warm_account_faces_async = lambda: None
partitions.load = lambda cfg: [(21, "日常/日常")]
client = server.app.test_client()
r = client.post("/api/autopost", json={"enabled": True, "rules": []})
check("启用但无规则 → 400", r.status_code == 400)
r = client.post("/api/autopost", json={"enabled": True, "rules": [
    {"name": "x", "watch_dir": W2, "mode": "bad"}]})
check("非法 mode → 400", r.status_code == 400)
r = client.post("/api/autopost", json={"enabled": True, "rules": [
    {"name": "x", "watch_dir": os.path.join(TMP, "没有这个目录")}]})
check("启用规则目录不存在 → 400", r.status_code == 400)
r = client.post("/api/autopost", json={"enabled": True, "scan_secs": 30, "rules": [
    {"name": "主播A", "watch_dir": W2, "mode": "multi", "template_id": tpl["id"],
     "stable_secs": 90, "group_idle_secs": 300}]})
check("合法多规则保存 → 200", r.status_code == 200, r.get_data(as_text=True)[:120])
saved = r.get_json()
rules = saved["autopost"]["config"]["rules"]
check("规则自动补 id、参数落盘", len(rules) == 1 and rules[0]["id"] and rules[0]["stable_secs"] == 90)
r = client.get("/api/autopost")
check("GET 状态含 config/pending/recent", r.status_code == 200
      and "config" in r.get_json()["autopost"] and "recent" in r.get_json()["autopost"])
check("立即扫描接口 → 200", client.post("/api/autopost/scan").status_code == 200)
r = client.get("/api/bootstrap")
check("bootstrap 下发 autopost 状态", r.status_code == 200 and "autopost" in r.get_json())
check("任务公开字段带 autopost 标记", any(t.get("autopost") for t in r.get_json()["tasks"]))

# --------------------------------------------------------- 8. 版本号与静态接线
print("\n[8] 版本号同步与接线")
ver_num = int(server.APP_VERSION.lstrip("v").split(".")[0])
ver_tag = server.APP_VERSION.lstrip("v").replace(".", "")
check("APP_VERSION 不低于 v112", ver_num >= 112, server.APP_VERSION)
html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
check("静态资源 ?v= 与 APP_VERSION 同步",
      f"style.css?v={ver_tag}" in html and f"app.js?v={ver_tag}" in html)
check("页面含自动投稿 UI", "apRules" in html and "apSave" in html and "录播自动投稿" in html)
js = open(os.path.join(BASE, "web", "app.js"), encoding="utf-8").read()
check("前端保存/刷新/扫描函数齐全", all(k in js for k in ("apSave", "apRefresh", "apScanNow", "apCollect")))
check("队列任务显示「自动」徽章", 'title="来自录播自动投稿"' in js)
src_server = open(os.path.join(BASE, "app", "server.py"), encoding="utf-8").read()
check("create_app 启动 autopost", "autopost.start()" in src_server)
src_worker = open(os.path.join(BASE, "app", "worker.py"), encoding="utf-8").read()
check("worker 成功分支回调归档", "archive_originals" in src_worker)
check("清理带 uploads 路径护栏", "commonpath" in src_worker)

print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + "、".join(FAIL))
    sys.exit(1)
print("v112 回归全部通过")