"""v111 回归：横幅优先级——暂停态优先，限流冷却只作背景信息。

修复前 `left > 0` 的限流冷却分支排在最前：
- reason=rate（连续限流、需人工恢复）时，冷却倒计时没走完的那几分钟会显示
  「预计 X 后自动继续投稿」——实际不会自动恢复，v108 的 rate 横幅被挡；
- reason=cover/quota 时，只要还有任一账号残留未过期的 rate_until，暂停横幅同样被挡。

这里的做法是**真的把 renderRateBanner 抽出来跑**：从 web/app.js 里按括号配对
取出该函数源码，套一层极简 DOM/state 桩，用 node 跑 7 个场景断言实际输出。

运行：python3 tests/verify_v111.py（需要 node，纯离线）
"""
import json
import os
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(BASE, "web", "app.js")

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   [{extra}]" if extra and not cond else ""))


def extract_fn(src, marker="  function renderRateBanner() {"):
    """从 app.js 里按大括号配对取出函数源码（含缩进，直接可执行）。"""
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


HARNESS = r"""
let els = {};
function $(id) {
  if (!els[id]) els[id] = {hidden: true, textContent: "", style: {width: ""}, dataset: {}};
  return els[id];
}
function humanWait(s) { const n = Math.max(1, Math.round(s)); return n + " 秒"; }
let state = {};
__FN__
const NOW = 1000;
const queued2 = [{status: "queued"}, {status: "queued"}, {status: "success"}];
const scenarios = __SC__;
const out = [];
for (const sc of scenarios) {
  els = {};
  state = Object.assign({tasks: queued2, rateUntil: 0, rateWait: 60,
                          coverResumeAt: 0, quotaResumeAt: 0, quotaBanUntil: 0}, sc.state);
  const realNow = Date.now;
  Date.now = () => NOW * 1000;
  const box0 = $("rateBanner");
  renderRateBanner();
  Date.now = realNow;
  out.push({
    name: sc.name, hidden: !!box0.hidden, mode: box0.dataset.mode || "",
    title: (els["rateTitle"] || {}).textContent || "",
    text: (els["rateText"] || {}).textContent || "",
    fill: (els["rateFill"] || {style: {}}).style.width || "",
  });
}
console.log(JSON.stringify(out));
"""

SCENARIOS = [
    {"name": "限流冷却（队列未暂停，应承诺自动继续）",
     "state": {"paused": False, "pauseReason": "", "rateUntil": 1120, "rateWait": 300}},
    {"name": "连续限流已暂停 + 冷却残留",
     "state": {"paused": True, "pauseReason": "rate", "rateUntil": 1120, "rateWait": 300}},
    {"name": "连续限流已暂停，无冷却",
     "state": {"paused": True, "pauseReason": "rate", "rateUntil": 0}},
    {"name": "封面暂停 + 其它账号冷却残留",
     "state": {"paused": True, "pauseReason": "cover", "coverResumeAt": 1300,
               "rateUntil": 1120, "rateWait": 300}},
    {"name": "额度暂停 + 其它账号冷却残留",
     "state": {"paused": True, "pauseReason": "quota", "quotaResumeAt": 1300,
               "rateUntil": 1120, "rateWait": 300}},
    {"name": "手动暂停 + 冷却残留",
     "state": {"paused": True, "pauseReason": "manual", "rateUntil": 1120, "rateWait": 60}},
    {"name": "一切正常",
     "state": {"paused": False, "pauseReason": "", "rateUntil": 0}},
]


def run_harness():
    src = open(APP_JS, encoding="utf-8").read()
    fn = extract_fn(src)
    js = HARNESS.replace("__FN__", fn).replace(
        "__SC__", json.dumps(SCENARIOS, ensure_ascii=False))
    tmp = os.path.join(tempfile.mkdtemp(prefix="banner-"), "banner.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(js)
    r = subprocess.run(["node", tmp], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        raise SystemExit("node 执行横幅逻辑失败")
    return {o["name"]: o for o in json.loads(r.stdout)}


print("\n[1] 真实跑 renderRateBanner：7 个场景")
out = run_harness()

o = out["限流冷却（队列未暂停，应承诺自动继续）"]
check("未暂停 + 冷却 → 显示冷却倒计时", not o["hidden"] and o["mode"] == "rate", str(o))
check("未暂停时才承诺自动继续", "自动继续投稿" in o["text"], o["text"])

o = out["连续限流已暂停 + 冷却残留"]
check("rate 暂停不被冷却挡住", o["title"].startswith("连续触发"), o["title"])
check("rate 暂停绝不出现「自动继续投稿」", "自动继续投稿" not in o["text"], o["text"])
check("rate 暂停明确要人工恢复", "需人工核对" in o["text"], o["text"])
check("rate 暂停用红色样式", o["mode"] == "ban", o["mode"])
check("残留冷却只作背景说明", "冷却约" in o["text"] and "但队列仍等人工恢复" in o["text"], o["text"])

o = out["连续限流已暂停，无冷却"]
check("无冷却时不再提冷却", "冷却" not in o["text"], o["text"])
check("无冷却时标题仍是连续限流", o["title"].startswith("连续触发"), o["title"])

o = out["封面暂停 + 其它账号冷却残留"]
check("cover 暂停不被冷却挡住", "封面被 B站 拒收" in o["title"], o["title"])
check("cover 横幅保留「5 分钟自动重试」", "暂停 5 分钟" in o["text"], o["text"])
check("cover 横幅附注其它账号冷却", "另有账号仍在限流冷却" in o["text"], o["text"])
check("cover 横幅不承诺整队自动继续", "自动继续投稿" not in o["text"], o["text"])

o = out["额度暂停 + 其它账号冷却残留"]
check("quota 横幅优先于冷却", "24h 投稿额度" in o["title"], o["title"])
check("quota 横幅附注冷却", "另有账号仍在限流冷却" in o["text"], o["text"])

o = out["手动暂停 + 冷却残留"]
check("manual 横幅优先于冷却", "队列已暂停" in o["title"], o["title"])
check("manual 用中性灰样式", o["mode"] == "manual", o["mode"])
check("manual 不收 any 错误承诺", "自动继续投稿" not in o["text"], o["text"])

o = out["一切正常"]
check("无暂停无冷却 → 横幅隐藏", o["hidden"] is True, str(o))


print("\n[2] 结构断言（防止以后又把冷却分支挪回最前）")
src = open(APP_JS, encoding="utf-8").read()
first_paused = src.index("if (state.paused) {")
first_left = src.index("if (left > 0) {", first_paused)
check("暂停分支排在 left>0 分支之前", first_paused < first_left)
rate_block = src[src.index('if (reason === "rate") {'):src.index('if (reason === "cover") {')]
check("rate 暂停分支里没有「自动继续投稿」字样",
      "自动继续投稿" not in rate_block, rate_block[:120])

server_py = open(os.path.join(BASE, "app", "server.py"), encoding="utf-8").read()
html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
ver = [ln.split('"')[1] for ln in server_py.splitlines() if "APP_VERSION =" in ln][0]
check("静态资源 ?v= 与 APP_VERSION 同步", ("?v=" + ver.lstrip("v")) in html, ver)
check("大版本不低于 v111", int(ver.lstrip("v")) >= 111, ver)

print("\n" + "=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败：" + "、".join(FAIL))
    sys.exit(1)
print("v111 回归全部通过")
