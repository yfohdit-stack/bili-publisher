"""v96 前端验证：接口限流冷却提示条（600s / 60s / 无冷却 / 深色）。

用法：python3 tests/verify_ui_v96.py [base_url] [shot_dir]
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5295"
SHOT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/v96shots"
os.makedirs(SHOT, exist_ok=True)

ok = fail = 0
errors = []
MODE = {"wait": 600}


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS", name)
    else:
        fail += 1
        print("  FAIL", name, extra)


def patch(route):
    r = route.fetch()
    try:
        d = r.json()
    except Exception:
        return route.fulfill(response=r)
    d["queue"] = dict(d.get("queue") or {})
    d["queue"]["rate_until"] = time.time() + MODE["wait"] if MODE["wait"] else 0
    d["queue"]["rate_wait"] = float(MODE["wait"] or 0)
    route.fulfill(response=r, json=d)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
    page = b.new_page(viewport={"width": 1600, "height": 1000})
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("**/api/tasks", patch)
    page.route("**/api/bootstrap", patch)

    print("=== A. 600 秒档：上传额度受限 ===")
    MODE["wait"] = 600
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(1500)
    chk("提示条可见", page.is_visible("#rateBanner"))
    title = page.inner_text("#rateTitle")
    chk("标题写明 10 分钟投稿过多", "上传额度受限" in title and "10 分钟" in title, title)
    text = page.inner_text("#rateText")
    chk("倒计时按分钟显示", "分钟" in text, text)
    chk("说明不会重投", "不会重投" in text, text)
    w = page.eval_on_selector("#rateFill", "e=>parseFloat(e.style.width)")
    chk("进度条接近满格", w > 90, str(w))
    page.screenshot(path=os.path.join(SHOT, "v96-1600-限流600秒.png"),
                    clip={"x": 0, "y": 0, "width": 1600, "height": 420})

    print("=== B. 60 秒档：封面接口过频 ===")
    MODE["wait"] = 60
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1500)
    title = page.inner_text("#rateTitle")
    chk("标题写明封面接口", "封面接口" in title, title)
    text = page.inner_text("#rateText")
    chk("倒计时按秒显示", "秒" in text, text)
    chk("60 秒档不提示稍后再点继续", "稍后" not in text, text)
    page.screenshot(path=os.path.join(SHOT, "v96-1600-限流60秒.png"),
                    clip={"x": 0, "y": 0, "width": 1600, "height": 420})

    print("=== C. 深色主题 ===")
    page.click("#btnTheme")
    page.wait_for_timeout(500)
    chk("深色下提示条仍可见", page.is_visible("#rateBanner"))
    chk("深色属性已生效",
        page.eval_on_selector("body", "e=>e.dataset.theme") == "dark"
        or page.eval_on_selector("html", "e=>e.dataset.theme") == "dark"
        or "dark" in (page.get_attribute("body", "class") or ""),
        page.eval_on_selector("body", "e=>e.dataset.theme"))
    page.screenshot(path=os.path.join(SHOT, "v96-1600-限流深色.png"),
                    clip={"x": 0, "y": 0, "width": 1600, "height": 420})

    print("=== D. 冷却结束自动隐藏 ===")
    MODE["wait"] = 0
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1500)
    chk("无冷却时提示条隐藏", not page.is_visible("#rateBanner"))
    chk("版本号为 v104", "v104" in page.inner_text("#appVer"),
        page.inner_text("#appVer"))
    chk("静态资源已升级到 v104",
        'app.js?v=104' in page.content() and 'style.css?v=104' in page.content())

    chk("无 JS 运行时错误", not errors, str(errors[:3]))
    b.close()

print("\n===== 结果：%d 通过 / %d 失败 =====" % (ok, fail))
sys.exit(1 if fail else 0)
