#!/usr/bin/env python3.11
"""v103 前端验证：通知 / 鉴权设置面板渲染、保存回显、交互、真实鉴权。

用法：python3 tests/verify_ui_v103.py [base_url]
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5305"
TOKEN = "uitoken123"

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
    b = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1600, "height": 1000})
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(1200)

    print("=== A. 设置面板新增区块 ===")
    page.click("#btnSettings")
    page.wait_for_timeout(400)
    # 用 text_content：section-title 被 CSS 转大写，inner_text 取的是渲染后文本
    body = page.locator("#drawer").text_content() or ""
    chk("状态通知卡片可见", "状态通知（Webhook）" in body)
    chk("访问鉴权卡片可见", "Web 访问鉴权" in body)
    for sel in ("notifyUrl", "notifyKind", "notifyChatId", "notifyEnabled",
                "notifyEvTask", "notifyEvRate", "notifyEvQuota", "notifyEvResume",
                "btnNotifyTest", "webAuthEnabled", "webAuthMode", "webAuthToken",
                "webAuthUser", "webAuthPass"):
        chk("元素 #%s 存在" % sel, page.locator("#" + sel).count() == 1)
    chk("初始 Telegram chat_id 行隐藏", not page.is_visible("#notifyChatRow"))
    chk("初始 Basic 行隐藏", not page.is_visible("#webAuthBasicRow"))
    chk("初始 Token 行显示", page.is_visible("#webAuthTokenRow"))

    print("=== B. 联动显示 ===")
    page.select_option("#notifyKind", "telegram")
    chk("选 Telegram 显示 chat_id", page.is_visible("#notifyChatRow"))
    page.select_option("#notifyKind", "auto")
    chk("切回自动隐藏 chat_id", not page.is_visible("#notifyChatRow"))
    page.select_option("#webAuthMode", "basic")
    chk("选 Basic 显示账号密码", page.is_visible("#webAuthBasicRow"))
    chk("选 Basic 隐藏 Token", not page.is_visible("#webAuthTokenRow"))
    page.select_option("#webAuthMode", "token")
    chk("切回 Token 恢复显示", page.is_visible("#webAuthTokenRow")
        and not page.is_visible("#webAuthBasicRow"))

    print("=== C. 文案与说明删除 ===")
    chk("过期文案已改为本地定时", "本地定时稿件过期超过" in body)
    chk("上回加回的说明已删除", "达到上限 +1 会触发 B站 24h 投稿封禁" not in body)

    print("=== D. 保存并回显 ===")
    page.fill("#notifyUrl", "http://127.0.0.1:1/nope")
    page.uncheck("#notifyEvResume")
    page.click("#btnSaveSettings")
    page.wait_for_timeout(2200)
    chk("Webhook 地址保存后回显",
        page.input_value("#notifyUrl") == "http://127.0.0.1:1/nope",
        page.input_value("#notifyUrl"))
    chk("事件开关保存后回显", not page.is_checked("#notifyEvResume"))
    chk("其它事件仍勾选", page.is_checked("#notifyEvTask"))

    print("=== E. 测试通知按钮 ===")
    page.click("#btnNotifyTest")
    page.wait_for_function(
        "document.querySelector('#btnNotifyTest').textContent.indexOf('发送测试通知') >= 0",
        timeout=15000)
    chk("按钮恢复正常", not page.is_disabled("#btnNotifyTest"))

    print("=== F. 真实 Token 鉴权 ===")
    page.check("#webAuthEnabled")
    page.fill("#webAuthToken", TOKEN)
    page.click("#btnSaveSettings")
    page.wait_for_timeout(2200)
    chk("启用鉴权后当前会话仍可用", page.locator("#appVer").count() == 1)
    chk("Cookie 已写入", any("wb_auth" in c["name"] for c in ctx.cookies()))
    chk("Token 回显为掩码", "***" in page.input_value("#webAuthToken"),
        page.input_value("#webAuthToken"))

    fresh = b.new_context()
    r = fresh.new_page().goto(BASE + "/", wait_until="domcontentloaded")
    chk("新会话无凭证被拦 401", r.status == 401, str(r.status if r else "?"))
    pg = fresh.new_page()
    resp = pg.goto(BASE + "/", wait_until="domcontentloaded")
    chk("401 页面含输入框", resp.status == 401
        and pg.locator('input[name="token"]').count() == 1
        and "需要访问凭证" in pg.content())
    resp2 = fresh.new_page().goto(BASE + "/?token=" + TOKEN, wait_until="domcontentloaded")
    chk("带 token 放行 200", resp2.status == 200, str(resp2.status if resp2 else "?"))
    fresh.close()

    chk("无 JS 运行时错误", not errors, str(errors[:3]))
    b.close()

print("\n===== 结果：%d 通过 / %d 失败 =====" % (ok, fail))
sys.exit(1 if fail else 0)
