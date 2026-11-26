"""v117.1 回归：转载稿「转载来源」字段全链路。

B站对 copyright=2 的转载稿强制要求 source，留空会报 21021。本版：
- tasks 表新增 source 列（老库自动补列）；
- 投稿参数带上 source，留空兜底「转载」；
- 前端在「转载」时才显示来源输入框，投稿/模板/编辑/恢复默认都带上它；
- 任务编辑接口的字段白名单也放行 source（否则改稿会静默丢字段）。

全部离线，不碰B站接口。运行：python3 tests/verify_v1171.py
"""
import importlib
import os
import sys
import tempfile
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

pkg = types.ModuleType("app")
pkg.__path__ = [os.path.join(BASE, "app")]
sys.modules["app"] = pkg

TMP = tempfile.mkdtemp(prefix="bili-v1171-")
store = importlib.import_module("app.store")
store.DATA_DIR = TMP
store.DB_PATH = os.path.join(TMP, "publish.db")
store.CONFIG_PATH = os.path.join(TMP, "config.json")
store.init_db()

worker = importlib.import_module("app.worker")
worker.HISTORY_FILE = os.path.join(TMP, "upload_history.json")
importlib.import_module("app.quota")
importlib.import_module("app.ledger")
importlib.import_module("app.media")
worker._notify = lambda *a, **k: None

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   [{extra}]" if extra and not cond else ""))


print("\n[1] 存储层：source 字段与老库迁移")
t = store.create_task({"title": "转载稿", "copyright": 2, "source": "某公众号 / 原作者"})
check("source 随任务落库", store.get_task(t["id"])["source"] == "某公众号 / 原作者",
      store.get_task(t["id"]).get("source"))
t0 = store.create_task({"title": "自制稿", "copyright": 1})
check("未填 source 默认空串", (store.get_task(t0["id"])["source"] or "") == "")
check("TASK_FIELDS 含 source", "source" in store.TASK_FIELDS)
store_py = open(os.path.join(BASE, "app", "store.py"), encoding="utf-8").read()
check("老库自动补 source 列", "ADD COLUMN source TEXT DEFAULT ''" in store_py)
check("表结构声明 source 列", "source       TEXT DEFAULT ''" in store_py)


print("\n[2] 投稿接口：source 透传，留空兜底「转载」")
official_py = open(os.path.join(BASE, "app", "channels", "official.py"), encoding="utf-8").read()
check("转载稿提交时带 source 参数", "source=(str(task.get(\"source\") or \"\").strip() or \"转载\")" in official_py)
entry = official_py[official_py.index("source=(str(task.get"):][:260]
check("source 兜底值是「转载」", '"转载"' in entry, entry[:120])


print("\n[3] 任务编辑接口：改稿不再丢 source")
server = importlib.import_module("app.server")
partitions = importlib.import_module("app.partitions")
server._account = lambda force=False: {"logged_in": False, "mid": 0, "uname": ""}
server.warm_account_faces_async = lambda: None
partitions.load = lambda cfg: [(21, "日常/日常")]
client = server.app.test_client()

r = client.post(f"/api/tasks/{t['id']}/action",
                json={"action": "update", "source": "改后的来源"})
check("action=update 返回 200", r.status_code == 200, r.get_data(as_text=True)[:120])
check("编辑后来源已更新", (store.get_task(t["id"])["source"] or "") == "改后的来源",
      store.get_task(t["id"]).get("source"))
body = r.get_json() or {}
check("返回的任务对象带 source", (body.get("task") or {}).get("source") == "改后的来源",
      str(body)[:120])
r = client.get("/api/tasks")
tasks = (r.get_json() or {}).get("tasks") or []
check("任务列表下发 source", all("source" in x for x in tasks), str(tasks[:1])[:120])


print("\n[4] 前端：仅转载时显示来源框，各提交路径都带上")
html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
js = open(os.path.join(BASE, "web", "app.js"), encoding="utf-8").read()
check("存在来源输入框（默认隐藏）", 'id="sourceField"' in html and 'display:none' in html)
check("输入框名为 sourceInput 且限 200 字", 'id="sourceInput"' in html and "maxlength=\"200\"" in html)
check("syncCopyright 按转载切换显示", 'sf.style.display = isReprint ? "" : "none"' in js)
check("投稿 payload 带 source", js.count("source: ($(\"sourceInput\")") >= 2,
      str(js.count("source: ($(\"sourceInput\")")))
check("模板 payload 带 source", "source: ($(\"sourceInput\") && $(\"sourceInput\").value.trim()) || \"\"" in js)
check("编辑/套用模板会回填 source", js.count('$("sourceInput").value = p.source || ""') >= 1
      and js.count('$("sourceInput").value = d.source || ""') >= 1)
check("恢复默认会清空 source", '$("sourceInput").value = ""' in js)


print("\n[5] 版本号同步")
_major, _minor = (server.APP_VERSION.lstrip("v").split(".") + ["0"])[:2]
check("APP_VERSION 不低于 v117.1", (int(_major), int(_minor)) >= (117, 1), server.APP_VERSION)
_tag = server.APP_VERSION.lstrip("v").replace(".", "")
check("静态资源 ?v= 与 APP_VERSION 同步",
      f"style.css?v={_tag}" in html and f"app.js?v={_tag}" in html)

print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败：" + "、".join(FAIL))
    sys.exit(1)
print("v117.1 回归全部通过")
