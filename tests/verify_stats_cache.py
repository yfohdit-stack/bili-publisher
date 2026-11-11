"""概览统计缓存测试：账号累计按天、稿件计数 90 秒 + 投稿作废、落盘、多账号隔离。

隔离方式：把 store 的目录/库/配置常量与 worker 历史文件全部打到临时目录，
并置 store._conn=None，避免误连真实库。临时目录退出时自动清理。
"""
import atexit
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="stats-cache-")
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
from app import quota, server, store, worker  # noqa: E402

store.DATA_DIR = TMP
store.DB_PATH = os.path.join(TMP, "publish.db")
store.CONFIG_PATH = os.path.join(TMP, "config.json")
worker.HISTORY_FILE = os.path.join(TMP, "upload_history.json")
store._conn = None
server.STATS_CACHE_FILE = os.path.join(TMP, "stats_cache.json")

ok = fail = 0


def chk(name, cond):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS", name)
    else:
        fail += 1
        print("  FAIL", name)


def acc(aid, mid, uname, sd):
    return {"id": aid, "mid": mid, "uname": uname,
            "cookie": {"sessdata": sd, "bili_jct": "j" + sd[:4], "dedeuserid": str(mid)}}


ACC1 = acc("acc_1", 111, "号一", "S" * 60 + "aaa")
ACC2 = acc("acc_2", 222, "号二", "T" * 60 + "bbb")
CFG = {"active_account": "acc_1", "cookie": ACC1["cookie"], "accounts": [ACC1, ACC2],
       "account": {"mid": 111, "uname": "号一", "face": ""}}

server.store.load_config = lambda: CFG

calls = {"workbench": 0, "archive": 0}
stamps = []


def fake_stats(cfg, part):
    calls[part] += 1
    if part == "workbench":
        stamps.append(__import__("time").time())
    if part == "workbench":
        return {"ok": True, "items": {"play": 795519, "like": 23097, "coin": 918, "fav": 5019}}
    return {"ok": True, "items": {"total": 35635, "pubed": 35595, "is_pubing": 0, "not_pubed": 40}}


server.quota.account_stats = fake_stats
client = server.app.test_client()


def reset():
    server._stats_cache.clear()
    calls["workbench"] = 0
    calls["archive"] = 0
    try:
        os.remove(server.STATS_CACHE_FILE)
    except OSError:
        pass


print("=== A. 账号累计：一天只取一次 ===")
reset()
r1 = client.get("/api/account/stats?part=workbench").get_json()
chk("首次 ok 且非缓存", r1["ok"] and r1["cached"] is False)
chk("首次数值正确", r1["items"]["play"] == 795519)
r2 = client.get("/api/account/stats?part=workbench").get_json()
chk("当天第二次命中缓存", r2["cached"] is True)
chk("整天只打 1 次 B站", calls["workbench"] == 1)
chk("返回统计日", r1.get("day") == server._stats_day())
chk("note 说明每天 12:00 汇总", "12:00" in (r1.get("note") or ""))

print("=== B. 跨过 12:00 换日后重新取 ===")
real_day = server._stats_day
server._stats_day = lambda now=None: "2099-01-01"   # 假装到了新的一天
r3 = client.get("/api/account/stats?part=workbench").get_json()
chk("换日后 miss 重新请求", r3["cached"] is False and calls["workbench"] == 2)
server._stats_day = real_day

print("=== C. 稿件计数：90 秒缓存 + 投稿作废 ===")
reset()
r4 = client.get("/api/account/stats?part=archive").get_json()
chk("首次取数", r4["ok"] and calls["archive"] == 1)
r5 = client.get("/api/account/stats?part=archive").get_json()
chk("90 秒内命中缓存", r5["cached"] is True and calls["archive"] == 1)
with server._stats_lock:
    for k, v in server._stats_cache.items():
        if k.endswith("|archive"):
            v["at"] = v["at"] - 120   # 人为推到 2 分钟前
r6 = client.get("/api/account/stats?part=archive").get_json()
chk("超过 90 秒后失效", r6["cached"] is False and calls["archive"] == 2)

server._stats_invalidate("archive")
r7 = client.get("/api/account/stats?part=archive").get_json()
chk("投稿作废后立刻重取", r7["cached"] is False and calls["archive"] == 3)

print("=== D. 落盘与重启复用 ===")
reset()
client.get("/api/account/stats?part=workbench")
client.get("/api/account/stats?part=archive")
chk("缓存文件已生成", os.path.exists(server.STATS_CACHE_FILE))
with open(server.STATS_CACHE_FILE, encoding="utf-8") as f:
    disk = json.load(f)
chk("文件里含工作台与稿件两份", any(k.endswith("|workbench") for k in disk) and any(k.endswith("|archive") for k in disk))
server._stats_cache.clear()
server._stats_load()
calls["workbench"] = calls["archive"] = 0
r8 = client.get("/api/account/stats?part=workbench").get_json()
chk("重启后当天仍命中缓存（不重打 B站）", r8["cached"] is True and calls["workbench"] == 0)

print("=== E. 多账号互不污染 ===")
reset()
a1 = client.get("/api/account/stats?part=workbench").get_json()
CFG2 = dict(CFG, active_account="acc_2", cookie=ACC2["cookie"], account={"mid": 222, "uname": "号二"})
server.store.load_config = lambda: CFG2
a2 = client.get("/api/account/stats?part=workbench").get_json()
chk("切号后不命中上一个号的缓存", a2["cached"] is False and calls["workbench"] == 2)
with server._stats_lock:
    keys = list(server._stats_cache.keys())
chk("两个号分别入缓存", any("acc_1" in k for k in keys) and any("acc_2" in k for k in keys))
server.store.load_config = lambda: CFG

print("=== F. 每天给所有账号预取一次 ===")
reset()
stamps.clear()
server._stats_prefetch_accounts()
chk("两个号各取一次", calls["workbench"] == 2)
chk("两个号之间有间隔（防集中打接口）",
    len(stamps) >= 2 and stamps[1] - stamps[0] >= server.STATS_PREFETCH_GAP * 0.9)
server._stats_prefetch_accounts()
chk("同一天不重复取（第二次 0 次新请求）", calls["workbench"] == 2)
with server._stats_lock:
    keys = [k for k in server._stats_cache if k.endswith("|workbench")]
chk("两个号都有当天缓存", len(keys) == 2)

print("=== G. 异常兜底 ===")
reset()
server.quota.account_stats = lambda cfg, part: (_ for _ in ()).throw(RuntimeError("B站接口超时"))
r9 = client.get("/api/account/stats?part=workbench").get_json()
chk("接口异常返回 ok:false 不抛 5xx", r9["ok"] is False and "超时" in r9["error"])
chk("异常不写缓存", not server._stats_cache)
server.quota.account_stats = fake_stats

print("=== H. 部分接口失败：不许拿 0 冒充 ===")
reset()
server.quota.account_stats = lambda cfg, part: {"ok": True, "partial": True,
    "items": {"total": 2398, "pubed": 2398, "is_pubing": None, "not_pubed": None}}
r10 = client.get("/api/account/stats?part=archive").get_json()
chk("缺失字段为 null 而不是 0", r10["items"]["is_pubing"] is None and r10["items"]["not_pubed"] is None)
chk("已取到的字段保留真值", r10["items"]["total"] == 2398)
chk("partial 结果不写缓存", not server._stats_cache)
chk("提示里说明部分字段没取到", "没取到" in (r10.get("note") or ""))
r11 = client.get("/api/account/stats?part=archive").get_json()
chk("下一次请求重新取（不被假 0 卡住 90 秒）", r11["cached"] is False)

print("=== I. 风控伪 0：整体判失败，显示「—」 ===")
reset()
server.quota.account_stats = lambda cfg, part: (_ for _ in ()).throw(RuntimeError("稿件总数为 0 且状态读取不全"))
r12 = client.get("/api/account/stats?part=archive").get_json()
chk("全 0 且取不全时 ok:false", r12["ok"] is False)
chk("错误不写缓存", not server._stats_cache)

print("=== J. 真的 0 稿件账号：允许显示 0 ===")
reset()
server.quota.account_stats = lambda cfg, part: {"ok": True, "partial": False,
    "items": {"total": 0, "pubed": 0, "is_pubing": 0, "not_pubed": 0}}
r13 = client.get("/api/account/stats?part=archive").get_json()
chk("真 0 正常返回", r13["ok"] is True and r13["items"]["total"] == 0)
chk("真 0 可以缓存", r13["cached"] is False and bool(server._stats_cache))
r14 = client.get("/api/account/stats?part=archive").get_json()
chk("真 0 第二次命中缓存", r14["cached"] is True)

print("=== K. 磁盘旧缓存：旧键格式 / partial / 全 0 载入时丢弃 ===")
reset()
server._stats_cache.clear()
fp1 = server._stats_fp(ACC1["cookie"]["sessdata"])
good_key = "acc_1|%s|archive" % fp1
old_key = "acc_1|216|aaaaaa|archive"          # v88 及更早的「长度+前6位」格式
bad = {
    old_key: {"at": 1e12, "day": "2026-11-09", "ok": True,
        "data": {"ok": True, "items": {"total": 254, "pubed": 253, "is_pubing": 0, "not_pubed": 1}}},
    "acc_1|216|aaaaaa|workbench": {"at": 1e12, "day": server._stats_day(), "ok": True,
        "data": {"ok": True, "items": {"play": 1, "like": 2, "coin": 3, "fav": 4}}},
    good_key: {"at": 1e12, "day": "2026-11-09", "ok": True,
        "data": {"ok": True, "partial": True, "items": {"total": 0, "pubed": 0, "is_pubing": 0, "not_pubed": None}}},
}
with open(server.STATS_CACHE_FILE, "w", encoding="utf-8") as f:
    json.dump(bad, f)
server._stats_load()
chk("旧键格式整体丢弃", not any(k.count("|") == 3 for k in server._stats_cache))
chk("新键但 partial 的 archive 被丢弃", not any(k.endswith("|archive") for k in server._stats_cache))
chk("旧格式 workbench 也丢弃（不留两套数据）", not server._stats_cache)

print("=== L. 指纹：sha256 前 12 位，不再用长度+前 6 位 ===")
k1 = server._stats_key(CFG, "workbench")
chk("键为 3 段且指纹 12 位", k1.count("|") == 2 and len(k1.split("|")[1]) == 12)
chk("指纹不含 SESSDATA 明文", "SSSS" not in k1 and ACC1["cookie"]["sessdata"][:6] not in k1)
chk("长度相同、前 6 位相同的两个号不会串号",
    server._stats_fp("A" * 60 + "x") != server._stats_fp("A" * 60 + "y"))
chk("同一 SESSDATA 指纹稳定", server._stats_fp("abc") == server._stats_fp("abc"))
chk("未登录用 anon", server._stats_fp("") == "anon")

print("=== M. 日增 incr 透传 ===")
reset()


def fake_stats_incr(cfg, part):
    calls[part] += 1
    if part == "workbench":
        return {"ok": True, "items": {"play": 539, "like": 62, "coin": 44, "fav": 0},
                "incr": {"play": 12, "like": 0, "coin": -3, "fav": None}}
    return fake_stats(cfg, part)


server.quota.account_stats = fake_stats_incr
r15 = client.get("/api/account/stats?part=workbench").get_json()
chk("接口带 incr 字段", isinstance(r15.get("incr"), dict))
chk("正增量原样返回", r15["incr"]["play"] == 12)
chk("0 增量不被当成缺失", r15["incr"]["like"] == 0)
chk("负增量保留负号", r15["incr"]["coin"] == -3)
chk("缺失字段为 null", r15["incr"]["fav"] is None)
r16 = client.get("/api/account/stats?part=workbench").get_json()
chk("缓存命中时增量一起返回", r16["cached"] is True and r16["incr"]["play"] == 12)
server.quota.account_stats = fake_stats

print("=== N. 并发落盘：唯一 tmp + 落盘锁，不写坏 JSON ===")
reset()
import threading  # noqa: E402

errs = []
for _ in range(6):
    with server._stats_lock:
        for i in range(50):
            server._stats_cache["acc_1|%012x|archive" % i] = {
                "at": 1e12, "day": "2026-11-09", "ok": True,
                "data": {"ok": True, "items": {"total": i, "pubed": i, "is_pubing": 0, "not_pubed": 0}}}


def hammer():
    try:
        for _ in range(30):
            server._stats_save()
    except Exception as e:                       # noqa: BLE001
        errs.append(e)


ths = [threading.Thread(target=hammer) for _ in range(6)]
[t.start() for t in ths]
[t.join() for t in ths]
chk("并发 save 无异常", not errs)
try:
    with open(server.STATS_CACHE_FILE, encoding="utf-8") as f:
        disk2 = json.load(f)
    chk("落盘 JSON 完整可解析", isinstance(disk2, dict) and len(disk2) == 50)
except Exception as e:                           # noqa: BLE001
    chk("落盘 JSON 完整可解析", False)
left = [n for n in os.listdir(TMP) if n.endswith(".tmp")]
chk("没有残留 .tmp 文件", not left)

print("=== O. 全部稿件：status 参数校验与透传 ===")


def fake_list(cfg, pn=1, ps=10, status=None):
    return {"items": [], "page": pn, "ps": ps, "total": 0, "status": status or "",
            "total_pages": 0, "has_more": False}


seen = {}
server.quota.list_archives = lambda cfg, pn=1, ps=10, status=None: (
    seen.update({"status": status}) or fake_list(cfg, pn, ps, status))
server.store.list_tasks = lambda *a, **k: []      # 本测试不关心本地任务关联，避开建表
c1 = client.get("/api/archives?page=1&ps=10&status=is_pubing")
chk("合法 status 透传给 B站查询", c1.status_code == 200 and seen.get("status") == "is_pubing")
c2 = client.get("/api/archives?page=1&ps=10&status=bogus")
chk("非法 status 返回 400", c2.status_code == 400)
c3 = client.get("/api/archives?page=1&ps=10")
chk("不传 status 视为全部", c3.status_code == 200 and seen.get("status") is None)

print(f"\n===== 结果：{ok} 通过 / {fail} 失败 =====")
sys.exit(1 if fail else 0)
