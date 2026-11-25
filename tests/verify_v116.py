"""v116 回归：定时发布时间只能指向未来；模板与投稿设置联动（前端静态断言）。

全部离线，不碰B站接口。运行：python3 tests/verify_v116.py
"""
import importlib
import json
import os
import sys
import tempfile
import time
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

pkg = types.ModuleType("app")
pkg.__path__ = [os.path.join(BASE, "app")]
sys.modules["app"] = pkg

TMP = tempfile.mkdtemp(prefix="bili-v116-")
store = importlib.import_module("app.store")
store.DATA_DIR = TMP
store.DB_PATH = os.path.join(TMP, "publish.db")
store.CONFIG_PATH = os.path.join(TMP, "config.json")
store.init_db()

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   [{extra}]" if extra and not cond else ""))


def read(path):
    with open(os.path.join(BASE, path), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------- 1. 后端：定时时间
print("\n[1] 任务改期：只能改到未来")
server = importlib.import_module("app.server")
server._account = lambda force=False: {"logged_in": False, "mid": 0, "uname": ""}
server.warm_account_faces_async = lambda: None
client = server.app.test_client()

task = store.create_task({"title": "定时稿", "files": [], "max_attempts": 2})
tid = task["id"]

r = client.post(f"/api/tasks/{tid}/action",
                json={"action": "update", "publish_at": time.time() - 60})
check("过去的定时时间 → 400", r.status_code == 400, str(r.status_code))
check("错误文案点明必须晚于当前时间",
      "晚于当前时间" in ((r.get_json() or {}).get("error") or ""),
      str(r.get_json()))

future = time.time() + 3600
r = client.post(f"/api/tasks/{tid}/action",
                json={"action": "update", "publish_at": future})
check("未来的定时时间 → 200", r.status_code == 200, str(r.status_code))
check("定时时间已落库",
      abs(float((store.get_task(tid) or {}).get("publish_at") or 0) - future) < 1,
      str((store.get_task(tid) or {}).get("publish_at")))

r = client.post(f"/api/tasks/{tid}/action",
                json={"action": "update", "publish_at": "明天"})
check("非数字定时时间 → 400", r.status_code == 400, str(r.status_code))

r = client.post(f"/api/tasks/{tid}/action",
                json={"action": "update", "publish_at": None})
check("清空定时时间 → 200", r.status_code == 200 and not (store.get_task(tid) or {}).get("publish_at"),
      str(r.status_code))

# ---------------------------------------------------------- 2. 投稿入口：同样的规则
print("\n[2] 投稿入口：过去的定时时间不入库")
import io

server.worker.refresh_quota = lambda **kw: {"logged_in": False, "ok": False}
server.worker.cached_quota = lambda **kw: {"logged_in": False, "ok": False}
server.partitions.load = lambda cfg: [(21, "日常/日常")]
server.partitions.ids = lambda cfg: [21]


def post_publish(publish_at):
    meta = {"title": "x", "tid": 21, "tags": ["a"], "mode": "single", "publish_at": publish_at}
    return client.post("/api/publish",
                       data={"meta": json.dumps(meta),
                             "files": (io.BytesIO(b"\x00" * 8), "a.mp4")},
                       content_type="multipart/form-data")


before = len(store.list_tasks())
r = post_publish(time.time() - 30)
check("投稿入口拒绝过去的定时时间 → 400", r.status_code == 400, str(r.status_code))
check("被拒时没有落盘任务", len(store.list_tasks()) == before, str(len(store.list_tasks())))
r = post_publish(time.time() + 3600)
check("投稿入口接受未来定时 → 200", r.status_code == 200, str(r.status_code))
check("未来定时任务已入库", len(store.list_tasks()) == before + 1, str(len(store.list_tasks())))

# ---------------------------------------------------------- 3. 前端：定时与模板
print("\n[3] 前端实现断言")
js = read("web/app.js")
html = read("web/index.html")

check("存在 syncPublishAtMin（下限随时间刷新）", "function syncPublishAtMin" in js)
check("存在 publishAtError（提交前校验）", "function publishAtError" in js)
check("定时输入框带「只能选未来时间」提示", "只能选未来时间" in html)
check("聚焦时刷新下限", 'addEventListener("focus", syncPublishAtMin)' in js)
check("提交前调用 publishAtError", "const paErr = publishAtError()" in js)

check("存在 markTemplateDirty（改动后脱离模板）", "function markTemplateDirty" in js)
check("禁止转载进入模板 payload", "no_reprint: $(\"noReprint\")" in js)
check("套用模板时恢复禁止转载", '$("noReprint").checked = !!p.no_reprint' in js)
check("从模板页套用时对齐下拉", '$("tplSelect").value = String(t.id)' in js)

ver = "v" + str(int(server.APP_VERSION.lstrip("v")))
check("静态资源 ?v= 与 APP_VERSION 同步",
      f"?v={ver.lstrip('v')}" in html and ver == server.APP_VERSION, server.APP_VERSION)

print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + "、".join(FAIL))
    sys.exit(1)
