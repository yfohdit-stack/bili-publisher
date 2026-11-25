"""v117 UI 校验：切换账号时合集选择不静默丢失。

- 原账号选的合集切回来能自动恢复；
- 目标账号有同名合集时按标题自动匹配；
- 目标账号没有同名合集时明确提示，而不是悄悄变成「不加入合集」。

用法：python3 tests/verify_ui_v117.py [base_url]
默认 http://127.0.0.1:5277（需已在跑）。用 Playwright 拦截账号/合集接口模拟多账号，不访问B站。
"""
import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5277"
passed, failed = 0, 0

ACCOUNTS = {
    "accA": {"id": "accA", "mid": 1, "uname": "甲账号", "face": "", "logged_in": False},
    "accB": {"id": "accB", "mid": 2, "uname": "乙账号", "face": "", "logged_in": False},
    "accC": {"id": "accC", "mid": 3, "uname": "丙账号", "face": "", "logged_in": False},
}
SEASONS = {
    "accA": [{"id": 111, "title": "2026录播合集", "count": 3, "section_id": 0, "state": 0}],
    "accB": [{"id": 222, "title": "2026录播合集", "count": 5, "section_id": 0, "state": 0}],
    "accC": [{"id": 333, "title": "别的合集", "count": 1, "section_id": 0, "state": 0}],
}
ACTIVE = {"id": "accA"}


def chk(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/local/bin/chromium", args=["--no-sandbox"])
        pg = b.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))

        def real(path):
            with urllib.request.urlopen(BASE + path, timeout=15) as r:
                return r.read()

        def on_boot(route):
            body = json.loads(real("/api/bootstrap"))
            body["accounts"] = list(ACCOUNTS.values())
            body["active_account"] = ACTIVE["id"]
            body["account"] = dict(ACCOUNTS[ACTIVE["id"]], id=ACTIVE["id"])
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body, ensure_ascii=False))

        def on_seasons(route):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "items": SEASONS[ACTIVE["id"]]},
                                          ensure_ascii=False))

        def on_switch(route):
            req = json.loads(route.request.post_data or "{}")
            ACTIVE["id"] = req.get("id") or ACTIVE["id"]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True,
                "account": dict(ACCOUNTS[ACTIVE["id"]], id=ACTIVE["id"]),
                "accounts": list(ACCOUNTS.values()),
                "active": ACTIVE["id"],
                "quota": {"logged_in": False, "ok": False},
            }, ensure_ascii=False))

        pg.route("**/api/bootstrap*", on_boot)
        pg.route("**/api/seasons", on_seasons)
        pg.route("**/api/accounts/switch", on_switch)

        def season_value():
            return pg.evaluate("() => document.getElementById('seasonSelect').value")

        def toast_text():
            return pg.evaluate("() => document.querySelector('.toast-wrap') ? document.querySelector('.toast-wrap').innerText : ''")

        def switch(aid):
            pg.evaluate("""(aid) => { const rows = [...document.querySelectorAll('[data-switch]')];
                const r = rows.find(x => x.getAttribute('data-switch') === aid); if (r) r.click(); }""", aid)
            pg.wait_for_timeout(1600)

        pg.goto(BASE, wait_until="domcontentloaded")
        pg.wait_for_timeout(1800)
        chk("初始账号有合集 111", "111" in pg.evaluate(
            "() => [...document.getElementById('seasonSelect').options].map(o => o.value)"))

        # 选甲账号的合集
        pg.evaluate("""() => { const s = document.getElementById('seasonSelect');
            s.value = '111'; s.dispatchEvent(new Event('change', {bubbles: true})); }""")
        pg.wait_for_timeout(150)
        chk("已选中合集 111", season_value() == "111", season_value())

        # 切到乙：同名合集 → 自动匹配 222
        switch("accB")
        chk("切到乙后按同名匹配 222", season_value() == "222", season_value())
        chk("提示已按同名匹配", "同名匹配" in toast_text(), toast_text()[-80:])

        # 切回甲：恢复原选择 111
        switch("accA")
        chk("切回甲后恢复 111", season_value() == "111", season_value())

        # 切到丙：没有同名合集 → 0 + 明确提示
        switch("accC")
        chk("切到丙后回到不加入合集", season_value() == "0", season_value())
        chk("提示原合集在本账号没有同名", "没有同名合集" in toast_text(), toast_text()[-100:])

        # 再切回甲仍是 111
        switch("accA")
        chk("再次切回甲仍恢复 111", season_value() == "111", season_value())

        chk("全程无 JS 报错", not errs, str(errs[:3]))
        b.close()

    print(f"\n===== 结果：{passed} 通过 / {failed} 失败 =====")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
