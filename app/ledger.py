"""本机投稿账本：记下每一次「真的提交成功」的稿件，用来把 24h 额度算准。

为什么要另建账本，而不是只数创作者中心列表：

B站创作者中心（member.bilibili.com/x/web/archives）对新提交的稿件有入库延迟，
实测从「提交成功」到「在列表里能查到」要几十秒到几分钟。而队列是几秒一稿的节奏，
于是投稿前那一刻查到的永远是这个账号「几分钟前的样子」——每个任务都看到 48/50、
49/50，看起来还有额度，实际早就顶到 51 了。

所以 24h 额度 = 已确认（B站列表能统计到的）+ 未确认（本机投出去、还没进列表的）：

  额度 = |列表稿件 ∪ 账本稿件|（按 BV 号去重，没有 BV 号时用 aid）

- 账本无延迟：本程序投出去几个就是几个，立刻计入「未确认」；
- B站列表覆盖「在网页/手机端手动投的」以及换机前投的，防止账本偏小；
- 同一篇稿在两边都出现时只算一次，不会重复计数、也不会虚高误拦。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from . import store

LEDGER_NAME = "submit_ledger.json"


def ledger_file() -> str:
    """随 store.DATA_DIR 走：测试会把它指到临时目录，所以不能在 import 时定死路径。"""
    return os.path.join(store.DATA_DIR, LEDGER_NAME)

# 保留 26 小时：24 小时窗口外的记录立即失去意义，多留 2 小时给时钟抖动与排查
KEEP_SECS = 26 * 3600
MAX_PER_ACCOUNT = 400

_lock = threading.Lock()


def _norm(account_id: Optional[str]) -> str:
    """账号键归一：None / 空串都表示「默认账号」，与 worker._acct 的口径一致。"""
    return str(account_id or "")


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


def record(account_id: Optional[str], task_id: str = "", bvid: str = "",
           aid: Any = None, ts: Optional[float] = None) -> None:
    """记一次投稿成功。调用点在工作线程里，投稿成功后立刻写，不带任何缓存。

    bvid / aid 是跟 B站列表对账用的身份键：同一稿件在两边都出现时按它去重，
    所以能拿到就必须记下来（少数通道只回 aid，那就用 aid）。
    """
    now = time.time() if ts is None else float(ts)
    if now <= time.time() - KEEP_SECS:
        # 补写一条已经滑出保留期的记录没有意义，直接丢掉，免得文件里留废条目
        return
    key = _norm(account_id)
    try:
        aid_i = int(aid or 0)
    except (TypeError, ValueError):
        aid_i = 0
    with _lock:
        by = _load()
        # 裁剪一律以「当下」为准：补写历史记录时不能把过期条目带回来
        rows = _prune(by.get(key, []), time.time())
        # 投稿成功后的收尾可能因进程重启/重复回调再执行一次；同任务或同稿件只记一次。
        for row in rows:
            same_task = bool(task_id) and row.get("task_id") == task_id
            same_bvid = bool(bvid) and row.get("bvid") == bvid
            same_aid = bool(aid_i) and int(row.get("aid") or 0) == aid_i
            if same_task or same_bvid or same_aid:
                return
        rows.append({"ts": now, "task_id": task_id or "", "bvid": bvid or "", "aid": aid_i})
        by[key] = rows
        _save(by)


def recent(account_id: Optional[str], hours: int = 24,
           now: Optional[float] = None) -> List[Dict[str, Any]]:
    now = time.time() if now is None else float(now)
    cutoff = now - max(1, int(hours)) * 3600
    with _lock:
        rows = _load().get(_norm(account_id), [])
    return [r for r in rows if float(r.get("ts") or 0) > cutoff]


def count(account_id: Optional[str], hours: int = 24,
          now: Optional[float] = None) -> int:
    return len(recent(account_id, hours, now))


def drop(account_id: Optional[str]) -> None:
    """账号被删除时清掉它的账本，避免同 id 重建后继承旧记录。"""
    key = _norm(account_id)
    with _lock:
        by = _load()
        if key in by:
            by.pop(key, None)
            _save(by)
