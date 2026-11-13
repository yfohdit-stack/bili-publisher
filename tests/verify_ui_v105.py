"""v105 UI 校验：确认 biliup 设置项已彻底消失、页面无 JS 报错、设置可正常保存。

用法：python3 tests/verify_ui_v105.py [base_url]
默认 http://127.0.0.1:5277，需要 5277 已在跑。
"""
import sys
import json
import urllib.request

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5277"
passed, failed = 0, 0


def chk(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def api(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    print("\n=== A. 前端资源里已无 biliup ===")
    for f in ("index.html", "app.js", "style.css"):
        body = urllib.request.urlopen(f"{BASE}/static/{f}", timeout=20).read().decode("utf-8", "ignore")
        chk(f"{f} 不含 biliup", "biliup" not in body.lower(), "")
    chk("index.html 版本号 v105", "?v=105" in urllib.request.urlopen(f"{BASE}/static/index.html", timeout=20).read().decode("utf-8", "ignore"))

    print("\n=== B. bootstrap 只下发官方通道 ===")
    bs = api("/api/bootstrap")
    chk("version = v105", bs.get("version") == "v105", str(bs.get("version")))
    chk("env 无 biliup 键", "biliup" not in (bs.get("env") or {}), str(list((bs.get("env") or {}).keys())))
    chk("env 有 official / ffmpeg", {"official", "ffmpeg"} <= set((bs.get("env") or {}).keys()))
    cfg = bs.get("config") or {}
    chk("config 无 biliup_* 键", not any(k.startswith("biliup_") for k in cfg), str([k for k in cfg if "biliup" in k]))
    chk("config.channel = official", cfg.get("channel") == "official", str(cfg.get("channel")))

    print("\n=== C. 设置面板已移除上传通道/biliup 控件 ===")
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/local/bin/chromium", args=["--no-sandbox"])
        page = b.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        for eid in ("cfgChannel", "cfgBiliupBin", "cfgBiliupLine", "cfgBiliupCookie",
                    "cfgBiliupCookieAccount", "biliupIdentityHint", "envBiliup"):
            el = page.evaluate("id => !!document.getElementById(id)", eid)
            chk(f"#{eid} 已不存在", el is False, f"仍有 {eid}")

        body_text = page.evaluate("() => document.body.innerText.toLowerCase()")
        chk("页面文本不含 biliup", "biliup" not in body_text, "")
        chk("上传线路选择器仍在", page.evaluate("() => !!document.getElementById('cfgUploadLine')") is True)
        chk("环境行：官方接口 / ffmpeg 仍在",
            page.evaluate("() => !!document.getElementById('envOfficial') && !!document.getElementById('envFfmpeg')") is True)

        # 打开设置并保存一次，确认不再引用已删除的控件
        page.evaluate("() => document.querySelector('[data-tab=\"settings\"], #tabSettings, .nav-item[data-view=\"settings\"]')?.click()")
        page.wait_for_timeout(600)
        save = page.evaluate("() => { const b = [...document.querySelectorAll('button')].find(x => /保存/.test(x.textContent)); if (b) { b.click(); return true; } return false; }")
        chk("找到保存按钮", bool(save), "")
        page.wait_for_timeout(2500)
        chk("保存全程无 JS 报错", not errors, str(errors[:3]))
        b.close()

    print("\n=== D. 保存后配置仍无 biliup ===")
    cfg2 = api("/api/bootstrap").get("config") or {}
    chk("保存后 config 无 biliup_* 键", not any(k.startswith("biliup_") for k in cfg2), str([k for k in cfg2 if "biliup" in k]))
    chk("保存后 channel = official", cfg2.get("channel") == "official", str(cfg2.get("channel")))
    chk("保存后 upload_line 仍在", "upload_line" in cfg2, str(cfg2.get("upload_line")))

    print(f"\n===== 结果：{passed} 通过 / {failed} 失败 =====")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
