"""v89 前端验证：概览条完整数字 / 日增 / 下钻筛选 / 溢出。

用法：python3 tests/verify_ui_v89.py [base_url] [shot_dir]
需要 playwright + chromium（仅开发验证用，不影响软件运行）。
"""
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3100"
SHOT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/v89shots"
os.makedirs(SHOT, exist_ok=True)

ok = fail = 0
errors = []


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS", name)
    else:
        fail += 1
        print("  FAIL", name, extra)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/local/bin/chromium", args=["--no-sandbox"])
    page = b.new_page(viewport={"width": 1600, "height": 1000})
    console = []
    page.on("console", lambda m: console.append((m.type, m.text)))
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(1200)

    print("=== A. 工作台：完整数字 + 日增 ===")
    chk("视图为 workbench", page.eval_on_selector("body", "e=>e.dataset.view") == "workbench")
    nums = page.eval_on_selector_all("#ov0,#ov1,#ov2,#ov3", "els=>els.map(e=>e.textContent)")
    labels = page.eval_on_selector_all("#statStrip .si .l", "els=>els.map(e=>e.textContent)")
    chk("4 格为播放/点赞/投币/收藏", labels == ["播放数", "点赞数", "投币数", "收藏数"], str(labels))
    chk("数字为完整千分位、无万/亿", all(("万" not in n and "亿" not in n) for n in nums), str(nums))
    # 不写死真实数据：任何账号、任何数值都必须是完整数字（可带千分位，禁止万/亿）
    chk("播放数为完整数字（可带千分位）",
        bool(re.fullmatch(r"\d{1,3}(,\d{3})*|\d+", (nums[0] or "").strip())), str(nums))
    incr = page.eval_on_selector_all("#ovd0,#ovd1,#ovd2,#ovd3",
                                     "els=>els.map(e=>({t:e.textContent,h:e.hidden,c:e.className}))")
    # 不写死「本次真实为 0」：只校验「没值才隐藏、有值必须显示」，不依赖当天真实增量
    chk("增量为 0 或缺失时不占位（有值才显示）",
        all(i["h"] == (not i["t"].strip()) for i in incr), str(incr))

    print("=== A2. 日增展示（模拟 +12 / 0 / -3 / 缺失）===")
    ctx = b.new_context(viewport={"width": 1600, "height": 1000})
    mp = ctx.new_page()
    mock = {"ok": True, "cached": False, "day": "",
            "note": "mock", "items": {"play": 1234567, "like": 62, "coin": 44, "fav": 0},
            "incr": {"play": 12, "like": 0, "coin": -3, "fav": None}}
    mp.route("**/api/account/stats?part=workbench*",
             lambda route: route.fulfill(status=200, content_type="application/json",
                                         body=json.dumps(mock)))
    mp.goto(BASE + "/", wait_until="networkidle")
    mp.wait_for_timeout(1200)
    mnum = mp.eval_on_selector_all("#ov0,#ov1,#ov2,#ov3", "els=>els.map(e=>e.textContent)")
    chk("百万级数字完整显示 + 千分位", mnum[0] == "1,234,567", str(mnum))
    chk("不出现万/亿缩写", all("万" not in n and "亿" not in n for n in mnum), str(mnum))
    mi = mp.eval_on_selector_all("#ovd0,#ovd1,#ovd2,#ovd3",
                                 "els=>els.map(e=>({t:e.textContent,h:e.hidden,c:e.className,"
                                 "col:getComputedStyle(e).color}))")
    chk("+12 显示且为涨色", mi[0]["t"] == "+12" and "up" in mi[0]["c"] and not mi[0]["h"], str(mi[0]))
    chk("0 不显示", mi[1]["h"], str(mi[1]))
    chk("-3 显示且为跌色", mi[2]["t"] == "-3" and "down" in mi[2]["c"] and not mi[2]["h"], str(mi[2]))
    chk("缺失字段不显示", mi[3]["h"], str(mi[3]))
    chk("涨跌颜色不同", mi[0]["col"] != mi[2]["col"], str((mi[0]["col"], mi[2]["col"])))
    chk("七位数字仍不溢出",
        not mp.eval_on_selector("#ov0", "e=>e.scrollWidth>e.clientWidth+1"))
    mp.screenshot(path=os.path.join(SHOT, "v89-1600-日增.png"), full_page=True)
    ctx.close()

    print("=== A3. 超长数字（万亿级）不缩写、不溢出 ===")
    ctx4 = b.new_context(viewport={"width": 1366, "height": 900})
    p4 = ctx4.new_page()
    big = {"ok": True, "cached": False, "day": "", "note": "",
           "items": {"play": 1234567890123, "like": 98765432, "coin": 44, "fav": 0},
           "incr": {"play": 0, "like": 0, "coin": 0, "fav": 0}}
    p4.route("**/api/account/stats?part=workbench*",
             lambda route: route.fulfill(status=200, content_type="application/json",
                                         body=json.dumps(big)))
    p4.goto(BASE + "/", wait_until="networkidle")
    p4.wait_for_timeout(1000)
    t = p4.text_content("#ov0")
    chk("万亿级数字完整显示（仍千分位）", t == "1,234,567,890,123", t)
    chk("超长数字触发更小字号", p4.eval_on_selector("#ov0", "e=>e.classList.contains('num-xxlong')"))
    chk("1366 下万亿数字不溢出", not p4.eval_on_selector("#ov0", "e=>e.scrollWidth>e.clientWidth+1"))
    p4.screenshot(path=os.path.join(SHOT, "v89-1366-万亿数字.png"), full_page=True)
    ctx4.close()

    print("=== B. 概览格可点击（工作台 → B站数据页）===")
    chk("格子有 pointer 光标",
        page.eval_on_selector("#ovSlot0", "e=>getComputedStyle(e).cursor") == "pointer")
    chk("格子有下钻 title",
        "B站" in (page.get_attribute("#ovSlot0", "title") or ""))
    with page.expect_popup() as pop:
        page.click("#ovSlot0")
    popup = pop.value
    chk("点击播放格打开 B站创作中心", "member.bilibili.com" in popup.url, popup.url)
    popup.close()

    # 大屏溢出检查
    def overflow(sel_all):
        return page.eval_on_selector_all(
            sel_all,
            "els=>els.map(e=>({id:e.id,w:e.clientWidth,sw:e.scrollWidth})).filter(x=>x.sw>x.w+1)")

    print("=== C. 大屏 1920 无溢出 ===")
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.wait_for_timeout(300)
    chk("1920 概览数字不溢出", not overflow("#ov0,#ov1,#ov2,#ov3"), str(overflow("#ov0,#ov1,#ov2,#ov3")))
    chk("数字 white-space:nowrap",
        page.eval_on_selector("#ov0", "e=>getComputedStyle(e).whiteSpace") == "nowrap")
    page.screenshot(path=os.path.join(SHOT, "v89-1920-工作台.png"), full_page=True)

    page.set_viewport_size({"width": 1366, "height": 900})
    page.wait_for_timeout(300)
    chk("1366 概览数字不溢出", not overflow("#ov0,#ov1,#ov2,#ov3"))
    page.screenshot(path=os.path.join(SHOT, "v89-1366-工作台.png"), full_page=True)

    print("=== D. 全部稿件：4 格 + 筛选胶囊 ===")
    page.set_viewport_size({"width": 1600, "height": 1000})
    page.click('.rail-item[data-view="archive"]')
    page.wait_for_timeout(2500)
    chk("视图为 archive", page.eval_on_selector("body", "e=>e.dataset.view") == "archive")
    a_nums = page.eval_on_selector_all("#ov0,#ov1,#ov2,#ov3", "els=>els.map(e=>e.textContent)")
    a_lab = page.eval_on_selector_all("#statStrip .si .l", "els=>els.map(e=>e.textContent)")
    chk("4 格为稿件数/审核通过/审核中/未通过", a_lab == ["稿件数", "审核通过", "审核中", "未通过"], str(a_lab))
    chk("稿件数为真实数字（非 —）", a_nums[0] not in ("—", ""), str(a_nums))
    chk("筛选胶囊已显示", page.eval_on_selector("#archFilters", "e=>!e.hidden"))
    chk("日增行在稿件视图隐藏",
        all(page.eval_on_selector("#ovd%d" % i, "e=>e.hidden") for i in range(4)))
    page.screenshot(path=os.path.join(SHOT, "v89-1920-全部稿件.png"), full_page=True)

    print("=== E. 点「审核通过」→ 带筛选进列表 ===")
    with page.expect_request(lambda r: "/api/archives" in r.url and "status=" in r.url, timeout=8000) as req:
        page.click("#ovSlot1")
    page.wait_for_timeout(2000)
    chk("请求带 status=pubed", "status=pubed" in req.value.url, req.value.url)
    chk("胶囊切到「审核通过」",
        page.eval_on_selector('.arch-chip[data-arch-status="pubed"]', "e=>e.classList.contains('on')"))
    chk("分页文案标注筛选", "审核通过" in (page.text_content("#queuePager") or ""))
    chk("列表有稿件行", page.eval_on_selector_all("#queueList .arch-row", "e=>e.length") > 0)
    page.screenshot(path=os.path.join(SHOT, "v89-1920-筛选审核通过.png"), full_page=True)

    print("=== E2. 稿件计数 90 秒 TTL 真正生效（不额外打 B站）===")
    ctx3 = b.new_context(viewport={"width": 1600, "height": 1000})
    p3 = ctx3.new_page()
    hit = []

    def arc(route):
        hit.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "ok": True, "cached": False, "day": "", "note": "",
            "items": {"total": 254, "pubed": 253, "is_pubing": 0, "not_pubed": 1}}))

    p3.route(re.compile(r".*/api/account/stats\?part=archive.*"), arc)
    p3.goto(BASE + "/", wait_until="networkidle")
    p3.wait_for_timeout(700)
    p3.click('.rail-item[data-view="archive"]')
    p3.wait_for_timeout(900)
    chk("首次进入取 1 次", len(hit) == 1, str(len(hit)))
    p3.click('.rail-item[data-view="queue"]')
    p3.wait_for_timeout(400)
    p3.click('.rail-item[data-view="archive"]')
    p3.wait_for_timeout(700)
    chk("90 秒内再进入命中缓存（不重取）", len(hit) == 1, str(len(hit)))
    # 把页面时间快进 100 秒：等价于停留超过 90 秒后再进入
    p3.evaluate("(()=>{const o=Date.now; Date.now=()=>o()+100000;})()")
    p3.click('.rail-item[data-view="queue"]')
    p3.wait_for_timeout(300)
    p3.click('.rail-item[data-view="archive"]')
    p3.wait_for_timeout(900)
    chk("超过 90 秒后重取（旧版会一直吃缓存）", len(hit) == 2, str(len(hit)))
    ctx3.close()

    print("=== F. 再点一次同一格 / 点「全部」回到全部 ===")
    page.click('.arch-chip[data-arch-status=""]')
    page.wait_for_timeout(1500)
    chk("回到全部（无筛选高亮）",
        page.eval_on_selector('.arch-chip[data-arch-status=""]', "e=>e.classList.contains('on')"))
    chk("分页文案不带筛选", "审核通过" not in (page.text_content("#queuePager") or ""))

    print("=== G. 队列格下钻 ===")
    page.click('.rail-item[data-view="queue"]')
    page.wait_for_timeout(900)
    page.click("#ovSlot1")     # 队列待办 → pending
    page.wait_for_timeout(700)
    chk("队列格把筛选设为 pending",
        page.evaluate("document.querySelector('.stat[data-filter=\"pending\"]').classList.contains('is-active')"))
    page.click("#ovSlot2")     # 剩余额度 → 打开设置
    page.wait_for_timeout(500)
    chk("额度格打开设置抽屉", page.eval_on_selector("#drawer", "e=>e.classList.contains('open')"))
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)

    print("=== G2. 封面统一 154×87（稿件列表 + 队列）===")
    page.click('.rail-item[data-view="archive"]')
    page.wait_for_timeout(1400)
    cov = page.evaluate("""() => Array.from(document.querySelectorAll('.arch-row .thumb')).slice(0,6).map(e=>{
        const r=e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)];})""")
    chk("稿件列表封面全部 154×87", len(cov) > 0 and all(w == 154 and h == 87 for w, h in cov), str(cov))
    page.click('.rail-item[data-view="queue"]')
    page.wait_for_timeout(700)
    page.click('.stat[data-filter=""]')      # 先回到「全部」，避免上一步的筛选把列表清空
    page.wait_for_timeout(1200)
    covq = page.evaluate("""() => Array.from(document.querySelectorAll('#queueList .task .thumb')).slice(0,6).map(e=>{
        const r=e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)];})""")
    chk("队列缩略图全部 154×87", len(covq) > 0 and all(w == 154 and h == 87 for w, h in covq), str(covq))
    page.set_viewport_size({"width": 768, "height": 1000})
    page.wait_for_timeout(900)
    covq2 = page.evaluate("""() => Array.from(document.querySelectorAll('#queueList .task .thumb')).slice(0,3).map(e=>{
        const r=e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)];})""")
    chk("768 队列仍 154×87 且不溢出",
        len(covq2) > 0 and all(w == 154 and h == 87 for w, h in covq2)
        and page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 2"), str(covq2))
    # 768 下左侧导航收进抽屉，直接 click 会报「元素在视口外」，改用脚本内切换
    page.evaluate("() => document.querySelector('.rail-item[data-view=\"archive\"]').click()")
    page.wait_for_timeout(1000)
    cov2 = page.evaluate("""() => Array.from(document.querySelectorAll('.arch-row .thumb')).slice(0,3).map(e=>{
        const r=e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)];})""")
    chk("768 稿件封面仍 154×87 且不溢出",
        len(cov2) > 0 and all(w == 154 and h == 87 for w, h in cov2)
        and page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 2"), str(cov2))
    page.set_viewport_size({"width": 1920, "height": 1000})
    page.wait_for_timeout(600)

    print("=== H. 深色 + 收起态无异常 ===")
    page.click("#btnTheme")
    page.wait_for_timeout(500)
    page.click('.rail-item[data-view="workbench"]')
    page.wait_for_timeout(900)
    page.screenshot(path=os.path.join(SHOT, "v89-1920-深色工作台.png"), full_page=True)
    chk("深色下数字正常渲染",
        page.eval_on_selector("#ov0",
                              "e=>{const t=(e.textContent||'').trim();return /\\d/.test(t) && e.getBoundingClientRect().height>10;}"))

    bad = [c for c in console if c[0] == "error"]
    chk("无 JS 运行错误", not errors, str(errors[:3]))
    chk("无 console error", not bad, str(bad[:3]))

    b.close()

print("\n===== 结果：%d 通过 / %d 失败 =====" % (ok, fail))
sys.exit(1 if fail else 0)
