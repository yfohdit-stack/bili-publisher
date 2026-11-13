"""配置与任务存储。

所有运行数据都放在项目目录下的 data/：
  data/config.json   —— 登录凭证、默认投稿参数
  data/publish.db    —— 任务队列、日志、模板
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "publish.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

_lock = threading.RLock()

# 目录只建一次；按 DATA_DIR 记，测试把数据目录改到临时目录时会自动重建。
_dirs_ready_for: Optional[str] = None

# 配置内存缓存：load_config 是全局最热的函数（几乎每个接口、worker 每次决策
# 都会调），原实现每次都读磁盘 + json 解析 + 账号迁移比对 + 可能的写盘。
# 这里按 (mtime_ns, size) 判新鲜，命中直接返回深拷贝（调用方会改嵌套的 accounts，
# 必须深拷贝，否则会污染缓存）。
_cfg_cache: Dict[str, Any] = {"sig": None, "data": None}

DEFAULT_CONFIG: Dict[str, Any] = {
    "cookie": {"sessdata": "", "bili_jct": "", "buvid3": "", "dedeuserid": ""},
    "account": {"mid": 0, "uname": "", "face": ""},
    # 多账号：accounts 存全部账号（各自 cookie），active_account 为当前生效的 id。
    # 为兼容旧逻辑，cfg["cookie"] / cfg["account"] 始终镜像当前生效账号。
    "accounts": [],
    "active_account": "",
    "legacy_account_backfill": False,
    # v105：移除 biliup 通道，上传固定走内置官方接口
    "channel": "official",
    # 24 小时投稿数上限，超过即拦截
    "quota_limit": 50,
    "quota_hours": 24,
    "max_attempts": 2,
    # 额度数据抓不全时是否保守拦截：True=拦下等人确认，False=只提示、照常投
    "quota_strict": True,
    # 上传前置校验：单文件体积上限（MB）与单稿件分P数量上限
    "max_file_mb": 8192,
    "max_parts": 20,
    "defaults": {
        "tid": 21,
        "copyright": 1,
        "tags": [],
        "desc": "",
        "no_reprint": False,
        "dynamic": "",
    },
    # v103：状态通知（url 留空 = 完全不触发）与 Web 访问鉴权（默认关闭 = 零影响）。
    # 显式写进默认配置，保证全新安装也能在设置里看到并可保存。
    "notify": {"enabled": True, "url": "", "kind": "auto", "chat_id": "",
               "timeout": 6, "events": {"task": True, "rate": True,
                                        "quota": True, "resume": True}},
    "web_auth": {"enabled": False, "mode": "token", "token": "",
                 "username": "", "password": ""},
}


def _merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_dirs() -> None:
    global _dirs_ready_for
    if _dirs_ready_for != DATA_DIR:
        os.makedirs(DATA_DIR, exist_ok=True)
        _dirs_ready_for = DATA_DIR


def _config_sig():
    """配置文件指纹；文件不存在返回 None。"""
    try:
        st = os.stat(CONFIG_PATH)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _cache_put(cfg: Dict[str, Any]) -> Dict[str, Any]:
    _cfg_cache.update({"sig": _config_sig(), "data": copy.deepcopy(cfg)})
    return copy.deepcopy(cfg)


def load_config() -> Dict[str, Any]:
    ensure_dirs()
    with _lock:
        sig = _config_sig()
        if sig is not None and _cfg_cache["data"] is not None and _cfg_cache["sig"] == sig:
            return copy.deepcopy(_cfg_cache["data"])
        if sig is None:
            # 首次运行：save_config 会顺带把缓存填上
            return save_config(DEFAULT_CONFIG)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
        cfg = _merge(DEFAULT_CONFIG, raw)
        migrated = _migrate_accounts(cfg)
        if migrated is not cfg:      # 旧配置升级：把单账号登记进 accounts 并落盘
            return save_config(migrated)
        return _cache_put(migrated)


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    ensure_dirs()
    cfg = _merge(DEFAULT_CONFIG, cfg)
    with _lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except Exception:
            pass
        return _cache_put(cfg)


def invalidate_config_cache() -> None:
    """外部直接改了 config.json 时调用，强制下次重新读盘。"""
    with _lock:
        _cfg_cache.update({"sig": None, "data": None})


def patch_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    return save_config(_merge(load_config(), patch))


# ---------------------------------------------------------------- 多账号

COOKIE_KEYS = ("sessdata", "bili_jct", "buvid3", "dedeuserid")


def _acc_id(mid: int = 0) -> str:
    return f"acc_{mid}" if mid else "acc_" + uuid.uuid4().hex[:8]


def _clean_cookie(cookie: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    c = cookie or {}
    return {k: (c.get(k) or "") for k in COOKIE_KEYS}


def _migrate_accounts(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """账号体系自愈：把顶层 cookie 与 accounts 对齐。

    - 老配置只有单份 cookie → 登记成账号；
    - 顶层 cookie 属于一个新号（扫码/粘贴登录刚写入）→ 追加为新账号并设为当前；
    - 顶层 cookie 是当前账号刷新后的值 → 更新该账号的 cookie。
    返回新对象表示与入参不同（需要落盘）。
    """
    accounts = [dict(a) for a in (cfg.get("accounts") or [])]
    top = _clean_cookie(cfg.get("cookie"))
    new_active = ""

    if not accounts and top.get("sessdata"):
        acc = cfg.get("account") or {}
        mid = int(acc.get("mid") or 0) or int(top.get("dedeuserid") or 0)
        accounts = [{
            "id": _acc_id(mid), "mid": mid,
            "uname": acc.get("uname") or "", "face": acc.get("face") or "",
            "cookie": top, "added_at": time.time(),
        }]
        new_active = accounts[0]["id"]
    elif top.get("sessdata"):
        mid = int(top.get("dedeuserid") or 0)
        idx = None
        for i, a in enumerate(accounts):
            if mid and int(a.get("mid") or 0) == mid:
                idx = i
                break
            if not mid and _clean_cookie(a.get("cookie")).get("dedeuserid") == top.get("dedeuserid"):
                idx = i
                break
        if idx is None:
            # 新账号：dedeuserid 就是 mid，先登记，uname/face 等校验后再补
            accounts.append({"id": _acc_id(mid), "mid": mid, "uname": "", "face": "",
                             "cookie": top, "added_at": time.time()})
            new_active = accounts[-1]["id"]
        else:
            a = accounts[idx]
            if _clean_cookie(a.get("cookie")) != top:
                a = dict(a)
                a["cookie"] = top
                if mid:
                    a["mid"] = mid
                accounts[idx] = a

    if not accounts:
        active = "" if cfg.get("active_account") else None
        nxt = dict(cfg, active_account="", accounts=[])
        return nxt if active is not None and active != cfg.get("active_account") else cfg

    ids = {a.get("id") for a in accounts}
    active = new_active or cfg.get("active_account") or ""
    if active not in ids:
        active = sorted(accounts, key=lambda a: a.get("added_at") or 0)[0]["id"]

    act = next(a for a in accounts if a["id"] == active)
    nxt = dict(cfg)
    nxt["accounts"] = accounts
    nxt["active_account"] = active
    nxt["cookie"] = _clean_cookie(act.get("cookie"))
    nxt["account"] = {"mid": act.get("mid") or 0, "uname": act.get("uname") or "",
                      "face": act.get("face") or ""}

    before = (json.dumps(cfg.get("accounts") or [], sort_keys=True, ensure_ascii=False),
              cfg.get("active_account"), cfg.get("cookie"))
    after = (json.dumps(accounts, sort_keys=True, ensure_ascii=False),
             nxt["active_account"], nxt["cookie"])
    return nxt if before != after else cfg


def list_accounts() -> List[Dict[str, Any]]:
    cfg = load_config()
    active = cfg.get("active_account") or ""
    out = []
    for a in cfg.get("accounts") or []:
        out.append({
            "id": a.get("id"), "mid": a.get("mid") or 0,
            "uname": a.get("uname") or "", "face": a.get("face") or "",
            "added_at": a.get("added_at") or 0,
            "active": a.get("id") == active,
            "logged_in": bool((a.get("cookie") or {}).get("sessdata")),
        })
    return out


def active_account_id() -> str:
    return load_config().get("active_account") or ""


def account_cfg(account_id: Optional[str]) -> Dict[str, Any]:
    """按账号 id 取一份配置副本（cookie 换成该账号）。id 为空/找不到时用当前生效账号。"""
    cfg = load_config()
    if not account_id:
        return cfg
    for a in cfg.get("accounts") or []:
        if a.get("id") == account_id:
            out = dict(cfg)
            out["cookie"] = _clean_cookie(a.get("cookie"))
            out["account"] = {"mid": a.get("mid") or 0, "uname": a.get("uname") or "",
                              "face": a.get("face") or ""}
            return out
    return cfg


def set_active_account(account_id: str) -> Dict[str, Any]:
    cfg = load_config()
    target = None
    for a in cfg.get("accounts") or []:
        if a.get("id") == account_id:
            target = a
            break
    if target is None:
        raise KeyError(f"账号 {account_id} 不存在")
    # 直接同步换好顶层 cookie/account 并落盘，不能依赖读盘时的 _migrate_accounts
    # （load_config 命中内存缓存时不会跑迁移）
    patch = {
        "active_account": account_id,
        "cookie": _clean_cookie(target.get("cookie")),
        "account": {"mid": target.get("mid") or 0,
                    "uname": target.get("uname") or "",
                    "face": target.get("face") or ""},
    }
    return save_config(_merge(cfg, patch))


def upsert_account(cookie: Dict[str, Any], info: Optional[Dict[str, Any]] = None) -> str:
    """新增或更新账号（按 mid 去重），并设为当前生效账号。返回账号 id。"""
    cfg = load_config()
    info = info or {}
    mid = int(info.get("mid") or 0)
    cookie = _clean_cookie(cookie)
    accounts = list(cfg.get("accounts") or [])
    target = None
    for a in accounts:
        if mid and int(a.get("mid") or 0) == mid:
            target = a
            break
        if not mid and (a.get("cookie") or {}).get("dedeuserid") and                 (a.get("cookie") or {}).get("dedeuserid") == cookie.get("dedeuserid"):
            target = a
            break
    if target is None:
        target = {"id": _acc_id(mid), "mid": mid, "added_at": time.time()}
        accounts.append(target)
    target["cookie"] = cookie
    if mid:
        target["mid"] = mid
    if info.get("uname"):
        target["uname"] = info.get("uname")
    if info.get("face") is not None:
        target["face"] = info.get("face") or ""
    save_config(dict(cfg, accounts=accounts, active_account=target["id"]))
    return target["id"]


def remove_account(account_id: str) -> Dict[str, Any]:
    cfg = load_config()
    accounts = [a for a in cfg.get("accounts") or [] if a.get("id") != account_id]
    if len(accounts) == len(cfg.get("accounts") or []):
        raise KeyError(f"账号 {account_id} 不存在")
    active = cfg.get("active_account")
    nxt = dict(cfg, accounts=accounts)
    nxt["active_account"] = active
    if active == account_id:
        nxt["active_account"] = accounts[0]["id"] if accounts else ""
    save_config(nxt)
    return load_config()


def cookie_string(cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg or load_config()
    c = cfg.get("cookie") or {}
    parts = []
    if c.get("sessdata"):
        parts.append(f"SESSDATA={c['sessdata']}")
    if c.get("bili_jct"):
        parts.append(f"bili_jct={c['bili_jct']}")
    if c.get("buvid3"):
        parts.append(f"buvid3={c['buvid3']}")
    if c.get("dedeuserid"):
        parts.append(f"DedeUserID={c['dedeuserid']}")
    return "; ".join(parts)


# ---------------------------------------------------------------- 数据库

_conn: Optional[sqlite3.Connection] = None


def connect() -> sqlite3.Connection:
    """全局共享的持久连接。

    所有调用方都在 _lock 内使用连接，配合 check_same_thread=False 保证线程安全；
    建连与 PRAGMA 切换（单次约 30ms）只在首次执行一次，不再每次读写都重连。
    """
    global _conn
    ensure_dirs()
    if _conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL：worker 高频写进度时不阻塞前端读；NORMAL 在速度和断电安全之间取平衡
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass  # 某些文件系统不支持 WAL，退回默认日志模式即可
        _conn = conn
    return _conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    finished_at  REAL,
    status       TEXT NOT NULL,          -- queued/uploading/waiting_schedule/success/failed/blocked/cancelled/interrupted
    stage        TEXT DEFAULT '',
    progress     REAL DEFAULT 0,
    title        TEXT NOT NULL,
    desc         TEXT DEFAULT '',
    tags         TEXT DEFAULT '[]',
    tid          INTEGER DEFAULT 21,
    copyright    INTEGER DEFAULT 1,
    no_reprint   INTEGER DEFAULT 0,
    dynamic      TEXT DEFAULT '',
    cover_path   TEXT DEFAULT '',
    publish_at   REAL,
    channel      TEXT DEFAULT 'auto',
    season_id    INTEGER DEFAULT 0,
    topic_id     INTEGER DEFAULT 0,
    topic_name   TEXT DEFAULT '',
    mission_id   INTEGER DEFAULT 0,
    no_disturbance INTEGER DEFAULT 0,
    is_only_self INTEGER DEFAULT 0,
    speed        REAL DEFAULT 0,
    cleanup      TEXT DEFAULT '',
    schedule_mode TEXT DEFAULT 'bili',
    account_id   TEXT DEFAULT '',
    files        TEXT DEFAULT '[]',
    bvid         TEXT DEFAULT '',
    aid          INTEGER,
    attempts     INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 2,
    error        TEXT DEFAULT '',
    message      TEXT DEFAULT '',
    quota_info   TEXT DEFAULT '',
    cover_rev    INTEGER DEFAULT 0     -- 换封面时才 +1，供前端做稳定的图片缓存键
);
CREATE TABLE IF NOT EXISTS logs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  TEXT,
    ts       REAL NOT NULL,
    level    TEXT DEFAULT 'info',
    msg      TEXT
);
CREATE TABLE IF NOT EXISTS templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_logs_task ON logs(task_id);
"""


def init_db() -> None:
    ensure_dirs()
    with _lock:
        conn = connect()
        conn.executescript(SCHEMA)
        # 老库补字段：合集是后加的功能，不能要求用户重建数据库
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "season_id" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN season_id INTEGER DEFAULT 0")
        if "topic_id" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN topic_id INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE tasks ADD COLUMN topic_name TEXT DEFAULT ''")
        if "mission_id" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN mission_id INTEGER DEFAULT 0")
        if "no_disturbance" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN no_disturbance INTEGER DEFAULT 0")
        if "is_only_self" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN is_only_self INTEGER DEFAULT 0")
        if "speed" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN speed REAL DEFAULT 0")
        if "cleanup" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN cleanup TEXT DEFAULT ''")
        if "account_id" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN account_id TEXT DEFAULT ''")
        if "schedule_mode" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN schedule_mode TEXT DEFAULT 'bili'")
        if "cover_rev" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN cover_rev INTEGER DEFAULT 0")
        conn.commit()
        _backfill_legacy_accounts(conn)


def _backfill_legacy_accounts(conn: sqlite3.Connection) -> None:
    """多账号上线前的任务没有 account_id，按最早添加的账号补上（那时只有这一个号）。"""
    try:
        cfg = load_config()
        if cfg.get("legacy_account_backfill"):
            return
        accounts = cfg.get("accounts") or []
        if not accounts:
            return
        oldest = sorted(accounts, key=lambda a: a.get("added_at") or 0)[0]
        cur = conn.execute("UPDATE tasks SET account_id = ? WHERE account_id IS NULL OR account_id = ''",
                           (oldest.get("id") or "",))
        conn.commit()
        if cur.rowcount:
            print(f"[accounts] 已为 {cur.rowcount} 条历史任务补记账号：{oldest.get('uname') or oldest.get('id')}")
        save_config(dict(cfg, legacy_account_backfill=True))
    except Exception as e:
        print("[accounts] 历史任务回填失败：", e)


TASK_FIELDS = (
    "id", "created_at", "updated_at", "finished_at", "status", "stage", "progress",
    "title", "desc", "tags", "tid", "copyright", "no_reprint", "dynamic",
    "cover_path", "publish_at", "channel", "season_id", "topic_id", "topic_name", "mission_id", "no_disturbance",
    "is_only_self", "speed", "cleanup", "schedule_mode", "account_id",
    "files", "bvid", "aid",
    "attempts", "max_attempts", "error", "message", "quota_info", "cover_rev",
)


def row_to_task(row: sqlite3.Row) -> Dict[str, Any]:
    d = {k: row[k] for k in row.keys()}
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except Exception:
        d["tags"] = []
    try:
        d["files"] = json.loads(d.get("files") or "[]")
    except Exception:
        d["files"] = []
    return d


def create_task(payload: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    tid = payload.get("id") or uuid.uuid4().hex[:12]
    task = {
        "id": tid,
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
        "status": payload.get("status", "queued"),
        "stage": "",
        "progress": 0.0,
        "title": (payload.get("title") or "").strip() or "未命名稿件",
        "desc": payload.get("desc") or "",
        "tags": payload.get("tags") or [],
        "tid": int(payload.get("tid") or 21),
        "copyright": int(payload.get("copyright") or 1),
        "no_reprint": 1 if payload.get("no_reprint") else 0,
        "dynamic": payload.get("dynamic") or "",
        "cover_path": payload.get("cover_path") or "",
        "publish_at": payload.get("publish_at"),
        "channel": payload.get("channel") or "auto",
        "season_id": int(payload.get("season_id") or 0),
        "topic_id": int(payload.get("topic_id") or 0),
        "topic_name": (payload.get("topic_name") or ""),
        "mission_id": int(payload.get("mission_id") or 0),
        "no_disturbance": 1 if payload.get("no_disturbance") else 0,
        "is_only_self": 1 if payload.get("is_only_self") else 0,
        "speed": 0,
        "cleanup": "",
        "schedule_mode": payload.get("schedule_mode") or "bili",
        "account_id": payload.get("account_id") or "",
        "files": payload.get("files") or [],
        "bvid": "",
        "aid": None,
        "attempts": 0,
        "max_attempts": int(payload.get("max_attempts") or 2),
        "error": "",
        "message": "",
        "quota_info": "",
        "cover_rev": 0,
    }
    with _lock:
        conn = connect()
        cols = ",".join(TASK_FIELDS)
        placeholders = ",".join("?" for _ in TASK_FIELDS)
        conn.execute(
            f"INSERT INTO tasks ({cols}) VALUES ({placeholders})",
            [json.dumps(task[k], ensure_ascii=False) if k in ("tags", "files") else task[k]
             for k in TASK_FIELDS],
        )
        conn.commit()
    return task


def update_task(task_id: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    sets = []
    values = []
    for k, v in fields.items():
        if k in ("tags", "files") and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k} = ?")
        values.append(v)
    with _lock:
        conn = connect()
        conn.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id = ?", values + [task_id])
        conn.commit()


def set_message(task_id: str, message: str) -> None:
    """只更新 message：不刷 updated_at，避免倒计时文案每分钟触发 commit 和前端重渲染。"""
    with _lock:
        connect().execute("UPDATE tasks SET message = ? WHERE id = ?", (message, task_id))
        connect().commit()


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = connect()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row_to_task(row) if row else None


def list_tasks(limit: int = 200) -> List[Dict[str, Any]]:
    with _lock:
        conn = connect()
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [row_to_task(r) for r in rows]


def next_pending(skip_accounts: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """取出下一个可以开始处理的任务（排队中优先，其次到点的定时稿）。

    skip_accounts: 处于节流/风控中的账号 id 集合。v75 起这些账号的任务本轮
    跳过，让其它账号的投稿能继续，避免单个账号占满窗口把整条队列卡住。
    老任务 account_id 为空时用 COALESCE 归一成空串，保证能被正确排除。
    """
    now = time.time()
    skip = tuple(str(a) for a in (skip_accounts or ()))
    cond = ""
    args: List[Any] = []
    if skip:
        cond = " AND COALESCE(account_id, '') NOT IN (%s)" % ",".join("?" for _ in skip)
        args = list(skip)
    with _lock:
        conn = connect()
        row = conn.execute(
            "SELECT * FROM tasks WHERE status = 'queued'" + cond +
            " ORDER BY created_at ASC LIMIT 1",
            args,
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM tasks WHERE status = 'waiting_schedule' AND publish_at <= ?" +
                cond + " ORDER BY publish_at ASC LIMIT 1",
                [now + 600] + args,
            ).fetchone()
    return row_to_task(row) if row else None


def next_queued() -> Optional[Dict[str, Any]]:
    """取最早进入队列的 queued 任务。

    定时稿由 next_pending() 一并挑选（到点前 10 分钟放行）。
    """
    with _lock:
        conn = connect()
        row = conn.execute(
            "SELECT * FROM tasks WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    return row_to_task(row) if row else None


def list_waiting_schedule() -> List[Dict[str, Any]]:
    with _lock:
        conn = connect()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = 'waiting_schedule' ORDER BY publish_at ASC"
        ).fetchall()
    return [row_to_task(r) for r in rows]


def list_cleanup_pending(limit: int = 200) -> List[Dict[str, Any]]:
    """「通过审核后删除」待回读的任务，替代原来的全表 list_tasks()。"""
    with _lock:
        conn = connect()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE cleanup='pending' AND status='success' LIMIT ?",
            (limit,),
        ).fetchall()
    return [row_to_task(r) for r in rows]


def list_by_status(status: str, limit: int = 500) -> List[Dict[str, Any]]:
    """按状态取任务，最久没更新的排在前面（启动恢复、卡死看门狗用）。"""
    with _lock:
        conn = connect()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at ASC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [row_to_task(r) for r in rows]


def add_log(task_id: Optional[str], msg: str, level: str = "info") -> None:
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO logs (task_id, ts, level, msg) VALUES (?,?,?,?)",
            (task_id, time.time(), level, msg[:2000]),
        )
        conn.commit()


def get_logs(task_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    with _lock:
        conn = connect()
        if task_id:
            rows = conn.execute(
                "SELECT * FROM logs WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM logs WHERE task_id IS NULL ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in reversed(rows)]


def add_global_log(msg: str, level: str = "info") -> None:
    """系统级操作日志（不绑定具体任务）。"""
    add_log(None, msg, level)


def clear_logs() -> None:
    """清空全部日志（含各任务日志）。"""
    with _lock:
        conn = connect()
        conn.execute("DELETE FROM logs")
        conn.commit()


def get_all_logs(limit: int = 200) -> List[Dict[str, Any]]:
    """全局日志：只取系统动作与稿件最终结果，过程日志留在各自任务里。"""
    with _lock:
        conn = connect()
        rows = conn.execute(
            "SELECT id, task_id, ts, level, msg FROM logs "
            "WHERE task_id IS NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def delete_task(task_id: str) -> None:
    """彻底删除任务记录与日志。"""
    with _lock:
        conn = connect()
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.execute("DELETE FROM logs WHERE task_id = ?", (task_id,))
        conn.commit()


def count_status() -> Dict[str, int]:
    with _lock:
        conn = connect()
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# ------------------------------------------------- 轮询用的轻量指纹 / 辅助查询
# 前端每 2.2 秒拉一次 /api/tasks 与 /api/logs。原实现每次都全表 SELECT *、
# json.loads 每个任务的 tags/files、序列化 200 条再传输。这里用一条聚合 SQL
# 得到指纹，指纹没变就直接复用上一次序列化好的响应体，省掉整条构造链路。

def tasks_fingerprint() -> str:
    with _lock:
        row = connect().execute(
            "SELECT COUNT(*) AS n, MAX(updated_at) AS m, MAX(cover_rev) AS r FROM tasks"
        ).fetchone()
    return f"{row['n']}-{int((row['m'] or 0) * 1000)}-{row['r'] or 0}"


def logs_fingerprint() -> str:
    with _lock:
        row = connect().execute(
            "SELECT COUNT(*) AS n, MAX(id) AS m FROM logs WHERE task_id IS NULL"
        ).fetchone()
    return f"{row['n']}-{row['m'] or 0}"


def bump_cover_rev(task_id: str) -> None:
    """换封面时递增版本号；前端用它做图片缓存键，避免用 updated_at
    （上传中进度每秒落库都会刷新 updated_at，会把封面图反复重新下载）。"""
    with _lock:
        conn = connect()
        conn.execute("UPDATE tasks SET cover_rev = COALESCE(cover_rev, 0) + 1 WHERE id = ?", (task_id,))
        conn.commit()


def next_waiting_due(now: float, ahead: float = 600.0) -> Optional[float]:
    """最早一个「即将到点」的定时任务的发布时间；没有返回 None。

    原实现在 worker 里直接 connect().execute 且不加锁，与写操作并发时容易
    撞上 SQLite 锁，这里统一走 _lock。
    """
    with _lock:
        row = connect().execute(
            "SELECT publish_at FROM tasks WHERE status='waiting_schedule' "
            "AND publish_at IS NOT NULL ORDER BY publish_at ASC LIMIT 1"
        ).fetchone()
    if not row or row["publish_at"] is None:
        return None
    return float(row["publish_at"])


# ---------------------------------------------------------------- 模板

def list_templates() -> List[Dict[str, Any]]:
    with _lock:
        conn = connect()
        rows = conn.execute("SELECT * FROM templates ORDER BY updated_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"])
        except Exception:
            d["payload"] = {}
        out.append(d)
    return out


def save_template(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO templates (name, payload, updated_at) VALUES (?,?,?)",
            (name, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM templates ORDER BY id DESC LIMIT 1").fetchone()
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


def update_template(template_id: int, name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """覆盖保存已有模板的名字与参数（模板页「编辑」用）。name 为空时保留原名。"""
    with _lock:
        conn = connect()
        row = conn.execute("SELECT * FROM templates WHERE id = ?", (int(template_id),)).fetchone()
        if row is None:
            raise KeyError(f"模板 {template_id} 不存在")
        keep_name = (name or "").strip() or row["name"]
        conn.execute(
            "UPDATE templates SET name = ?, payload = ?, updated_at = ? WHERE id = ?",
            (keep_name[:40], json.dumps(payload, ensure_ascii=False), time.time(), int(template_id)),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM templates WHERE id = ?", (int(template_id),)).fetchone()
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


def delete_template(template_id: int) -> None:
    with _lock:
        conn = connect()
        conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        conn.commit()
