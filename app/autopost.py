"""录播自动投稿（v112）：多规则监视目录 → 录制完成判定 → 套模板入队 → 投成功归档。

其它录播软件（录播姬等）负责产生视频，本模块只负责「投出去」：
- 多规则：每条规则独立配置 监视目录/分P或单独/模板/账号/标题模板/静默期等，
  总开关 + 规则开关两级控制。
- 录制完成判定：文件大小 + mtime 在 stable_secs 内完全不变，且 ffprobe 能读出
  时长（正在录制的 flv/mp4 容器不完整，读不出时长）。没装 ffprobe 时退化为
  纯静默期判定。绝不上传还在写的文件。
- single：每个完成视频独立一稿；multi：同子文件夹 + 同日期合并为多分P，
  组内所有文件都完成、且 group_idle_secs 内没有新文件出现才入队（断播分段
  要等齐）。平铺在根目录的文件按「文件名日期前缀」分组。
- 原件直接引用上传（零拷贝）；投稿成功后由 worker 回调 archive_originals，
  把原件移入监视目录下「已投稿」子文件夹（保留原子目录结构）。失败/取消的
  稿件原件留在原地，可在队列里重试。worker 的「上传后删除」只清理程序落盘
  副本，不会碰监视目录里的原件。
- 防重投：autopost_seen 台账登记原件；活动任务始终占住路径，终态任务再按
  路径+体积+mtime 签名判断。删除任务表示放弃，不会因台账清理而重新入队。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import media, store

# 录制/下载中的临时后缀，一律跳过
SKIP_TAIL = (".part", ".tmp", ".downloading", ".crdownload", ".!qb", ".td", ".partial")
# 归档目录名：扫描时跳过，避免把已投成功的原件再投一遍
ARCHIVE_DIR = "已投稿"

RULE_DEFAULTS: Dict[str, Any] = {
    "id": "", "name": "", "enabled": True, "watch_dir": "", "mode": "single",
    "template_id": 0, "account_id": "", "title_tpl": "",
    "stable_secs": 120, "group_idle_secs": 600, "min_size_mb": 10,
    # v113：同一天里相邻文件 mtime 差超过这个值就切成不同场次，
    # 下午场+晚场不会硬合并成一稿（默认 2 小时）
    "session_gap_secs": 7200,
}

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_wake = threading.Event()
_lock = threading.Lock()
# path -> (size, mtime, first_seen)：大小/mtime 一变就重置静默计时
_sig: Dict[str, Tuple[int, float, float]] = {}
# path -> 上次告警时刻：probe 失败/超体积的提示节流，避免每轮刷日志
_warned: Dict[str, float] = {}
_status: Dict[str, Any] = {"last_scan": 0.0, "last_error": "", "ingested": 0,
                           "pending": []}
_last_prune = 0.0        # v113：seen 台账上次清理时刻（每小时一次）

_DATE_RE = re.compile(r"(20\d{2})[-_.年/]?(\d{1,2})[-_.月/]?(\d{1,2})")


def cfg() -> Dict[str, Any]:
    raw = store.load_config().get("autopost") or {}
    rules = []
    for r in raw.get("rules") or []:
        if isinstance(r, dict):
            merged = dict(RULE_DEFAULTS)
            merged.update(r)
            rules.append(merged)
    return {"enabled": bool(raw.get("enabled")),
            "scan_secs": int(raw.get("scan_secs") or 30),
            "rules": rules}


def wake() -> None:
    _wake.set()


def start() -> None:
    """随服务启动常驻；没有启用的规则时只空转等待，不碰磁盘。"""
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="autopost", daemon=True)
        _thread.start()


def _loop() -> None:
    global _last_prune
    while not _stop.is_set():
        c = cfg()
        active = [r for r in c["rules"] if r.get("enabled") and r.get("watch_dir")]
        scan_new = bool(c["enabled"] and active)
        try:
            # 总开关只控制是否发现新文件；已入队任务即使关闭开关也要能完成重判定。
            if scan_new or store.autopost_recheck_needed():
                scan_once(c, scan_new=scan_new)
        except Exception as e:  # noqa: BLE001  单轮扫描失败不能杀死线程
            _status["last_error"] = f"{type(e).__name__}: {e}"
            try:
                store.add_global_log(f"自动投稿扫描失败：{type(e).__name__}: {e}", "warn")
            except Exception:
                pass
        if scan_new:
            # 只清任务已不存在且原路径已消失的旧记录；仍存在的忽略文件永久防重。
            now = time.time()
            if now - _last_prune > 3600:
                _last_prune = now
                try:
                    store.autopost_prune(30)
                except Exception:  # noqa: BLE001
                    pass
        _wake.wait(max(10, int(c["scan_secs"])))
        _wake.clear()


def status() -> Dict[str, Any]:
    c = cfg()
    with _lock:
        running = (bool(_thread and _thread.is_alive()) and c["enabled"]
                   and any(r.get("enabled") and r.get("watch_dir") for r in c["rules"]))
        st = dict(_status)
        st["pending"] = list(_status.get("pending") or [])
    st.update({"enabled": c["enabled"], "running": running, "config": c,
               "seen_total": store.autopost_seen_count(),
               "recent": store.autopost_recent(8)})
    return st


# ---------------------------------------------------------------- 扫描

def _candidates(rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    """遍历一条规则的监视目录，收集候选视频（不做稳定性判定）。"""
    root = str(rule.get("watch_dir") or "")
    min_bytes = int(rule.get("min_size_mb") or 0) * 1024 * 1024
    out: List[Dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d != ARCHIVE_DIR]
        for name in filenames:
            if name.startswith("."):
                continue
            low = name.lower()
            if low.endswith(SKIP_TAIL):
                continue
            if os.path.splitext(low)[1] not in media.VIDEO_EXTS:
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            rel = os.path.relpath(path, root)
            out.append({"path": path, "name": name, "rel": rel,
                        "size": int(st.st_size), "mtime": float(st.st_mtime),
                        "too_small": st.st_size < min_bytes})
    return out


def _stable(f: Dict[str, Any], rule: Dict[str, Any], now: float) -> bool:
    """大小+mtime 静默期判定：任一变化都重置计时。"""
    path, size, mtime = f["path"], f["size"], f["mtime"]
    with _lock:
        old = _sig.get(path)
        if not old or old[0] != size or abs(old[1] - mtime) >= 1.0:
            _sig[path] = (size, mtime, now)
            return False
        first_seen = old[2]
    need = max(30, int(rule.get("stable_secs") or 120))
    return (now - first_seen >= need) and (now - mtime >= need)


def _probe_ready(f: Dict[str, Any], now: float) -> Tuple[bool, Dict[str, Any]]:
    """ffprobe 能读出时长才算容器完整。没装 ffprobe 时退化为静默期判定。"""
    info = media.probe(f["path"])
    if not media.have_ffmpeg():
        _throttled_warn("__ffmpeg_missing__", now,
                        "自动投稿：未安装 ffmpeg/ffprobe，目前仅按静置期判断录制完成；"
                        "建议安装后再长期无人值守运行")
        return True, info
    if float(info.get("duration") or 0) > 0:
        return True, info
    _throttled_warn(f["path"], now,
                    f"自动投稿：《{f['name']}》已静置但读不出时长，可能仍在录制或文件损坏，继续等待")
    return False, info


def _throttled_warn(path: str, now: float, msg: str) -> None:
    with _lock:
        last = _warned.get(path, 0.0)
        if now - last < 600:
            return
        _warned[path] = now
    try:
        store.add_global_log(msg, "warn")
    except Exception:
        pass


def _group_of(f: Dict[str, Any]) -> Tuple[str, str, str]:
    """分P分组：(group_key, 主播, 日期)。

    子文件夹里的文件：主播=第一层子文件夹名；根目录平铺：主播=文件名里日期
    之前的前缀。日期取文件名里的 YYYY-MM-DD / YYYYMMDD 等写法，取不到用 mtime。
    根目录且文件名无日期：每个文件自成一组（宁可单独投，不乱合并）。
    """
    parts = f["rel"].replace("\\", "/").split("/")
    top = parts[0] if len(parts) > 1 else ""
    m = _DATE_RE.search(f["name"])
    date = ""
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            date = f"{y}-{mo:02d}-{d:02d}"
    if not date:
        date = time.strftime("%Y-%m-%d", time.localtime(f["mtime"]))
    if top:
        streamer = top
    elif m:
        streamer = f["name"][:m.start()].rstrip("-_. ") or "录播"
    else:
        streamer = os.path.splitext(f["name"])[0]
    # 分P必须限定在完整相对父目录，不能把同一主播不同玩法/场次的子目录混在一起。
    parent = "/".join(parts[:-1])
    group_id = parent or streamer
    return f"{group_id}|{date}", streamer, date


def scan_once(c: Optional[Dict[str, Any]] = None, now: Optional[float] = None,
              scan_new: bool = True) -> int:
    """扫描一轮；scan_new=False 时只重判定已入队任务，不发现新文件。"""
    c = c or cfg()
    now = time.time() if now is None else float(now)
    created = 0
    pending: List[Dict[str, Any]] = []
    errors: List[str] = []
    alive: set = set()
    # v113：被 worker 以「源文件已变化」退回的稿件，重新走静默期判定，
    # 录完后原地更新签名继续投（不产生重复任务）
    try:
        _recheck_queued(c, now, alive)
    except Exception as e:  # noqa: BLE001
        errors.append(f"源文件重判定失败：{type(e).__name__}: {e}")
    for rule in c["rules"]:
        if not scan_new:
            break
        if not rule.get("enabled") or not rule.get("watch_dir"):
            continue
        root = str(rule["watch_dir"])
        if not os.path.isdir(root):
            errors.append(f"规则「{rule.get('name') or root}」目录不存在：{root}")
            continue
        try:
            created += _scan_rule(rule, now, pending, alive)
        except Exception as e:  # noqa: BLE001  一条规则失败不影响其它规则
            errors.append(f"规则「{rule.get('name') or root}」扫描失败：{type(e).__name__}: {e}")
    with _lock:
        for p in list(_sig):
            if p not in alive:
                _sig.pop(p, None)
                _warned.pop(p, None)
        _status["last_scan"] = now
        _status["last_error"] = "；".join(errors)
        _status["pending"] = pending[:80]
    return created


def _sessions(members: List[Dict[str, Any]], gap: int) -> List[List[Dict[str, Any]]]:
    """v113：按相邻文件 mtime 差切场次。超过 gap 就另起一组，
    同一天下午场+晚场不会被硬合并成一稿 P1…Pn。"""
    ms = sorted(members, key=lambda f: (f["mtime"], f["name"]))
    out: List[List[Dict[str, Any]]] = [[ms[0]]]
    for f in ms[1:]:
        if f["mtime"] - out[-1][-1]["mtime"] > gap:
            out.append([f])
        else:
            out[-1].append(f)
    return out


def _recheck_queued(c: Dict[str, Any], now: float, alive: set) -> None:
    """v113：重判定「源文件已变化」的在队稿件。

    worker 上传前发现签名不一致会把任务退回 queued；这里对源文件重新走
    静默期+probe 判定，全部录完后原地更新 files 签名并清空提示，worker
    下一轮即可继续投。文件被删的由 worker 直接判失败，不在此处理。
    """
    rules_by_id = {str(r.get("id")): r for r in c.get("rules") or []}
    for t in store.list_tasks():
        if not t.get("autopost_meta") or t.get("status") != "queued":
            continue
        if "源文件已变化" not in str(t.get("message") or ""):
            continue
        try:
            meta = json.loads(t.get("autopost_meta") or "{}")
        except Exception:  # noqa: BLE001
            continue
        rule = rules_by_id.get(str(meta.get("rule_id"))) or {}
        stable_secs = int(rule.get("stable_secs") or 120)
        files = t.get("files") or []
        ready: Dict[str, Tuple[Any, Dict[str, Any]]] = {}
        ok = bool(files)
        for f in files:
            path = str(f.get("path") or "")
            try:
                st = os.stat(path)
            except OSError:
                ok = False
                break
            alive.add(path)
            ff = {"path": path, "name": os.path.basename(path),
                  "size": int(st.st_size), "mtime": float(st.st_mtime)}
            if not _stable(ff, {"stable_secs": stable_secs}, now):
                ok = False
                break
            probe_ok, info = _probe_ready(ff, now)
            if not probe_ok:
                ok = False
                break
            ready[path] = (st, info)
        if not ok:
            continue
        new_files = []
        for f in files:
            st, info = ready[f["path"]]
            nf = dict(f)
            nf["size"] = int(st.st_size)
            nf["duration"] = float(info.get("duration") or 0)
            nf["src_size"] = int(st.st_size)
            nf["src_mtime"] = float(st.st_mtime)
            new_files.append(nf)
        store.autopost_recheck(t["id"], new_files)
        store.add_log(t["id"], "源文件重新判定完成（录制已完成），继续投稿", "ok")


def _scan_rule(rule: Dict[str, Any], now: float,
               pending: List[Dict[str, Any]], alive: set) -> int:
    files = _candidates(rule)
    alive.update(f["path"] for f in files)
    max_bytes = int(store.load_config().get("max_file_mb") or 8192) * 1024 * 1024
    rname = str(rule.get("name") or rule.get("watch_dir") or "")
    unseen: List[Dict[str, Any]] = []
    for f in files:
        if store.autopost_is_seen(f["path"], f["size"], f["mtime"]):
            continue
        if f["too_small"]:
            continue
        if f["size"] > max_bytes:
            _throttled_warn(f["path"], now,
                            f"自动投稿：《{f['name']}》超过单文件上限 "
                            f"{max_bytes // (1024 * 1024)} MB，已跳过（设置里可调）")
            pending.append({"rule": rname, "name": f["name"], "size": f["size"],
                            "reason": "超过单文件上限"})
            continue
        f["stable"] = _stable(f, rule, now)
        unseen.append(f)
        if not f["stable"]:
            pending.append({"rule": rname, "name": f["name"], "size": f["size"],
                            "reason": "仍在录制/静置中"})

    created = 0
    mode = str(rule.get("mode") or "single").lower()
    if mode == "multi":
        groups: Dict[str, List[Dict[str, Any]]] = {}
        meta: Dict[str, Tuple[str, str]] = {}
        for f in unseen:
            key, streamer, date = _group_of(f)
            groups.setdefault(key, []).append(f)
            meta[key] = (streamer, date)
        idle = max(60, int(rule.get("group_idle_secs") or 600))
        gap = max(600, int(rule.get("session_gap_secs") or 7200))
        for key, members in groups.items():
            # v113：同一天按相邻文件时间差切场次，下午场+晚场不硬合并
            for session in _sessions(members, gap):
                # 组里只要还有文件没静置完，整组继续等（断播分段要等齐）
                if not all(f["stable"] for f in session):
                    continue
                newest = max(f["mtime"] for f in session)
                if now - newest < idle:
                    pending.append({"rule": rname,
                                    "name": f"{meta[key][0]} {meta[key][1]}（{len(session)} 个文件）",
                                    "size": sum(f["size"] for f in session),
                                    "reason": "分P合并等待中"})
                    continue
                created += _ingest_group(session, rule, meta[key][0], meta[key][1],
                                         f"{rule.get('id')}:{key}", now)
    else:
        for f in unseen:
            if not f["stable"]:
                continue
            key, streamer, date = _group_of(f)
            created += _ingest_group([f], rule, streamer, date,
                                     f"{rule.get('id')}:single:{f['path']}", now)
    return created


# ---------------------------------------------------------------- 入队

def _render_title(tpl: str, filename: str, streamer: str, date: str) -> str:
    out = (tpl.replace("{文件名}", filename)
              .replace("{主播}", streamer)
              .replace("{日期}", date)).strip()
    return out[:80] or f"录播 {date}"


def _resolve_season(p: Dict[str, Any], account_id: str) -> int:
    """模板合集属于某个账号：ID 命中就用；否则按合集名在目标账号找同名；
    都没有就放弃挂载（留日志），不能拿别号的合集 ID 去投。"""
    sid = int(p.get("season_id") or 0)
    if not sid:
        return 0
    try:
        from . import season
        lst = season.list_seasons(store.account_cfg(account_id or None))
        ids = {int(s["id"]) for s in lst}
        if sid in ids:
            return sid
        want = str(p.get("season_title") or "").strip()
        if want:
            for s in lst:
                if str(s.get("title") or "").strip() == want:
                    return int(s["id"])
        store.add_global_log(f"自动投稿：模板合集 {sid} 不属于目标账号且找不到同名合集，本稿不挂合集", "warn")
        return 0
    except Exception:  # noqa: BLE001  网络抖动不拦投稿，保留原 ID 尽力而为
        return sid


def _ingest_group(members: List[Dict[str, Any]], rule: Dict[str, Any],
                  streamer: str, date: str, group_key: str, now: float) -> int:
    """一组文件入队。超过 max_parts 自动拆成多个稿件。返回入队稿件数。"""
    members = sorted(members, key=lambda f: (f["mtime"], f["name"]))
    # probe 门槛：任一文件容器不完整就整组继续等
    infos: Dict[str, Dict[str, Any]] = {}
    for f in members:
        ok, info = _probe_ready(f, now)
        if not ok:
            return 0
        infos[f["path"]] = info

    cfg_all = store.load_config()
    defaults = cfg_all.get("defaults") or {}
    max_parts = max(1, int(cfg_all.get("max_parts") or 20))
    tpl = None
    try:
        tid_want = int(rule.get("template_id") or 0)
        if tid_want:
            tpl = next((t for t in store.list_templates() if int(t["id"]) == tid_want), None)
    except Exception:  # noqa: BLE001
        tpl = None
    p = (tpl or {}).get("payload") or {}
    account_id = str(rule.get("account_id") or cfg_all.get("active_account") or "")
    season_id = _resolve_season(p, account_id)
    tags = [str(t).strip() for t in (p.get("tags") or defaults.get("tags") or []) if str(t).strip()]
    if not tags:
        tags = ["录播"]

    created = 0
    tpl_name = str((tpl or {}).get("name") or "")
    multi = str(rule.get("mode") or "single").lower() == "multi"
    for ci in range(0, len(members), max_parts):
        chunk = members[ci:ci + max_parts]
        if _ingest_chunk(chunk, infos, rule, p, defaults, cfg_all, account_id,
                         season_id, tags, streamer, date, group_key, tpl_name, multi):
            created += 1
    return created


def _ingest_chunk(chunk: List[Dict[str, Any]], infos: Dict[str, Dict[str, Any]],
                  rule: Dict[str, Any], p: Dict[str, Any], defaults: Dict[str, Any],
                  cfg_all: Dict[str, Any], account_id: str, season_id: int,
                  tags: List[str], streamer: str, date: str, group_key: str,
                  tpl_name: str, multi: bool) -> bool:
    task_id = uuid.uuid4().hex[:12]
    files: List[Dict[str, Any]] = []
    entries: List[Dict[str, Any]] = []
    try:
        # 直接引用原件上传（零拷贝）。worker 的清理有路径护栏，不会删监视目录原件；
        # 投成功后 archive_originals 把原件移入「已投稿」子文件夹。
        for f in chunk:
            info = infos.get(f["path"]) or {}
            files.append({"path": f["path"], "name": f["name"],
                          "size": f["size"] or int(info.get("size") or 0),
                          "duration": float(info.get("duration") or 0),
                          # v113：入队签名，worker 上传前复核，变了不硬传
                          "src_size": f["size"], "src_mtime": f["mtime"],
                          "part_title": media.video_title_from_filename(f["name"])})
            entries.append({"path": f["path"], "size": f["size"], "mtime": f["mtime"]})

        if multi:
            base_name = streamer
            tpl_str = str(rule.get("title_tpl") or "").strip() or "{主播} {日期} 录播"
        else:
            base_name = media.video_title_from_filename(chunk[0]["name"])
            tpl_str = str(rule.get("title_tpl") or "").strip() or "{文件名}"
        title = _render_title(tpl_str, base_name, streamer, date)
        seq = store.autopost_group_count(group_key)
        if multi and seq > 0:
            suffix = f"（{seq + 1}）"
            title = title[:80 - len(suffix)].rstrip() + suffix

        meta = json.dumps({"rule_id": rule.get("id") or "",
                           "rule_name": rule.get("name") or "",
                           "watch_dir": rule.get("watch_dir") or ""},
                          ensure_ascii=False)
        store.create_autopost_task({
            "id": task_id,
            "title": title,
            "desc": p.get("desc") or defaults.get("desc") or "",
            "tags": tags,
            "tid": int(p.get("tid") or defaults.get("tid") or 21),
            "copyright": int(p.get("copyright") or defaults.get("copyright") or 1),
            "no_reprint": p.get("no_reprint", defaults.get("no_reprint", False)),
            "dynamic": p.get("dynamic") or defaults.get("dynamic") or "",
            "cover_path": "",            # 无封面时 worker 自动截帧兜底
            "season_id": season_id,
            "topic_id": int(p.get("topic_id") or 0),
            "topic_name": p.get("topic_name") or "",
            "mission_id": int(p.get("mission_id") or 0),
            "no_disturbance": p.get("no_disturbance") or 0,
            "is_only_self": p.get("is_only_self") or 0,
            "schedule_mode": "bili",
            "account_id": account_id,
            "files": files,
            "max_attempts": int(cfg_all.get("max_attempts") or 2),
            "autopost_meta": meta,
        }, entries, group_key)
        store.add_log(task_id, f"自动投稿入队：来自规则「{rule.get('name') or rule.get('watch_dir')}」，"
                               f"{len(files)} 个分P（模板：{tpl_name or '未使用'}）")
        store.add_global_log(f"自动投稿已入队《{title}》（{len(files)} 个分P）", "ok")
        with _lock:
            _status["ingested"] = int(_status.get("ingested") or 0) + 1
        try:
            from . import worker
            worker.wake()
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception as e:  # noqa: BLE001  入队失败不留台账，下一轮可重试；原件不受影响
        _status["last_error"] = f"入队失败：{type(e).__name__}: {e}"
        try:
            store.add_global_log(f"自动投稿入队失败（{chunk[0]['name']} 等 {len(chunk)} 个文件）："
                                 f"{type(e).__name__}: {e}", "warn")
        except Exception:
            pass
        return False


# ---------------------------------------------------------------- 投成功归档

def archive_originals(task: Dict[str, Any]) -> int:
    """投稿成功后把原件移入监视目录下「已投稿」子文件夹（保留原子目录结构）。

    由 worker 成功分支回调；失败只记日志，绝不影响投稿结果。返回移动成功的文件数。
    """
    try:
        meta = json.loads(task.get("autopost_meta") or "{}")
    except Exception:  # noqa: BLE001
        return 0
    root = str(meta.get("watch_dir") or "")
    if not root or not os.path.isdir(root):
        return 0
    moved = 0
    files = list(task.get("files") or [])
    for f in files:
        src = str(f.get("path") or "")
        if not src or not os.path.isfile(src):
            continue
        # 只移动监视目录内的原件；「已投稿」里的不再动
        try:
            rel = os.path.relpath(src, root)
        except ValueError:
            continue
        if rel == ".." or rel.startswith(".." + os.sep) or rel.split(os.sep)[0] == ARCHIVE_DIR:
            continue
        dest_dir = os.path.join(root, ARCHIVE_DIR, os.path.dirname(rel))
        try:
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(src))
            if os.path.exists(dest):
                # 同一秒可能连续归档同名文件；逐个探测空位，绝不能覆盖历史录播。
                stem, ext = os.path.splitext(os.path.basename(src))
                prefix = os.path.join(dest_dir, f"{stem}_{int(time.time())}")
                dest = prefix + ext
                serial = 2
                while os.path.exists(dest):
                    dest = f"{prefix}_{serial}{ext}"
                    serial += 1
            shutil.move(src, dest)
        except OSError as e:
            store.add_log(task["id"], f"原件归档失败（留在原地）：{e}", "warn")
            continue
        f["path"] = dest
        moved += 1
        # 每移动一个就立即落库；后续文件失败也不会让已归档文件在任务里指向旧路径。
        store.update_task(task["id"], files=files)
    if moved:
        store.add_log(task["id"], f"投稿成功，{moved} 个原件已移入「{ARCHIVE_DIR}」子文件夹", "ok")
    return moved