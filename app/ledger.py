"""本机投稿账本：把「已经占掉 24h 额度」的每一篇稿件立刻记下来。

为什么要另建账本，而不是只数创作者中心列表：

B站创作者中心（member.bilibili.com/x/web/archives）对新提交的稿件有入库延迟，
实测从「提交成功」到「在列表里能查到」要几十秒到几分钟。而队列是几秒一稿的节奏，
于是投稿前那一刻查到的永远是这个账号「几分钟前的样子」——每个任务都看到 48/50、
49/50，看起来还有额度，实际早就顶到 51 了。

所以 24h 额度 = 已确认（B站列表能统计到的）+ 未确认（本机投出去、还没进列表的）：

  额度 = |列表稿件 ∪ 账本稿件|（按 BV 号去重，没有 BV 号时用 aid）

v106 起账本记录分三种状态，**一次投稿从开始上传那一刻就算额度**，不再等结果落地：

  inflight   开始上传 / 已提交但结果未知 —— 先占一个名额
  confirmed  确认投出（拿到 BV/aid，或B站已受理）
  uncertain  已进入提交阶段后失败（超时/中断） —— 稿件可能已发出，保守继续占额度

这样即使B站列表延迟、甚至提交请求超时，额度也不会被算少：
占位行没有 BV/aid 时按「一篇」计入 pending，确认后补上身份键——
同一篇稿在两边都出现时只算一次，不会重复计数、也不会虚高误拦。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from . import store

LEDGER_NAME = "submit_ledger.json"

# 记录状态：见模块注释
STATE_INFLIGHT = "inflight"
STATE_CONFIRMED = "confirmed"
STATE_UNCERTAIN = "uncertain"

# 保留 26 小时：24 小时窗口外的记录立即失去意义，多留 2 小时给时钟抖动与排查
KEEP_SECS = 26 * 3600
MAX_PER_ACCOUNT = 400

_lock = threading.Lock()


def ledger_file() -> str:
    """随 store.DATA_DIR 走：测试会把它指到临时目录，所以不能在 import 时定死路径。"""
    return os.path.join(store.DATA_DIR, LEDGER_NAME)


def _norm(account_id: Optional[str]) -> str:
    """账号键归一：None / 空串都表示「默认账号」，与 worker._acct 的口径一致。"""
    return str(account_id or "")


def _ident(bvid: Any, aid: Any) -> tuple:
    """归一身份键：BV 号大小写敏感只能 strip；aid 转 int，非法值按 0。"""
    b = str(bvid or "").strip()
    try:
        a = int(aid or 0)
    except (TypeError, ValueError):
        a = 0
    return b, a


def _load() -> Dict[str, List[Dict[str, Any]]]:
    try:
        with open(ledger_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            out: Dict[str, List[Dict[str, Any]]] = {}
            for k, v in data.items():
                if isinstance(v, list):
                    out[str(k)] = [x for x in v if isinstance(x, dict)]
            return out
    except (OSError, ValueError):
        pass
    return {}


def _save(by: Dict[str, List[Dict[str, Any]]]) -> None:
    """原子写入；失败必须抛给调用方，不能假装投稿已记账。"""
    path = ledger_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(by, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _prune(rows: List[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    keep = [r for r in rows if float(r.get("ts") or 0) > now - KEEP_SECS]
    return keep[-MAX_PER_ACCOUNT:]


def _match(rows: List[Dict[str, Any]], task_id: str = "",
           bvid: str = "", aid: int = 0) -> int:
    """按任务号 / BV / aid 找同一条稿件的记录下标，找不到返回 -1。"""
    for i, row in enumerate(rows):
        if task_id and row.get("task_id") == task_id:
            return i
        if bvid and row.get("bvid") == bvid:
            return i
        if aid and int(row.get("aid") or 0) == aid:
            return i
    return -1


def reserve(account_id: Optional[str], task_id: str = "",
            ts: Optional[float] = None) -> None:
    """投稿开始前先占一个额度名额（不等结果、不等B站入库）。

    从「上传第一个分块」到「确认投出」可能有好几分钟，这段时间这篇稿在B站
    列表里根本查不到。先记一行 inflight，下一个任务的额度判定就能立刻 +1，
    不会出现「投完还能再投一篇」的越界。
    """
    now = time.time() if ts is None else float(ts)
    key = _norm(account_id)
    task_id = str(task_id or "")
    with _lock:
        by = _load()
        rows = _prune(by.get(key, []), time.time())
        idx = _match(rows, task_id=task_id) if task_id else -1
        if idx >= 0:
            row = rows[idx]
            if row.get("state") in (STATE_CONFIRMED, STATE_UNCERTAIN):
                # 已确认 / 已判定可能投出：占位不需要重写，免得把窗口时间往后拖
                return
            row["ts"] = now
            row["state"] = STATE_INFLIGHT
        else:
            rows.append({"ts": now, "task_id": task_id, "bvid": "", "aid": 0,
                         "state": STATE_INFLIGHT})
        by[key] = _prune(rows, time.time())
        _save(by)


def confirm(account_id: Optional[str], task_id: str = "", bvid: str = "",
            aid: Any = None, ts: Optional[float] = None) -> None:
    """确认这篇稿真的投出去了：把占位行补成 confirmed（带 BV/aid 身份键）。

    时间取确认时刻（≈B站 ctime），宁可让窗口晚一点滑出，也不提前放行。
    """
    now = time.time() if ts is None else float(ts)
    b, a = _ident(bvid, aid)
    key = _norm(account_id)
    task_id = str(task_id or "")
    with _lock:
        by = _load()
        rows = _prune(by.get(key, []), time.time())
        idx = _match(rows, task_id=task_id, bvid=b, aid=a)
        if idx >= 0:
            row = dict(rows[idx])
            row["ts"] = now
            row["task_id"] = row.get("task_id") or task_id
            row["bvid"] = b or row.get("bvid") or ""
            row["aid"] = a or int(row.get("aid") or 0)
            row["state"] = STATE_CONFIRMED
            # 同一篇稿的其它副本（只给 aid / 只给 BV / 占位行）一起并掉
            rest: List[Dict[str, Any]] = []
            for j, r in enumerate(rows):
                if j == idx:
                    continue
                same_task = bool(task_id) and r.get("task_id") == task_id
                same_bvid = bool(b) and r.get("bvid") == b
                same_aid = bool(a) and int(r.get("aid") or 0) == a
                if same_task or same_bvid or same_aid:
                    continue
                rest.append(r)
            rest.append(row)
            rows = rest
        else:
            rows.append({"ts": now, "task_id": task_id, "bvid": b, "aid": a,
                         "state": STATE_CONFIRMED})
        by[key] = _prune(rows, time.time())
        _save(by)


def mark_uncertain(account_id: Optional[str], task_id: str = "") -> None:
    """已进入提交阶段后失败（超时/中断/进程退出）：稿件可能已经发出。

    不能撤掉占位，否则下一轮就会按B站那份「还没有这篇稿」的列表算额度，
    一路放行到越界。保持计入，状态标成 uncertain 便于排查。
    """
    key = _norm(account_id)
    task_id = str(task_id or "")
    with _lock:
        by = _load()
        rows = _prune(by.get(key, []), time.time())
        idx = _match(rows, task_id=task_id) if task_id else -1
        if idx < 0:
            rows.append({"ts": time.time(), "task_id": task_id, "bvid": "", "aid": 0,
                         "state": STATE_UNCERTAIN})
        else:
            rows[idx]["state"] = STATE_UNCERTAIN
        by[key] = _prune(rows, time.time())
        _save(by)


def release(account_id: Optional[str], task_id: str = "") -> bool:
    """撤掉一条「占位」记录：确定这一轮没有提交出去（如 406/429、风控、取消）。

    只删 inflight；confirmed / uncertain 绝不回收，它们代表真占用的额度。
    """
    key = _norm(account_id)
    task_id = str(task_id or "")
    with _lock:
        by = _load()
        rows = by.get(key, [])
        keep = [r for r in rows
                if not (r.get("state") == STATE_INFLIGHT
                        and (not task_id or r.get("task_id") == task_id))]
        changed = len(keep) != len(rows)
        if changed:
            by[key] = keep
            _save(by)
    return changed


def record(account_id: Optional[str], task_id: str = "", bvid: str = "",
           aid: Any = None, ts: Optional[float] = None) -> None:
    """记一次投稿成功（v106 起等价于 confirm，保留旧签名以兼容调用点与测试）。

    bvid / aid 是跟B站列表对账用的身份键：同一稿件在两边都出现时按它去重，
    所以能拿到就必须记下来（少数通道只回 aid，那就用 aid）。
    """
    confirm(account_id, task_id=task_id, bvid=bvid, aid=aid, ts=ts)


def recent(account_id: Optional[str], hours: int = 24,
           now: Optional[float] = None) -> List[Dict[str, Any]]:
    """窗口内的全部记录（含 inflight / uncertain：占掉的额度同样算数）。"""
    now = time.time() if now is None else float(now)
    cutoff = now - max(1, int(hours)) * 3600
    with _lock:
        rows = _load().get(_norm(account_id), [])
    return [r for r in rows if float(r.get("ts") or 0) > cutoff]


def count(account_id: Optional[str], hours: int = 24,
          now: Optional[float] = None) -> int:
    return len(recent(account_id, hours, now))


def inflight_count(account_id: Optional[str], hours: int = 24) -> int:
    """还没落地的记录数（上传中 / 结果未知），用于界面说明「未确认」的构成。"""
    return sum(1 for r in recent(account_id, hours)
               if r.get("state") in (STATE_INFLIGHT, STATE_UNCERTAIN))


def reconcile(valid_task_ids: Iterable[str]) -> int:
    """启动时收拾孤儿占位：任务已被删除/清空，对应的 inflight 行要一起撤掉。

    uncertain 行不动：它代表「可能已投出去」，宁可多占一天额度，也不能放行。
    """
    valid = {str(x) for x in (valid_task_ids or [])}
    removed = 0
    with _lock:
        by = _load()
        for key, rows in list(by.items()):
            keep = [r for r in rows
                    if not (r.get("state") == STATE_INFLIGHT
                            and r.get("task_id") and r["task_id"] not in valid)]
            if len(keep) != len(rows):
                removed += len(rows) - len(keep)
                by[key] = keep
        if removed:
            _save(by)
    return removed


def drop(account_id: Optional[str]) -> None:
    """账号被删除时清掉它的账本，避免同 id 重建后继承旧记录。"""
    key = _norm(account_id)
    with _lock:
        by = _load()
        if key in by:
            by.pop(key, None)
            _save(by)
