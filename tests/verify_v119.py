"""v119 回归：前端清理——修复缺陷 + 删除死代码 / 重复描述。

修复（均为纯前端，后端接口未动）：
- 本地定时提示：submitPublish 成功后 resetCompose 已清空 publishAt，原先再读它永远为空，
  「已在本地排队」提示从未显示过；现在在清表单前记下 localSchedule。
- 清空日志：DELETE /api/logs 失败没有 try/catch，Promise 未处理且界面无提示。
- Ctrl/Cmd+Enter：在编辑弹窗 / 设置抽屉的文本框里也会触发「加入投稿队列」。
- Escape：只能关抽屉，关不掉编辑弹窗。
- 编辑弹窗无封面时 src="" 会让浏览器把当前页面地址当图片请求。

清理：
- 不可达分支：filterTasks 的 "active"、分页 click 里的 "goto"/"jump"、uploadBlock 未用的 label、
  buildFormData 的 files || state.files、renderTasks 重复的 syncOverview（顺带修了计数先于概览的顺序）。
- HTML：被 CSS 隐藏多年的顶栏品牌块；自动投稿页与复选框重复的说明。
- CSS：从未被 HTML/JS 引用的选择器（旧视图开关 / 旧稿件行 chip 与 edit-btn / lg-who / rail-mini …）、
  被后置规则完全覆盖的变量定义、从未被引用的设计变量。

运行：python3 tests/verify_v119.py（需要 node，纯离线）
"""
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

# 数据目录指到临时位置：bootstrap 会读任务表，不能碰真实 data/
pkg = types.ModuleType("app")
pkg.__path__ = [os.path.join(BASE, "app")]
sys.modules["app"] = pkg
TMP = tempfile.mkdtemp(prefix="bili-v119-")
store = importlib.import_module("app.store")
store.DATA_DIR = TMP
store.DB_PATH = os.path.join(TMP, "publish.db")
store.CONFIG_PATH = os.path.join(TMP, "config.json")
store.init_db()
worker = importlib.import_module("app.worker")
worker.HISTORY_FILE = os.path.join(TMP, "upload_history.json")
worker._notify = lambda *a, **k: None

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   [{extra}]" if extra and not cond else ""))


def read(rel):
    return open(os.path.join(BASE, rel), encoding="utf-8").read()


def extract_fn(src, marker):
    """按大括号配对取出函数源码（含缩进，直接可执行）。"""
    i = src.index(marker)
    j = src.index("{", i)
    depth = 0
    while True:
        ch = src[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[i:j + 1]


js = read("web/app.js")
html = read("web/index.html")
css = read("web/style.css")

# ---------------------------------------------------------------- 1. 版本号
print("\n[1] 版本号与静态资源")
server = importlib.import_module("app.server")
tag = server.APP_VERSION.lstrip("v").replace(".", "")
check("APP_VERSION = v119", server.APP_VERSION == "v119", server.APP_VERSION)
check("静态资源 ?v=119（css/js）", f"style.css?v={tag}" in html and f"app.js?v={tag}" in html)

# ---------------------------------------------------------------- 2. 语法
print("\n[2] 语法")
r = subprocess.run(["node", "--check", os.path.join(BASE, "web", "app.js")], capture_output=True, text=True)
check("node --check web/app.js", r.returncode == 0, r.stderr.strip()[:200])

# ---------------------------------------------------------------- 3. 修复断言
print("\n[3] 修复断言")
check("本地定时方式在清表单之前记下",
      'const localSchedule = !!$("publishAt").value && $("scheduleMode").value === "local";' in js)
check("本地排队提示改用 localSchedule 且仅在真的入队时显示", "if (localSchedule && queued) {" in js)
check("提交后不再读已被清空的 publishAt", 'if ($("publishAt").value) {\n      const mode' not in js)
check("清空日志有 try/catch 与失败提示", 'toast("清空失败：" + e.message, "err");' in js)
check("Ctrl/Cmd+Enter 忽略编辑弹窗 / 设置抽屉", 'e.target.closest("#editModal, #drawer")' in js)
check("Escape 先关编辑弹窗", '$("editModal").classList.contains("open")) closeEdit();' in js)
check("无封面时移除 src 而不是写空串", 'coverImg.removeAttribute("src");' in js)
check("粘贴 Cookie 成功提示对 account 判空", '((r.account && r.account.uname) || "")' in js)
check("renderTasks 先更新计数再同步概览条",
      "if (counts) state.counts = counts;   // 先更新计数再同步概览条，避免用到上一轮的数字\n    syncOverview();\n    if (state.view"
      ' === "all") return renderArchives();' in js)
rt = extract_fn(js, "  function renderTasks(counts) {")
check("renderTasks 内只调一次 syncOverview", rt.count("syncOverview();") == 1, str(rt.count("syncOverview();")))

# ---------------------------------------------------------------- 4. 行为：filterTasks
print("\n[4] 行为：filterTasks 真跑")
HARNESS = r"""
let state = {taskFilter: ""};
__FN__
const list = [
  {id: 1, status: "queued"}, {id: 2, status: "waiting_schedule"}, {id: 3, status: "uploading"},
  {id: 4, status: "success"}, {id: 5, status: "failed"}, {id: 6, status: "blocked"},
  {id: 7, status: "cancelled"}, {id: 8, status: "interrupted"},
];
const ids = (xs) => xs.map((x) => x.id).join(",");
const run = (f) => { state.taskFilter = f; return ids(filterTasks(list)); };
const out = {
  all: run(""),
  pending: run("pending"),
  failed: run("failed"),
  uploading: run("uploading"),
  success: run("success"),
};
console.log(JSON.stringify(out));
"""
fn = extract_fn(js, "  function filterTasks(tasks) {")
tmp = os.path.join(tempfile.mkdtemp(prefix="v119-"), "filter.js")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(HARNESS.replace("__FN__", fn))
r = subprocess.run(["node", tmp], capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    print(r.stdout, r.stderr)
    raise SystemExit("node 执行 filterTasks 失败")
out = json.loads(r.stdout)
check("全部 → 原样返回", out["all"] == "1,2,3,4,5,6,7,8", out["all"])
check("待处理 → queued + waiting_schedule", out["pending"] == "1,2", out["pending"])
check("失败 → failed + blocked + cancelled + interrupted", out["failed"] == "5,6,7,8", out["failed"])
check("上传中 → 仅 uploading", out["uploading"] == "3", out["uploading"])
check("成功 → 仅 success（筛选值读 state.taskFilter）", out["success"] == "4", out["success"])

# ---------------------------------------------------------------- 5. 冗余清理
print("\n[5] 冗余清理")
for name, needle in (("filterTasks 的 active 分支", 'f === "active"'),
                     ("分页 goto 分支", 'kind === "goto"'),
                     ("分页 jump 点击分支", 'kind === "jump"'),
                     ("uploadBlock 的 label 实参", 'uploadBlock(built.fd, "'),
                     ("buildFormData 的 files || state.files", "files || state.files"),
                     ("pollTasks 里重复的暂停按钮文案", '$("btnPauseQueue").textContent = state.paused ? "继续队列" : "暂停队列";\n      // 签名')):
    check(f"JS 已删：{name}", needle not in js)
check("JS 已删： misplaced 的合集注释只剩一处", js.count("合集列表，登录后才拉得到") == 1,
      str(js.count("合集列表，登录后才拉得到")))
check("JS 注释不再叙述旧实现", "旧实现" not in js and "以前只重拉列表" not in js)

check("HTML 已删：顶栏品牌块", "brand-mark" not in html and 'class="brand"' not in html)
check("HTML 已精简：自动投稿说明不再与复选框重复",
      html.count("投成功后原件移入「已投稿」") == 1 and "绝不会上传" not in html,
      str(html.count("投成功后原件移入「已投稿」")))

dead = [".view-switch", ".vs-btn", ".lg-who", ".log-card.folded", ".rail-mini", ".rail-foot-row",
        ".pg-gap", ".self-tag", ".brand-mark", ".brand-title", ".arch-row .tbody", ".arch-row .ttl",
        ".arch-row .chip", ".edit-btn", ".topbar .brand", ".si .d.flat", ".qr-img svg",
        ".meta-line svg", ".rate-banner[hidden]", ".tag-chip[hidden]", ".login-guide[hidden]",
        ".advanced[hidden]"]
left = [d for d in dead if d in css]
check("CSS 已删：无引用的选择器", not left, "、".join(left))
n_root, n_dark = css.count(":root {"), css.count('[data-theme="dark"] {')
check("CSS 已删：被覆盖的首个 :root 色板与 v70 深色变量块", n_root == 2 and n_dark == 1,
      f"root={n_root} dark={n_dark}")
check("CSS 已删：重复的 [hidden] 兜底", css.count("[hidden] { display: none !important; }") == 1)
gone_vars = ["--accent", "--accent-soft", "--brand-hover", "--brand-ink", "--danger", "--danger-soft",
             "--run", "--run-soft", "--on-brand", "--txt", "--fs-xs", "--fs-sm", "--fs-base", "--fs-md",
             "--fs-lg", "--fs-xl", "--fs-2xl", "--fs-3xl", "--r-sm", "--r-md", "--r-xl", "--r-full"]
defined = set(re.findall(r"(--[\w-]+)\s*:", css))
check("CSS 已删：从未引用的设计变量", not [v for v in gone_vars if v in defined])
used = set(re.findall(r"var\((--[\w-]+)", css))
check("CSS 里每个已定义变量都有引用（--mono 除外）", not [v for v in defined if v not in used and v != "--mono"],
      "、".join(sorted(v for v in defined if v not in used and v != "--mono")))

# 反向护栏：CSS 选择器里的 class 必须能在 HTML/JS 里找到（lv-* 由 JS 按 level 动态拼接）
css_nc = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
sel_text = "".join(m.group(1) + "\n" for m in re.finditer(r"([^{}]+)\{", css_nc))
classes = set(re.findall(r"\.([A-Za-z_][\w-]*)", sel_text))
hay = html + "\n" + js
unused = sorted(c for c in classes if not re.search(r"(?<![\w-])" + re.escape(c) + r"(?![\w-])", hay))
check("CSS 没有新的无引用 class", set(unused) <= {"lv-error", "lv-success", "lv-warn"}, "、".join(unused))

# ---------------------------------------------------------------- 6. 页面能正常下发
print("\n[6] 页面下发")
server._account = lambda force=False: {"logged_in": False, "mid": 0, "uname": ""}
server.warm_account_faces_async = lambda: None
partitions = importlib.import_module("app.partitions")
partitions.load = lambda cfg: [(21, "日常")]
client = server.app.test_client()
page = client.get("/").get_data().decode("utf-8")
check("/ 下发 index.html 且引用 v119 资源", f"app.js?v={tag}" in page and f"style.css?v={tag}" in page)
check("/ 下发的页面不含顶栏品牌块", "brand-mark" not in page)
r = client.get("/static/app.js")
check("/static/app.js 200", r.status_code == 200 and b"localSchedule" in r.data)
r = client.get("/api/bootstrap")
body = r.get_json() or {}
check("/api/bootstrap 正常且版本 v119", body.get("version") == "v119", str(body.get("version")))

print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败：" + "、".join(FAIL))
    sys.exit(1)
print("v119 回归全部通过")
