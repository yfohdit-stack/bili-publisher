"""队列执行器：单线程顺序投稿，带 24h 额度把关、定时投放与失败重试。

用单线程是刻意的：B站对同一账号的并发上传并不友好，顺序执行也能保证
额度统计不打架。一次只跑一个任务，其余排队。
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import ledger, media, notify, quota, season, store
from .channels import official

# 定时发布：距发布时间 >= 这个间隔就把 dtime 直接交给B站定时队列。
# B站定时实际只要求至少提前 5 分钟（旧版误以为要提前 4 小时），这里留一倍
# 余量取 10 分钟：10 分钟以上交给B站定时；不足 10 分钟的由本地候到点再即时投放。
SCHEDULE_MIN_AHEAD = 10 * 60
POLL_INTERVAL = 2.0

# 上传节流：最近 10 个视频里，第 10 个若仍在 10 分钟内则等它滑出窗口再放行
WINDOW_SECS = 600
WINDOW_MAX = 10
RISK_WAIT = 120            # 触发B站验证码：等 2 分钟再试（单P/多P 一致，不再分叉）
# 限流按接口来源分流：封面 -702 每次等 1 分钟；预上传 406/429 每次等 5 分钟。
# 406 是「10 分钟内投稿过多」风控窗口，但不必干等满 10 分钟：5 分钟先探一次，
# 仍被限流就到 10 分钟时间点再探（窗口内共两次机会），连续 3 次才暂停待人工。
# 这些都是平台窗口问题，不应消耗单个稿件的失败重试次数。
RATE_WAIT_COVER = 60
RATE_WAIT_UPLOAD = 300
RATE_GIVEUP_COVER = 10
RATE_GIVEUP_UPLOAD = 3
HISTORY_FILE = os.path.join(store.DATA_DIR, "upload_history.json")
# v75：风控冷却与节流等待都按账号隔离。key 用任务里的 account_id 原值
# （空字符串代表未指定账号 / 历史任务），避免一个账号撞风控或占满窗口时
# 把其它账号一起拖住。
_risk_until: Dict[str, float] = {}       # account_id -> 风控冷却截止时刻
_account_wait: Dict[str, float] = {}     # account_id -> 节流窗口腾出时刻
_rate_until: Dict[str, float] = {}       # account_id -> 接口限流冷却截止时刻
_rate_wait: Dict[str, float] = {}        # account_id -> 本次冷却总时长
_rate_state: Dict[str, Dict[str, Any]] = {}  # account_id -> 连续限流类型与次数
_ACCT_DEFAULT = ""

# 进度落库节流：分块回调很密，逐块写库 + 写日志会把 SQLite 和日志表压垮
STAGE_MIN_INTERVAL = 1.0   # 距上次落库至少 1 秒
STAGE_MIN_DELTA = 0.01     # 或进度至少涨 1%
_NUM = re.compile(r"\d+(?:\.\d+)?")

# 卡死看门狗：uploading 状态超过这么久没有任何进度更新即判僵死。
# 取值高于 official.START_TIMEOUT(25 分钟)，官方通道不会误伤。
STUCK_TIMEOUT = 30 * 60
WATCHDOG_INTERVAL = 60

# 状态通知：同一事件文案在短时间内重复出现只推一次（限流/额度复查可能连击），
# 失败日志也做节流，避免 Webhook 挂掉时把总日志刷满。
NOTIFY_DEDUP_SECS = 20.0
NOTIFY_FAIL_LOG_GAP = 300.0
_notify_last: Dict[str, float] = {}
_notify_lock = threading.Lock()
_notify_fail_logged_at = 0.0



_thread: Optional[threading.Thread] = None
_watchdog_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_paused = threading.Event()
# 暂停原因：manual=人工点暂停（绝不自动恢复）；quota=24h 额度满自动暂停
# （额度窗口滑出后自动继续）；rate=连续限流，需人工核对。
_pause_reason = "manual"
_quota_resume_at = 0.0          # 额度最早腾出空位的时刻；0 表示改用兜底轮询
_quota_last_check = 0.0         # 上次复查额度的时刻，控制接口调用频率
_wake = threading.Event()       # 事件驱动：新建/取消/切换状态时唤醒，替代固定 2 秒空查
_cancel_requested: set[str] = set()
_current: Dict[str, Any] = {"task_id": "", "started_at": 0.0}
_state_lock = threading.RLock()   # RLock：start() 里会再调 _reset_stage()，需要可重入
_quota_cache: Dict[str, Any] = {}   # 多账号：按账号 id 分别缓存额度
_last_stage: Dict[str, Any] = {"task_id": "", "at": 0.0, "progress": -1.0, "label": ""}


# ------------------------------------------------------------------ 控制

def start() -> None:
    global _thread, _pause_reason, _quota_resume_at, _quota_last_check
    with _state_lock:
        if _thread and _thread.is_alive():
            return
        # 暂停态必须跨重启保留，否则升级/重启会在用户不知情时自动开投。
        # 同时恢复「暂停原因」：只有额度满的自动暂停才允许到点自动继续，
        # 人工暂停在重启后依然保持暂停，绝不擅自开投。
        cfg = store.load_config()
        auto_resume = cfg.get("auto_resume", True) is not False
        if cfg.get("paused_queue"):
            _paused.set()
            _pause_reason = str(cfg.get("paused_reason") or "manual")
            # boot = 部署/升级时写入的「保护性暂停」，不是用户主动点的。
            # 设置里开启「自动恢复队列」就直接解除，省掉人工点「继续队列」；
            # 解除后 worker 投稿前会现查额度，封禁/额度满会立刻转入 quota 等待，不会误投。
            # 用户主动点的 manual 暂停永远保留，绝不擅自开投。
            if _pause_reason == "boot" and auto_resume:
                _paused.clear()
                _pause_reason = ""
                try:
                    store.patch_config({"paused_queue": False, "paused_reason": "",
                                        "quota_resume_at": 0.0})
                except Exception:   # noqa: BLE001  内存态已生效，写盘失败不阻断启动
                    pass
        else:
            _paused.clear()
            _pause_reason = ""
        # 重启后沿用上次算出的腾位时刻，避免刚启动就多打一次额度接口
        _quota_resume_at = (float(cfg.get("quota_resume_at") or 0.0)
                            if _pause_reason == "quota" else 0.0)
        _quota_last_check = 0.0
        recover_interrupted()
        _reset_stage()
        _stop.clear()
        _wake.clear()
        _thread = threading.Thread(target=_loop, name="publish-worker", daemon=True)
        _thread.start()
        # 看门狗按需启动：有任务进入 uploading 时才拉起，空闲不挂线程


def wake() -> None:
    """通知主循环状态变了（新任务/取消/暂停切换/定时改动），立即重新查库。"""
    _wake.set()


def stop() -> None:
    _stop.set()
    _wake.set()


def shutdown(timeout: float = 10.0) -> int:
    """进程退出前收尾：停队列，并把仍在上传中的任务标为「已中断」。

    返回被标记的任务数。上传是没法从外部安全打断的（稿件可能已经提交出去了），
    所以这里只做两件事：尽快停轮询、把真实状态写回库，下次启动就不会看到
    永远卡在 uploading 的僵尸任务。
    """
    stop()
    th = _thread
    if th and th.is_alive() and th is not threading.current_thread():
        th.join(timeout=timeout)
    return _mark_interrupted("进程退出时上传未完成")


def recover_interrupted() -> int:
    """启动检查：上次运行遗留的 uploading 一律标为 interrupted，绝不自动重排。

    自动重排会造成重复投稿——中断的那一刻，稿件很可能已经提交成功了。
    """
    n = _mark_interrupted("上次运行中途中断")
    if n:
        store.add_global_log(
            f"启动检查：{n} 个任务上次运行中断，已标记为「已中断」，请到创作中心核对后再决定是否重投",
            "warn")
    return n


def _mark_interrupted(reason: str) -> int:
    n = 0
    for t in store.list_by_status("uploading"):
        store.update_task(
            t["id"], status="interrupted", finished_at=time.time(),
            stage="", speed=0, error="interrupted",
            message=f"{reason}：稿件可能已发出，请先去创作中心核对，确认没发出再点「重新排队」")
        store.add_log(t["id"], f"{reason}，已标记为「已中断」（不自动重投，避免重复稿件）", "warn")
        n += 1
    return n


def pause(flag: bool = True, reason: Optional[str] = None) -> bool:
    """暂停/恢复队列。

    reason 只在不改变暂停状态时也可能需要落盘（比如额度满时队列本就暂停），
    所以写配置的判断不能只看 changed。manual=用户主动暂停（绝不自动恢复），
    boot=部署/重启的保护性暂停（受设置 auto_resume 控制，开启时自动解除），
    quota=24h 额度满/封禁自动暂停（额度归零后自动继续），rate=连续限流待人工核对。
    """
    global _pause_reason, _quota_resume_at, _quota_last_check
    changed = _paused.is_set() != bool(flag)
    old_reason = _pause_reason
    if flag:
        _paused.set()
        _pause_reason = reason or "manual"
    else:
        _paused.clear()
        _pause_reason = ""
        _quota_resume_at = 0.0
        _quota_last_check = 0.0
    if _pause_reason != "quota":
        # 非额度等待态不该残留腾位时刻（人工暂停/限流暂停/恢复）
        _quota_resume_at = 0.0
    # 原因变了也要落盘：额度等待中再点「暂停队列」，暂停状态没变但意图已从
    # 「等额度自动续投」变成「人工停工」，不写盘的话重启会按残留的 quota 擅自开投。
    if changed or reason is not None or old_reason != _pause_reason:
        # 手工暂停和 worker 自动暂停都走这里，避免只持久化 HTTP 按钮那一条路径。
        try:
            store.patch_config({"paused_queue": bool(flag),
                                "paused_reason": _pause_reason,
                                "quota_resume_at": float(_quota_resume_at)})
        except Exception as e:  # 状态在内存里仍已生效，不能因为写配置失败继续投稿
            try:
                store.add_global_log(f"队列暂停状态保存失败：{type(e).__name__}: {e}", "warn")
            except Exception:
                pass
    return _paused.is_set()


def pause_for_quota(q: Dict[str, Any]) -> None:
    """额度满/24h 封禁：自动暂停，并记住解封（额度归零）时刻，到点自动继续。"""
    global _quota_resume_at, _quota_last_check
    now = time.time()
    nxt = float(q.get("next_release_at") or 0.0)
    _quota_resume_at = nxt if nxt > now else 0.0
    _quota_last_check = now       # 刚查过额度，别立刻再打一次接口
    pause(True, "quota")
    # v103：额度满 / 24h 封禁是「无需人工值守」里最容易卡住的一环，主动推一条。
    reason = str(q.get("error") or "近 24h 投稿额度已满")
    when = (f"预计 {time.strftime('%m-%d %H:%M', time.localtime(_quota_resume_at))} 复查额度"
            if _quota_resume_at else "将按兜底间隔继续复查额度")
    _notify("quota", "队列已暂停：24h 投稿额度已满 / 封禁",
            f"{reason}。稿件继续排队不会被丢弃，{when}，腾出空位后自动续投。", "warn")


# 额度没算出腾位时刻时的兜底复查间隔；算出时刻之前完全不请求接口。
QUOTA_RECHECK_SECS = 60.0


def _maybe_resume_quota(now: float) -> bool:
    """额度满自动暂停期间：到点复查一次额度，腾出空位就自动继续队列。

    只对 reason=quota 生效；人工暂停与限流暂停一律不碰，避免用户不知情时开投。
    到预计时刻之前不请求接口（额度窗口常按小时计，不能每 2 秒空转打 B站）。
    """
    global _quota_resume_at, _quota_last_check
    if not _paused.is_set() or _pause_reason != "quota":
        return False
    if _quota_resume_at > now:
        return False
    if now - _quota_last_check < QUOTA_RECHECK_SECS:
        return False
    _quota_last_check = now
    try:
        q = cached_quota(force=True)
    except Exception:
        return False
    if q.get("logged_in") and q.get("ok") and not q.get("incomplete"):
        pause(False)
        store.add_global_log("24h 投稿额度已腾出空位，队列自动继续投稿", "ok")
        # v103：封禁解除 / 额度归零后自动开投，主动推一条，不用再盯浏览器。
        _notify("resume", "额度已恢复，队列自动继续投稿",
                "24h 投稿额度已腾出空位（封禁解除或额度归零），队列已自动恢复，"
                "无需人工点「继续队列」。", "success")
        return True
    nxt = float(q.get("next_release_at") or 0.0)
    _quota_resume_at = nxt if nxt > now else 0.0
    return False


def is_paused() -> bool:
    return _paused.is_set()


def pause_reason() -> str:
    """当前暂停原因：""（未暂停）/ manual / quota / rate，供界面区分展示。"""
    return _pause_reason if _paused.is_set() else ""


def quota_resume_at() -> float:
    """额度满自动暂停时，预计腾出空位的时刻（0 = 未知，按兜底间隔复查）。"""
    return _quota_resume_at if _pause_reason == "quota" else 0.0


def current_task_id() -> str:
    return _current.get("task_id", "")


def request_cancel(task_id: str) -> None:
    _cancel_requested.add(task_id)


def _cancelled(task_id: str) -> bool:
    return task_id in _cancel_requested


def _clear_cancel(task_id: str) -> None:
    _cancel_requested.discard(task_id)


def clear_cancel(task_id: str) -> None:
    """供 HTTP 层调用：任务被重新排队 / 提前上传 / 编辑时，清掉旧的取消标记。

    旧实现里点「取消」会把 id 永久留在 _cancel_requested；之后再点「重新排队」，
    worker 一取到就立刻又标成已取消，用户得点两次才真的排上队。
    """
    _cancel_requested.discard(task_id)


# ------------------------------------------------------------------ 额度

def _quota_with_ledger(data: Dict[str, Any],
                      account_id: Optional[str] = None) -> Dict[str, Any]:
    """缓存里的 B站数字可以复用，账本必须现读、并集现算。

    否则投稿成功后下一个任务看到的还是投稿前那份缓存，于是一路「还差一个」，
    最后放到越界——这正是 24h 上限没拦住的直接原因。

    口径与 quota.split_quota 一致：已确认（列表）+ 未确认（账本里列表还没有的）。
    """
    if not data:
        return data
    try:
        hours = int(data.get("hours") or 24)
        rows = ledger.recent(account_id or store.active_account_id() or "", hours)
    except Exception:   # noqa: BLE001  账本读失败就退回原结果，不阻断投稿
        return data
    if len(rows) == int(data.get("local_count") or 0):
        return data      # 账本没变化，缓存结论仍然成立
    remote_n = int(data.get("remote_count") or 0)
    rk = set(data.get("remote_keys") or [])
    limit = int(data.get("limit") or 50)
    merged = quota.merge_quota(rk, remote_n, rows)
    pending = merged["pending"]
    count = merged["count"]
    out = dict(data)
    out["local_count"] = len(rows)
    out["confirmed"] = remote_n
    out["pending"] = pending
    out["intersect"] = merged["intersect"]
    out["merge_fallback"] = False
    out["count"] = count
    # 缓存合并后也必须过封禁判定：达到 n+1 就落盘封禁，封禁期内 ok 恒 False，
    # 绝不能被 count<limit 覆盖回去，否则会踩 B站 24h 封禁。
    try:
        ban = quota.quota_ban_until(store.load_config(), out.get("items") or [],
                                    hours, count, limit)
    except Exception:   # noqa: BLE001  读配置失败就沿用缓存里的封禁值
        ban = float(out.get("quota_ban_until") or 0.0)
    out["quota_ban_until"] = ban
    if ban > time.time():
        out["ok"] = False
        out["next_release_at"] = ban
        out["error"] = (f"已触发 B站 {hours}h 投稿封禁（近 {hours} 小时投稿 {count} 个，"
                        f"超过上限 {limit}）。封禁期内额度下降也不能投稿，需等额度归零后自动恢复；预计 "
                        + time.strftime("%m-%d %H:%M", time.localtime(ban)) + " 解除")
    else:
        out["ok"] = count < limit
        if not out["ok"]:
            extra = f"（已确认 {remote_n} + 未确认 {pending}）" if pending > 0 else ""
            out["error"] = (f"近 {hours} 小时已投稿 {count} 个（含审核中与未通过{extra}），"
                            f"达到上限 {limit}")
        elif out.get("error") and not out.get("incomplete"):
            # 账本记录滑出窗口后额度恢复，必须同时清掉旧的「达到上限」错误。
            out["error"] = ""
            out["next_release_at"] = 0.0
    return out


def cached_quota(force: bool = False, account_id: Optional[str] = None) -> Dict[str, Any]:
    """额度按账号缓存：多账号下不同账号的 24h 计数互不干扰。"""
    key = account_id or store.active_account_id() or "_active"
    now = time.time()
    item = _quota_cache.get(key) or {}
    if not force and item.get("data") and now - item.get("at", 0) < 180:
        # 校准结果写回缓存：否则下次比较的还是旧账本基线，账本先减后增时会误判「没变化」
        data = _quota_with_ledger(item["data"], account_id)
        item["data"] = data
        _quota_cache[key] = item
        return data
    data = quota.check_quota(store.account_cfg(account_id) if account_id else store.load_config(),
                             account_key=account_id)
    _quota_cache[key] = {"at": now, "data": data}
    return _quota_with_ledger(data, account_id)


def refresh_quota(account_id: Optional[str] = None) -> Dict[str, Any]:
    return cached_quota(force=True, account_id=account_id)


# 投稿入口（/api/publish）刚真实查过额度，worker 取任务时若结果仍「新鲜」就直接复用，
# 避免同一稿件把 nav + 三个状态列表请求重复发两遍；超龄或没缓存则强制重查。
QUOTA_FRESH_SECS = 10.0


def fresh_quota(account_id: Optional[str] = None,
                max_age: float = QUOTA_FRESH_SECS) -> Dict[str, Any]:
    key = account_id or store.active_account_id() or "_active"
    item = _quota_cache.get(key) or {}
    if item.get("data") and time.time() - item.get("at", 0.0) < max_age:
        data = _quota_with_ledger(item["data"], account_id)
        item["data"] = data
        _quota_cache[key] = item
        return data
    return cached_quota(force=True, account_id=account_id)


# ------------------------------------------------------------------ 节流

def _acct(task: Dict[str, Any]) -> str:
    """任务归属的账号 key。用数据库里的 account_id 原值，空串=未指定/历史任务。"""
    return str(task.get("account_id") or _ACCT_DEFAULT)


def _load_history() -> Dict[str, List[float]]:
    """读取按账号分桶的上传记录 {account_id: [ts...]}。

    兼容 v74 及更早的纯数组格式：整体归入默认账号桶，用户升级后不会丢记录。
    """
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if isinstance(data, list):
        return {_ACCT_DEFAULT: [float(x) for x in data]} if data else {}
    if isinstance(data, dict):
        by = data.get("by") if isinstance(data.get("by"), dict) else {}
        out: Dict[str, List[float]] = {}
        for k, v in by.items():
            if isinstance(v, list):
                out[str(k)] = [float(x) for x in v]
        return out
    return {}


def _save_history(items) -> None:
    """items 为 list -> 覆盖默认账号桶（兼容旧调用/测试）；dict -> 写全部分桶。"""
    by = ({str(k): [float(x) for x in v] for k, v in items.items()}
          if isinstance(items, dict)
          else {_ACCT_DEFAULT: [float(x) for x in items]})
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": 2, "by": by}, f)
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        pass


def _history(now: Optional[float] = None, account_id: str = _ACCT_DEFAULT) -> List[float]:
    """该账号窗口内的上传记录（时间升序）。窗口外的记录不参与节流，直接过滤掉。"""
    now = time.time() if now is None else now
    return sorted(t for t in _load_history().get(account_id, []) if t > now - WINDOW_SECS)


def _wait_from(h: List[float], need: int, now: float) -> int:
    """给一份已过滤好的窗口记录，算还要等几秒才腾得下 need 个名额。"""
    if need <= 0 or not h:
        return 0
    over = len(h) + need - WINDOW_MAX
    if over <= 0:
        return 0
    return max(1, int(h[over - 1] + WINDOW_SECS - now) + 1)


def _wait_seconds(need: int = 1, now: Optional[float] = None,
                  account_id: str = _ACCT_DEFAULT) -> int:
    """0 表示可以放行；否则返回还要等多少秒，该账号窗口才腾得下 need 个名额。

    按账号独立计算：A 账号传满 10 个不会再让 B 账号陪等（v75）。
    """
    now = time.time() if now is None else now
    return _wait_from(_history(now, account_id), need, now)


def _record_videos(pages: int, account_id: str = _ACCT_DEFAULT) -> None:
    """把本次上传的分P数记到该账号自己的窗口里，不动其它账号。"""
    now = time.time()
    by = _load_history()
    items = by.get(account_id, [])
    items.extend([now] * max(0, int(pages)))
    keep = [t for t in items if t > now - WINDOW_SECS][-WINDOW_MAX * 4:]
    by[account_id] = keep
    # 顺手清掉其它账号已经空掉的桶，避免文件里长期留着一堆空账号
    by = {k: [t for t in v if t > now - WINDOW_SECS] for k, v in by.items()}
    by = {k: v for k, v in by.items() if v}
    _save_history(by)


def _acct_label(account_id: str) -> str:
    """给日志用的可读账号名，找不到就退回 id 或「默认账号」。"""
    if not account_id:
        return "默认账号"
    try:
        for a in store.list_accounts():
            if str(a.get("id") or "") == account_id:
                return str(a.get("uname") or account_id)
    except Exception:
        pass
    return account_id[:10]


def _blocked_accounts(now: Optional[float] = None) -> Dict[str, float]:
    """返回 {account_id: 解除阻塞的时刻}，供主循环跳过这些账号的任务。

    合并三类阻塞：风控冷却、10 分钟窗口已满、_handle 刚判定的分P超额等待。
    只读一次历史文件，避免账号多时反复读盘。
    """
    now = time.time() if now is None else now
    out: Dict[str, float] = {}
    for acc, until in list(_risk_until.items()):
        if until > now:
            out[acc] = until
        else:
            _risk_until.pop(acc, None)
    for acc, until in list(_rate_until.items()):
        if until > now:
            out[acc] = max(out.get(acc, 0.0), until)
        else:
            _rate_until.pop(acc, None)
            _rate_wait.pop(acc, None)
    for acc, until in list(_account_wait.items()):
        if until > now:
            out[acc] = max(out.get(acc, 0.0), until)
        else:
            _account_wait.pop(acc, None)
    by = _load_history()
    for acc, times in by.items():
        h = sorted(t for t in times if t > now - WINDOW_SECS)
        w = _wait_from(h, 1, now)
        if w > 0:
            out[acc] = max(out.get(acc, 0.0), now + w)
    return out


def _pick_pending(skip: set):
    """取下一个可执行任务；skip 非空时排除这些账号。

    用可选参数调用，兼容把 next_pending 换成无参桩的离线测试。
    """
    fn = store.next_pending
    if not skip:
        return fn()
    try:
        return fn(skip_accounts=skip)
    except TypeError:
        return fn()


def _is_risk_error(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in ("-352", "-796", "-412", "风控", "验证码", "captcha",
                                "risk", "precondition"))


def _rate_kind(text: str) -> str:
    """cover=-702 封面接口；upload=406/429 预上传窗口；空串=不是限流。"""
    t = (text or "").lower()
    # B站实际抛的是 "NetworkException: 406" / "-702" 这类裸码，旧词表只认
    # 「状态码：406」等完整短语，真实限流一条都匹配不上。用独立数字边界匹配，
    # 避免把 BV 号或时间戳里的 406 误判成限流。
    if (any(k in t for k in ("too many requests", "rate limit", "请求过于频繁"))
            or re.search(r"(?<![0-9A-Za-z])(406|429)(?![0-9A-Za-z])", t)):
        return "upload"
    if any(k in t for k in ("-702", "请求频率过高")):
        return "cover"
    return ""


def rate_until() -> float:
    now = time.time()
    active = [until for until in _rate_until.values() if until > now]
    return max(active) if active else 0.0


def rate_wait() -> float:
    now = time.time()
    active = [(until, _rate_wait.get(acc, RATE_WAIT_COVER))
              for acc, until in _rate_until.items() if until > now]
    return float(max(active)[1]) if active else 0.0


def reset_rate_hits() -> None:
    """人工点「继续队列」：连续次数与冷却一起清。

    只清次数不够——_blocked_accounts 还会拿 _rate_until 把账号挡在外面，
    用户点了继续却仍然不动，看起来像按钮坏了。
    """
    _rate_state.clear()
    _rate_until.clear()
    _rate_wait.clear()


def _rate_limit_backoff(tid: str, task: Dict[str, Any], err: str) -> None:
    kind = _rate_kind(err)
    wait = RATE_WAIT_COVER if kind == "cover" else RATE_WAIT_UPLOAD
    giveup = RATE_GIVEUP_COVER if kind == "cover" else RATE_GIVEUP_UPLOAD
    acc = _acct(task)
    st = _rate_state.get(acc) or {}
    if st.get("kind") != kind:
        st = {"kind": kind, "n": 0}
    st["n"] = int(st.get("n") or 0) + 1
    _rate_state[acc] = st
    hits = st["n"]
    _rate_until[acc] = time.time() + wait
    _rate_wait[acc] = float(wait)
    label = _acct_label(acc)
    if kind == "cover":
        what = f"B站封面接口频率过高，账号「{label}」暂停 {wait} 秒后自动继续"
    else:
        what = f"B站上传额度受限，账号「{label}」暂停 {wait // 60} 分钟后自动继续"
    if hits >= giveup:
        pause(True, "rate")
        msg = f"连续 {hits} 次触发B站接口限流，队列已暂停，请稍后人工核对"
        store.add_global_log(f"账号「{label}」连续 {hits} 次被限流，队列已自动暂停", "warn")
    else:
        msg = f"{what}（第 {hits} 次）"
    # 上传前已 attempts+1；限流不是稿件错误，恢复成进入本轮前的值。
    store.update_task(tid, status="queued", attempts=int(task.get("attempts") or 0),
                      error=err, speed=0, stage="限流冷却", message=msg)
    _log(tid, f"{msg}：{err[:160]}", "warn")
    # 限流走独立的 rate 事件（可单独关），不再让 _summary 重复推一条 task 事件。
    _summary(task, msg, "warn", push=False)
    _notify("rate", "投稿被 B站 接口限流", f"{msg}。原始错误：{err[:160]}", "warn")
    _clear_cancel(tid)


def _risk_cooling(account_id: Optional[str] = None) -> float:
    """兼容旧调用：不传账号时返回所有账号中最长的剩余冷却。"""
    now = time.time()
    if account_id is not None:
        return max(0.0, _risk_until.get(account_id, 0.0) - now)
    return max([0.0] + [v - now for v in _risk_until.values()])


# ------------------------------------------------------------------ 主循环

def _idle_timeout(last_tick: float) -> float:
    """空闲时该睡多久：30 秒周期 tick 与「下一个定时任务进入候选」取小值。"""
    tick_in = max(0.05, 30.0 - (time.time() - last_tick))
    due = tick_in
    at = store.next_waiting_due(time.time())
    if at:
        now = time.time()
        # 到点前 10 分钟进入候选；已经在 10 分钟内就按到点时间
        ready_at = at - 600 if at - now > 600 else at
        due = min(due, max(0.05, ready_at - now))
    return due


# 主循环连续取到同一个任务、却始终没把它消费掉时的退避保护。
# 正常路径下 _handle 会返回明确的等待秒数；这个保护是为了兜住任何遗漏的
# 分支，避免主循环退化成零等待空转（实测旧版 0.3 秒能空跑 29 万次，CPU 跑满）。
_last_pending_id = ""
_pending_repeats = 0


def _loop() -> None:
    global _last_pending_id, _pending_repeats
    store.add_log(None, "队列服务已启动")
    last_tick = 0.0
    while not _stop.is_set():
        try:
            if _paused.is_set():
                # 额度满自动暂停：到点复查额度，腾出空位自动继续（人工暂停不受影响）
                if _maybe_resume_quota(time.time()):
                    continue
                # 暂停期间只等唤醒（恢复/新任务），封顶 2 秒后重新确认暂停态
                _wake.clear()
                _wake.wait(POLL_INTERVAL)
                continue
            if time.time() - last_tick > 30:
                # 定时倒计时改由前端按 publish_at 本地计算，后端不再每 30 秒
                # 逐条写 message（原来 N 个定时任务就是 N 次 commit + 前端整列重绘）
                _cleanup_approved()
                last_tick = time.time()
            # 先清唤醒标记再查库：任何在 clear 之后发生的状态变更都不会丢
            _wake.clear()
            # v75：算出被节流/风控的账号，取任务时跳过它们，
            # 这样 A 账号占满窗口不会让后面 B 账号的任务一起排队干等。
            blocked = _blocked_accounts()
            task = _pick_pending(set(blocked))
            if task:
                delay = _handle(task) or 0.0
                if delay > 0:
                    # 等待态：用可中断的 Event.wait，收到取消/暂停/新任务立刻醒
                    _wake.wait(min(float(delay), 30.0))
                    continue
                # 任务已消费（状态已变）。仍留一个极短的间隔，并对「同一任务
                # 连续被取出」做指数退避，保证任何分支都不会把 CPU 打满。
                tid = task["id"]
                if tid == _last_pending_id:
                    _pending_repeats += 1
                else:
                    _last_pending_id, _pending_repeats = tid, 1
                if _pending_repeats >= 3:
                    backoff = min(POLL_INTERVAL * (2 ** min(_pending_repeats - 3, 4)), 30.0)
                    _wake.wait(backoff)
                else:
                    _wake.wait(0.05)
            else:
                _last_pending_id, _pending_repeats = "", 0
                if blocked:
                    # 还有账号在节流/风控中、暂时没有别的任务可做：
                    # 睡到最早解除的那一刻（可中断），而不是固定空等
                    soon = min(blocked.values()) - time.time()
                    _wake.wait(min(max(soon, 0.05), 30.0))
                else:
                    _wake.wait(_idle_timeout(last_tick))
        except Exception as e:  # 队列本身不能因为单个任务崩掉
            store.add_log(None, f"队列异常：{type(e).__name__}: {e}", "error")
            _wake.wait(POLL_INTERVAL)
    store.add_log(None, "队列服务已停止")


def _ensure_watchdog() -> None:
    """有任务进入 uploading 时才拉起看门狗；空闲不挂线程。"""
    global _watchdog_thread
    with _state_lock:
        if _watchdog_thread and _watchdog_thread.is_alive():
            return
        t = threading.Thread(target=_watchdog_loop, name="publish-watchdog", daemon=True)
        _watchdog_thread = t
        t.start()


def _watchdog_loop() -> None:
    """独立线程盯 uploading 状态：worker 被卡住的子进程阻塞时，主循环自己救不了自己。

    没有 uploading 任务时线程自行退出，下次上传由 _ensure_watchdog() 再拉起。
    """
    while not _stop.wait(WATCHDOG_INTERVAL):
        try:
            if not store.list_by_status("uploading"):
                return
            _watchdog()
        except Exception as e:
            store.add_log(None, f"看门狗异常：{type(e).__name__}: {e}", "error")
    store.add_log(None, "看门狗已停止")


def _watchdog() -> None:
    """进度长时间不动的 uploading 判为卡死。

    只标记、不自动重排：这时候稿件可能已经提交成功，重排就是重复投稿。
    """
    now = time.time()
    for t in store.list_by_status("uploading"):
        stale_for = now - float(t.get("updated_at") or now)
        if stale_for < STUCK_TIMEOUT:
            continue
        store.update_task(
            t["id"], status="failed", finished_at=now, speed=0, error="stuck",
            message=f"上传超过 {STUCK_TIMEOUT // 60} 分钟没有任何进度更新，已判定卡死；"
                    f"稿件可能已发出，请先去创作中心核对再决定是否重投")
        _log(t["id"], f"看门狗：{int(stale_for // 60)} 分钟无进度更新，标记为卡死（不自动重投）", "error")
        _summary(t, "投稿卡死，已标记待人工确认", "error")


def _schedule_action(task: Dict[str, Any]) -> str:
    """定时稿在本轮的处置：
    'wait' 继续本地等待；'fail' 超 B站 15 天上限；'go' 放行进入上传流程。
    """
    publish_at = task.get("publish_at")
    if not publish_at:
        return "go"
    delta = float(publish_at) - time.time()
    # 过期保护：重启后发现已过期超过阈值，拦下等人工处理，不自动投
    if delta <= 0:
        cfg = store.load_config()
        if cfg.get("overdue_guard") and -delta > float(cfg.get("overdue_minutes") or 30) * 60:
            return "block"
    if (task.get("schedule_mode") or "bili") == "local":
        # 本地定时：到点就走，没到点无论多远都等，不受 B站 15 天约束
        return "wait" if delta > 0 else "go"
    if delta > 15 * 86400:
        return "fail"
    if delta > SCHEDULE_MIN_AHEAD:
        return "go"      # 提前量够，带 dtime 交给B站定时队列
    if delta > 0:
        return "wait"    # 不足 10 分钟，B站定时来不及，本地候到点再即时投放
    return "go"


def _schedule_wait_delay(delta: float) -> float:
    """定时任务的等待秒数：到点前 10 分钟才进候选，封顶 30 秒以便随时响应取消/暂停。

    这里的 600 是「进候选窗口」，和 SCHEDULE_MIN_AHEAD（交给B站定时所需提前量）
    数值恰好相同但语义不同，互不依赖。
    """
    target = delta - 600 if delta > 600 else delta
    return max(1.0, min(30.0, target))


def _fmt_remain(secs: float) -> str:
    """剩余时间的人类口径：天/小时/分钟，避免长定时只显示一个巨大的分钟数。"""
    if secs >= 86400:
        return f"{int(secs // 86400)} 天 {int((secs % 86400) // 3600)} 小时"
    if secs >= 3600:
        return f"{int(secs // 3600)} 小时 {int((secs % 3600) // 60)} 分"
    return f"{int(secs // 60)} 分钟"


def _refresh_countdowns() -> None:
    """刷新定时稿的「还剩多久」提示。"""
    for t in store.list_waiting_schedule():
        if not t.get("publish_at"):
            continue
        remain = float(t["publish_at"]) - time.time()
        if remain <= 0:
            msg = "正在到点投放…"
        elif t.get("schedule_mode") == "local":
            msg = f"等待到点自动上传（还剩 {_fmt_remain(remain)}）"
        elif remain > SCHEDULE_MIN_AHEAD:
            msg = f"将交给B站定时发布（{time.strftime('%m-%d %H:%M', time.localtime(float(t['publish_at'])))}）"
        else:
            msg = f"距发布不足 10 分钟，等待到点投放（还剩 {int(remain // 60)} 分钟）"
        if msg != (t.get("message") or ""):
            store.set_message(t["id"], msg)


def _log(task_id: str, msg: str, level: str = "info") -> None:
    store.add_log(task_id, msg, level)


# ---------------------------------------------------------------- 合集后台挂载
# 投稿成功后把稿件挂进合集要等B站把新稿入库（实测 30 秒上下），
# 这一步放在主循环里会直接卡住后面所有排队任务，所以丢给独立单线程串行处理。
_archive_q: "queue.Queue" = queue.Queue()
_archive_thread: Optional[threading.Thread] = None
_archive_lock = threading.Lock()


def _append_message(task_id: str, suffix: str) -> None:
    """在任务现有 message 末尾追加内容，避免覆盖主循环刚写入的结果。"""
    t = store.get_task(task_id) or {}
    store.update_task(task_id, message=((t.get("message") or "") + suffix))


def _archive_one(item: Dict[str, Any]) -> None:
    tid = item["task_id"]
    cfg = item["cfg"]
    aid = int(item["aid"])
    season_id = int(item["season_id"])
    title = item.get("title") or ""
    info: Dict[str, Any] = {}
    try:
        info = season.add_archive(cfg, aid, season_id, title)
    except Exception as e1:
        _log(tid, f"episodes/add 挂载失败，改用 switch 兜底：{type(e1).__name__}: {e1}", "warn")
        try:
            info = season.link_archive(cfg, aid, season_id, title)
        except Exception as e:
            _log(tid, f"加入合集失败：{type(e).__name__}: {e}", "error")
            _append_message(tid, f"（加入合集失败：{str(e)[:80]}）")
            return
    name = info.get("season_title") or season_id
    _log(tid, f"已加入合集「{name}」", "success")
    _append_message(tid, f"，已加入合集「{name}」")


def _archive_worker() -> None:
    while True:
        item = _archive_q.get()
        try:
            if item is not None:
                _archive_one(item)
        except Exception as e:      # 后台线程绝不能因为单条失败就退出
            try:
                _log(item.get("task_id") or "", f"合集后台任务异常：{type(e).__name__}: {e}", "error")
            except Exception:
                pass
        finally:
            _archive_q.task_done()


def _enqueue_archive(item: Dict[str, Any]) -> None:
    global _archive_thread
    with _archive_lock:
        if _archive_thread is None or not _archive_thread.is_alive():
            _archive_thread = threading.Thread(target=_archive_worker,
                                               name="season-archiver", daemon=True)
            _archive_thread.start()
    _archive_q.put(item)


def _stage_label(text: str) -> str:
    """把进度文本里的数字抹平，用来判断「阶段是否真的变了」。

    '上传 45% · 3.2 MB/s' 和 '上传 46% · 2.8 MB/s' 归一成同一个标签，
    于是一个分块一条日志的刷屏就没了；阶段切换（上传封面 / 提交稿件）仍然留痕。
    """
    return _NUM.sub("#", text or "")


def _stage(task_id: str, text: str, progress: float, speed: Optional[float] = None,
           force: bool = False) -> None:
    """更新任务进度。

    落库按「≥1 秒 或 ≥1% 变化」节流，日志只在阶段标签变化时写；
    force=True 用于关键节点，保证一定落库、一定留痕。
    """
    progress = round(max(0.0, min(1.0, float(progress or 0.0))), 4)
    label = _stage_label(text)
    now = time.time()
    with _state_lock:
        st = _last_stage
        # 阶段标签变了就立刻放行：换线路重传时进度会往回走（88% → 10%），
        # 只按「时间到 / 进度涨」判断会把这种关键切换整个吞掉
        label_changed = st["task_id"] != task_id or st["label"] != label
        due = (force or label_changed or progress >= 1.0
               or now - st["at"] >= STAGE_MIN_INTERVAL
               or progress - st["progress"] >= STAGE_MIN_DELTA)
        if due:
            st.update({"task_id": task_id, "at": now, "progress": progress, "label": label})
    if not due:
        return
    fields: Dict[str, Any] = {"stage": text, "progress": progress, "status": "uploading"}
    if speed is not None:
        fields["speed"] = round(float(speed), 2)
    store.update_task(task_id, **fields)
    if label_changed or force:
        _log(task_id, text)


def _reset_stage(task_id: str = "") -> None:
    """换任务或重启时清掉节流状态，避免新任务第一条进度被吞。"""
    with _state_lock:
        _last_stage.update({"task_id": task_id, "at": 0.0, "progress": -1.0, "label": ""})


def _notify(event: str, title: str, text: str, level: str = "info") -> None:
    """状态通知入口：没填 Webhook 就完全不触发，发送丢独立线程，失败不阻塞队列。

    调用点（v103）：_summary（投稿结果）、pause_for_quota（额度暂停 / 24h 封禁）、
    _maybe_resume_quota（封禁解除自动续投）、_rate_limit_backoff（限流卡住）。
    """
    try:
        cfg = store.load_config()
        opt = notify.normalize(cfg)
        # 「不填配置就完全不触发」：这里是第一道闸，连线程都不起。
        if not opt["enabled"] or not opt["url"] or not opt["events"].get(event, True):
            return
        key = f"{event}|{title}|{text}"
        now = time.time()
        with _notify_lock:
            if now - _notify_last.get(key, 0.0) < NOTIFY_DEDUP_SECS:
                return
            _notify_last[key] = now
            if len(_notify_last) > 200:
                _notify_last.clear()
        th = threading.Thread(target=_notify_send, args=(cfg, event, title, text, level),
                              daemon=True, name="notify-push")
        th.start()
    except Exception:  # noqa: BLE001  通知永远不能影响投稿
        pass


def _choose_channel(task: Dict[str, Any], cfg: Dict[str, Any],
                    log: Callable[[str, str], None]) -> str:
    """v105 起上传固定走内置官方接口：biliup 通道与实体校验已整体移除。

    保留函数壳是为了兼容现有调用点与回归测试（测试仍断言返回 official）。
    """
    return "official"


def _notify_send(cfg: Dict[str, Any], event: str, title: str,
                 text: str, level: str) -> None:
    """后台线程：真正发一次；失败只做节流式记日志，绝不抛到队列主循环。"""
    global _notify_fail_logged_at
    try:
        r = notify.dispatch(cfg, event, title, text, level)
        if r.get("sent"):
            return
        reason = r.get("reason") or r.get("detail") or "未知原因"
        if reason in ("状态通知已关闭", "未配置 Webhook 地址", "该事件已关闭推送"):
            return
        now = time.time()
        with _notify_lock:
            if now - _notify_fail_logged_at < NOTIFY_FAIL_LOG_GAP:
                return
            _notify_fail_logged_at = now
        try:
            store.add_global_log(f"状态通知推送失败（不影响投稿）：{reason}", "warn")
        except Exception:
            pass
    except Exception:  # noqa: BLE001
        pass


def _summary(task: Dict[str, Any], text: str, level: str = "info",
             push: bool = True) -> None:
    """把稿件最终结果写进总日志（带标题，过程仍在任务日志里），并按需推送。"""
    title = (task.get("title") or "未命名稿件").strip()[:40]
    store.add_global_log(f"《{title}》{text}", level)
    if push:
        _notify("task", f"《{title}》", text, level)


def _cleanup_files(task: Dict[str, Any]) -> int:
    """删掉任务上传时落盘的视频文件（不动封面等衍生文件）。"""
    removed = 0
    dirs = set()
    for f in task.get("files") or []:
        path = f.get("path") or ""
        if not path:
            continue
        dirs.add(os.path.dirname(path))
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
        except OSError:
            pass
    for d in dirs:
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
    return removed


def _delete_after() -> str:
    return str(store.load_config().get("delete_after") or "off").lower()


def _cleanup_approved() -> None:
    """「通过审核后删除」：定期回读稿件状态，state==0（开放浏览）即算通过。"""
    if _delete_after() != "approved":
        return
    # 只取待回读的任务，别每 30 秒把整张任务表（含 files JSON）全捞一遍
    for t in store.list_cleanup_pending():
        if t.get("cleanup") != "pending" or t.get("status") != "success":
            continue
        aid = int(t.get("aid") or 0) or season.aid_from_bvid(t.get("bvid") or "")
        if not aid:
            continue
        try:
            state = season.archive_state(store.load_config(), aid)
        except Exception:
            continue
        if state == 0:
            n = _cleanup_files(t)
            store.update_task(t["id"], cleanup="done")
            _log(t["id"], f"稿件已通过审核，已删除 {n} 个原文件", "success")


def cleanup_now() -> Dict[str, int]:
    """一键清理：删除所有成功任务的落盘副本，再清掉 uploads 里没有任何任务引用的孤儿文件。

    任务记录、BV 号、日志全部保留；失败/排队中任务的文件受引用保护，不会被删。
    """
    upload_dir = os.path.join(store.DATA_DIR, "uploads")
    referenced: set = set()
    task_files = 0
    task_bytes = 0
    done_tasks = 0

    for t in store.list_tasks():
        for f in t.get("files") or []:
            p = f.get("path") or ""
            if p:
                referenced.add(os.path.abspath(p))
        if t.get("status") == "success" and t.get("cleanup") != "done":
            for f in t.get("files") or []:
                p = f.get("path") or ""
                if p and os.path.isfile(p):
                    try:
                        task_bytes += os.path.getsize(p)
                    except OSError:
                        pass
            n = _cleanup_files(t)
            task_files += n
            done_tasks += 1
            store.update_task(t["id"], cleanup="done")

    # 孤儿文件：接口中途失败等情况下落盘、却没有任务引用的残留
    orphan_files = 0
    orphan_bytes = 0
    if os.path.isdir(upload_dir):
        for root, _dirs, names in os.walk(upload_dir):
            for name in names:
                p = os.path.abspath(os.path.join(root, name))
                if p in referenced:
                    continue
                try:
                    size = os.path.getsize(p)
                    os.remove(p)
                except OSError:
                    continue
                orphan_files += 1
                orphan_bytes += size
        # 删空目录（自底向上）
        for root, dirs, _files in os.walk(upload_dir, topdown=False):
            for d in dirs:
                p = os.path.join(root, d)
                try:
                    if not os.listdir(p):
                        os.rmdir(p)
                except OSError:
                    pass

    return {
        "tasks": done_tasks,
        "files": task_files,
        "orphan_files": orphan_files,
        "bytes_freed": task_bytes + orphan_bytes,
    }


def _handle(task: Dict[str, Any]) -> float:
    """处理一个候选任务。

    返回 0   —— 任务已被消费（状态已变，主循环可以立刻取下一个）；
    返回 >0  —— 本轮没有真正开始上传，主循环应等待这些秒再重试（可中断）。
    """
    global _risk_until
    tid = task["id"]

    if _cancelled(tid):
        store.update_task(tid, status="cancelled", finished_at=time.time(), message="已取消")
        _clear_cancel(tid)
        return 0.0

    # 1. 定时稿：local=程序本地候着到点自动上传；bili=远的交给B站定时、近的本地等
    publish_at = task.get("publish_at")
    now = time.time()
    delta = float(publish_at) - now if publish_at else None
    sched_mode = task.get("schedule_mode") or "bili"
    if publish_at:
        action = _schedule_action(task)
        if action == "fail":
            store.update_task(tid, status="failed", finished_at=now,
                              message="定时时间超过B站 15 天上限", error="schedule_too_far", speed=0)
            _log(tid, "定时时间超过 15 天，B站不支持", "error")
            _summary(task, "投稿失败：定时时间超过B站 15 天上限", "error")
            return 0.0
        if action == "wait":
            if sched_mode == "local":
                msg = f"等待到点自动上传（还剩 {_fmt_remain(delta)}）"
            else:
                msg = f"距发布不足 10 分钟，本地候到点投放（还剩 {int(delta // 60)} 分钟）"
            store.update_task(tid, status="waiting_schedule", message=msg)
            return _schedule_wait_delay(delta)
        if action == "block":
            mins = int(float(store.load_config().get("overdue_minutes") or 30))
            store.update_task(tid, status="blocked", finished_at=now,
                              message=f"本地定时稿件已过期超过 {mins} 分钟，未自动投稿，请手动处理",
                              error="overdue_skipped", speed=0)
            _log(tid, f"本地定时稿件已过期超过 {mins} 分钟，按设置拦下", "warn")
            _summary(task, f"已拦下：本地定时稿件过期超过 {mins} 分钟未自动投稿", "warn")
            return 0.0

    with _state_lock:
        _current["task_id"] = tid
        _current["started_at"] = time.time()
    _reset_stage(tid)

    try:
        # 2. 10 分钟窗口节流：只针对本任务所属账号，窗口腾不下就把该账号
        #    登记为「暂时跳过」，返回 0 让主循环立刻去取别的账号的任务。
        pages = max(1, len(task.get("files") or []))
        acc = _acct(task)
        if pages > WINDOW_MAX:
            _log(tid, f"本稿件有 {pages} 个分P，超过窗口容量 {WINDOW_MAX}，"
                      f"只能按窗口空余尽量节流", "warn")
        wait = _wait_seconds(pages, account_id=acc)
        if wait > 0:
            _account_wait[acc] = time.time() + wait
            # 文案只精确到分钟：否则每 2 秒轮询都会算出新字符串，
            # 判定成「消息变了」就一直写库 + 刷日志
            wait_min = (wait + 59) // 60
            msg = (f"节流等待：账号「{_acct_label(acc)}」最近 {WINDOW_SECS // 60} 分钟内"
                   f"已有 {len(_history(account_id=acc))} 个视频，本稿件占 {pages} 个名额，"
                   f"约 {wait_min} 分钟后放行（其它账号不受影响）")
            if (task.get("message") or "") != msg:
                store.update_task(tid, status="queued", message=msg, stage="节流等待", speed=0)
                _log(tid, msg, "warn")
            return 0.0

        # 3. 额度把关：10 秒内投稿入口刚查过就复用，否则现查（不吃 180 秒旧缓存）
        q = fresh_quota(account_id=task.get("account_id"))
        store.update_task(tid, quota_info=str(q.get("count", "")))
        # 判定顺序不能反：额度满时 q.error 一定非空。旧代码把「有 error 且未通过」
        # 一律当接口异常，于是额度满永远走不到下面的暂停分支，还被标成 blocked 死稿
        # （一屏红色、要逐个手工重新排队）。这里先分流三种情况再判定额度。
        if not q.get("logged_in"):
            store.update_task(tid, status="failed", finished_at=time.time(),
                              message=q.get("error") or "账号未登录", error="not_logged_in")
            _log(tid, f"无法投稿：{q.get('error')}", "error")
            _summary(task, f"投稿失败：{q.get('error')}", "error")
            return 0.0
        if q.get("incomplete") and not q.get("ok"):
            # 统计没抓全是临时故障：任务一律留队里。若已知处在 24h 封禁期，
            # 按封禁时刻转入自动等待（接口恢复后自动复查），不必人工点继续；
            # 否则数据不可信，保持人工暂停语义等人核对。
            if float(q.get("quota_ban_until") or 0) > time.time():
                pause_for_quota(q)
                store.add_global_log("额度统计不完整，但已知处于 24h 封禁期，队列按解封时刻自动等待", "warn")
            else:
                pause(True, "manual")
                store.add_global_log("额度统计不完整，队列已自动暂停（核对后点「继续队列」）", "warn")
            store.update_task(tid, status="queued", stage="额度待核对", speed=0,
                              message=f"额度校验未通过：{q.get('error')}")
            _log(tid, f"额度校验未通过：{q.get('error')}", "error")
            _summary(task, f"投稿被拦下：{q.get('error')}", "warn")
            return 0.0
        if not q.get("ok"):
            # 额度满是「时间问题」：暂停并记住预计腾出空位的时刻，到点自动继续，
            # 不需要人守着点「继续队列」。任务一律留 queued，绝不重投、绝不标红。
            pause_for_quota(q)
            # 两个口径都摆出来：「已确认」是B站能统计到的，「未确认」是本机刚投出去、
            # 列表还没显示的。加起来（去重后）才是真正用掉的额度。
            src = ""
            if isinstance(q.get("confirmed"), int):
                src = f"（已确认 {q['confirmed']} + 未确认 {q.get('pending') or 0}）"
            msg = (f"近 {q['hours']} 小时已投稿 {q['count']} 个{src}"
                   f"，上限 {q['limit']}，队列已暂停")
            # 留 queued 而不是 blocked：额度是「今天投满了」，窗口滑过去还能接着投，
            # 标红只会让一屏任务看着像失败，还得一个个重新排队。
            store.update_task(tid, status="queued", stage="额度已满",
                              message=msg, speed=0)
            _log(tid, msg, "error")
            store.add_global_log(
                f"24h 投稿数已达上限 {q['limit']}（已确认 {q.get('confirmed')} / "
                f"未确认 {q.get('pending') or 0} / 合计 {q['count']}），队列已自动暂停", "warn")
            _summary(task, f"投稿被拦下：24h 已投 {q['count']}/{q['limit']}，队列已暂停", "warn")
            return 0.0

        # 4. 补齐封面：封面在B站是可选项，抽帧失败绝不能把整条投稿判失败
        if not (task.get("cover_path") and os.path.isfile(task["cover_path"])):
            first = (task.get("files") or [{}])[0].get("path")
            out = os.path.join(store.DATA_DIR, "covers", f"{tid}.jpg")
            made = media.extract_cover(first, out) if first else None
            if made:
                store.update_task(tid, cover_path=made)
                task["cover_path"] = made
                _log(tid, "未指定封面，已从视频抽取一帧")
            else:
                # 抽不到画面（编码异常/关键帧稀疏/视频不可读）：用占位图继续投，
                # 发出去后在B站后台换封面即可，比整条任务失败白等一轮强。
                fb = media.fallback_cover(out)
                if fb:
                    store.update_task(tid, cover_path=fb)
                    task["cover_path"] = fb
                    _log(tid, "封面抽帧失败，已用占位图继续投稿（可在B站后台更换封面）", "warn")
                    store.add_global_log(
                        f"《{task.get('title') or tid}》封面抽帧失败，已用占位图继续投稿", "warn")
        if not task.get("cover_path"):
            store.update_task(tid, status="failed", finished_at=time.time(),
                              message="没有可用封面（ffmpeg 不可用，无法生成占位图）", error="no_cover", speed=0)
            _log(tid, "没有可用封面，已终止", "error")
            _summary(task, "投稿失败：没有可用封面", "error")
            return 0.0

        # 5. 选通道
        # 多账号：任务绑定的账号优先，没绑定就用当前生效账号
        cfg = store.account_cfg(task.get("account_id"))
        channel = _choose_channel(task, cfg, lambda m, lv="info": _log(tid, m, lv))

        store.update_task(tid, status="uploading", attempts=int(task.get("attempts") or 0) + 1,
                          stage="准备上传", progress=0.01)
        _ensure_watchdog()
        _log(tid, "使用通道：官方接口")

        def log_fn(msg: str) -> None:
            _log(tid, msg)

        def stage_fn(text: str, progress: float, speed: Optional[float] = None) -> None:
            if _cancelled(tid):
                raise RuntimeError("用户已取消")
            _stage(tid, text, progress, speed)

        # 只有「离发布还很久」且选了 B站定时的稿子才把发布时间交给B站
        send_task = dict(task)
        send_task["submit_dtime"] = (
            int(float(publish_at))
            if (publish_at and sched_mode != "local" and delta is not None
                and delta > SCHEDULE_MIN_AHEAD) else None
        )

        result = official.publish(send_task, cfg, log_fn, stage_fn)

        bvid = (result or {}).get("bvid") or ""
        aid = (result or {}).get("aid")
        # 立刻记进 24h 账本：B站列表要 1~3 分钟才显示这稿，额度判定不能等它。
        # 这一条是「未确认」的唯一来源，写完下一个任务就能看到 +1。
        try:
            # 账号键必须与额度查询侧一致：任务没绑账号时回落到当前生效账号，
            # 否则记录会落进空串桶，查询读的是 active 账号桶，未确认永远是 0。
            ledger.record(_acct(task) or store.active_account_id() or "",
                          task_id=tid, bvid=bvid, aid=aid)
        except Exception as e:   # noqa: BLE001  账本写失败不能影响投稿结果
            _log(tid, f"投稿账本写入失败（不影响投稿）：{type(e).__name__}: {e}", "warn")
        msg = "投稿成功" + (f"，BV 号 {bvid}" if bvid else "（BV 号未取到，请到创作中心确认）")
        if send_task.get("submit_dtime"):
            msg += "，已交给B站定时发布"
        elif publish_at:
            msg += "，按定时要求即时投放"
        _record_videos(len(task.get("files") or []), _acct(task))  # 只记入本账号窗口
        # 真的投出去了，之前攒的连续限流次数和冷却都清零；否则下次偶发一次
        # -702 就会被旧计数直接推到「暂停等人工」。
        _acc_done = _acct(task)
        _rate_state.pop(_acc_done, None)
        _rate_until.pop(_acc_done, None)
        _rate_wait.pop(_acc_done, None)
        store.update_task(tid, status="success", progress=1.0, stage="完成", speed=0,
                          bvid=bvid, aid=aid, finished_at=time.time(),
                          message=msg, error="")
        _log(tid, msg, "success")
        _summary(task, msg, "success")

        # 上传完成后删除原文件（只删本程序落盘的副本，不动用户本机原件）
        mode = _delete_after()
        if mode == "uploaded":
            n = _cleanup_files(task)
            store.update_task(tid, cleanup="done")
            if n:
                _log(tid, f"已按设置删除 {n} 个原文件", "success")
        elif mode == "approved":
            store.update_task(tid, cleanup="pending")
            _log(tid, "已按设置标记：通过审核后删除原文件", "info")

        # 投稿成功后挂合集：只有拿到 aid 才能挂，失败也只记日志，不影响投稿结果。
        # 合集挂载要等B站把新稿入库（实测 30 秒上下），放在主循环里会直接把队列卡住，
        # 所以交给后台单线程串行处理：投稿结果先落地，合集随后补挂。
        season_id = int(task.get("season_id") or 0)
        if season_id:
            if not aid and bvid:
                aid = season.aid_from_bvid(bvid)
            if aid:
                _stage(tid, "加入合集", 0.99, force=True)
                _enqueue_archive({
                    "task_id": tid,
                    "cfg": cfg,
                    "aid": int(aid),
                    "season_id": season_id,
                    "title": task.get("title") or "",
                    "message": msg,
                })
            else:
                _log(tid, "加入合集失败：没拿到 aid，无法挂合集", "error")
                msg = msg + "（加入合集失败：没拿到 aid）"
        store.update_task(tid, status="success", progress=1.0, stage="完成", message=msg)

    except Exception as e:
        attempts = int(task.get("attempts") or 0) + 1
        max_attempts = int(task.get("max_attempts") or 2)
        err = f"{type(e).__name__}: {e}"
        # 限流（封面 -702 / 预上传 406、429）与验证码风控是两类问题：前者是平台
        # 窗口，等 1 或 5 分钟就能再探（406 的 10 分钟窗口内 5min/10min 各探一次），
        # 不该消耗稿件的失败重试次数；后者才按账号冷却。限流判断放在风控前面，
        # 避免错误文案交叉时被风控分支吃掉。
        if _rate_kind(err):
            _rate_limit_backoff(tid, task, err)
            return 0.0
        if _is_risk_error(err):
            wait_s = RISK_WAIT
            acc = _acct(task)
            _risk_until[acc] = time.time() + wait_s
            label = _acct_label(acc)
            store.update_task(tid, status="queued", error=err, speed=0,
                              message=f"账号「{label}」触发验证码，该账号暂停 {wait_s // 60} 分钟后自动重试")
            _log(tid, f"账号「{label}」触发验证码，暂停 {wait_s // 60} 分钟（其它账号继续）：{err[:160]}", "warn")
            _summary(task, f"账号「{label}」触发验证码，{wait_s // 60} 分钟后重试", "warn")
            _clear_cancel(tid)
            return 0.0
        if _cancelled(tid):
            store.update_task(tid, status="cancelled", finished_at=time.time(),
                              message="已取消")
            _clear_cancel(tid)
            _log(tid, "任务已取消", "warn")
            _summary(task, "已取消", "warn")
        elif attempts < max_attempts:
            store.update_task(tid, status="queued", attempts=attempts, error=err,
                              message=f"第 {attempts} 次失败，稍后重试：{err[:120]}")
            _log(tid, f"第 {attempts} 次投稿失败，将重试：{err}", "error")
            _summary(task, f"第 {attempts} 次投稿失败，准备重试", "warn")
            # 不在 worker 线程里阻塞 sleep：把退避交给主循环的 Event.wait，
            # 期间用户取消 / 暂停队列都能立刻生效
            return float(min(30, 5 * attempts))
        else:
            store.update_task(tid, status="failed", attempts=attempts, error=err,
                              finished_at=time.time(), message=err[:300])
            _log(tid, f"投稿失败：{err}", "error")
            _summary(task, f"投稿失败：{err[:120]}", "error")
    finally:
        with _state_lock:
            _current["task_id"] = ""
            _current["started_at"] = 0.0
        _quota_cache.pop(task.get("account_id") or store.active_account_id(), None)
        _quota_cache.pop(store.active_account_id(), None)
        _reset_stage()
        _stop.wait(1.0)   # 用 Event.wait：收到停止信号能立刻退出，不用干等
    return 0.0
