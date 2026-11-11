"""biliup CLI 通道 —— 复用本机 biliup 的上传链路。

注意 biliup 1.2.x 的登录态不是网页 cookie，而是它自己 `biliup login`
生成的 cookies.json（内含 app access_token，无法从 SESSDATA 伪造）。
所以这里只做两件事：指向那份凭证文件，并把投稿参数按命令行传进去。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List

# probe 每次都会起一个 `biliup --version` 子进程（最长阻塞 20 秒）。
# bootstrap / 保存设置 / 扫码登录都会调它，登录态不会秒变，缓存 5 分钟。
PROBE_TTL = 300.0
_probe_cache: Dict[str, Any] = {"at": 0.0, "sig": None, "data": None}

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PERCENT = re.compile(r"(\d{1,3}(?:\.\d)?)\s*%")
BVID = re.compile(r"(BV[0-9A-Za-z]{10})")

FRIENDLY = {
    "credential validation failed": "biliup 的登录文件无效或已过期，请在终端运行 `biliup login` 重新登录",
    "未知平台": "biliup 登录文件里的 platform 字段不合法，请重新运行 `biliup login` 生成",
    "open cookies file": "找不到 biliup 登录文件，请确认路径或先运行 `biliup login`",
}


def _resolve_cookie_file(cfg: Dict[str, Any]) -> str:
    raw = (cfg.get("biliup_cookie_file") or "cookies.json").strip()
    if os.path.isabs(raw):
        return raw
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidates = [
        os.path.join(os.getcwd(), raw),
        os.path.join(base, raw),
        os.path.join(base, "data", raw),
        os.path.expanduser("~/.config/biliup/cookies.json"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return os.path.join(base, raw)


def _cookie_file_ok(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = (data.get("cookie_info") or {}).get("cookies") or []
        return bool(cookies)
    except Exception:
        return False


def probe(bin_path: str = "biliup", cfg: Dict[str, Any] | None = None,
          force: bool = False) -> Dict[str, Any]:
    cfg = cfg or {}
    cookie_file = _resolve_cookie_file(cfg)
    sig = (bin_path, os.path.abspath(cookie_file))
    now = time.time()
    if (not force and _probe_cache["data"] and _probe_cache["sig"] == sig
            and now - _probe_cache["at"] < PROBE_TTL):
        return dict(_probe_cache["data"])

    path = shutil.which(bin_path) or (bin_path if os.path.isfile(bin_path) else "")
    if not path:
        return {"available": False, "version": "", "path": "", "cookie_file": "",
                "logged_in": False, "error": "未找到 biliup 可执行文件"}
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
        ver = (out.stdout or out.stderr or "").strip().splitlines()
        version = ver[0] if ver else ""
    except Exception as e:
        return {"available": False, "version": "", "path": path, "cookie_file": "",
                "logged_in": False, "error": str(e)}
    logged = _cookie_file_ok(cookie_file)
    result = {
        "available": True, "version": version, "path": path,
        "cookie_file": cookie_file, "logged_in": logged,
        "error": "" if logged else "未找到有效的 biliup 登录文件（需先运行 biliup login）",
    }
    _probe_cache.update({"at": now, "sig": sig, "data": result})
    return result


def publish(task: Dict[str, Any], cfg: Dict[str, Any],
            log: Callable[[str], None],
            stage: Callable[[str, float], None]) -> Dict[str, Any]:
    # 真正上传时不吃缓存：登录文件可能刚被 biliup login 重写
    info = probe(cfg.get("biliup_bin") or "biliup", cfg, force=True)
    if not info["available"]:
        raise RuntimeError(info["error"])
    if not info["logged_in"]:
        raise RuntimeError("biliup 未登录：" + info["error"])

    files: List[Dict[str, Any]] = task.get("files") or []
    if not files:
        raise ValueError("没有待上传的视频文件")
    paths = [f["path"] for f in files]
    for p in paths:
        if not os.path.isfile(p):
            raise FileNotFoundError(f"文件不存在：{p}")
    if not (task.get("cover_path") and os.path.isfile(task["cover_path"])):
        raise ValueError("缺少封面文件")

    args = [
        info["path"], "-u", info["cookie_file"], "upload", *paths,
        "--title", task["title"][:80],
        "--tid", str(int(task.get("tid") or 21)),
        "--desc", (task.get("desc") or "")[:2000],
        "--tag", ",".join(str(t).strip() for t in (task.get("tags") or []) if str(t).strip()),
        "--copyright", str(int(task.get("copyright") or 1)),
        "--no-reprint", "1" if task.get("no_reprint") else "0",
        "--cover", task["cover_path"],
        "--submit", "web",
    ]
    if task.get("dynamic"):
        args += ["--dynamic", task["dynamic"][:233]]
    if task.get("submit_dtime"):
        args += ["--dtime", str(int(float(task["submit_dtime"])))]
    if cfg.get("biliup_line"):
        args += ["-l", cfg["biliup_line"]]

    log("执行 biliup：" + " ".join(a if " " not in a else f'"{a}"' for a in args))
    stage("调用 biliup 上传", 0.03)

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=False, bufsize=0, env=env,
    )
    last_pct = 0.0
    tail: List[str] = []
    try:
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            text = ANSI.sub("", chunk.decode("utf-8", "ignore")).replace("\r", "\n")
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                tail.append(line)
                tail[:] = tail[-40:]
                m = PERCENT.search(line)
                if m:
                    pct = min(99.0, float(m.group(1)))
                    if pct - last_pct >= 1.0:
                        last_pct = pct
                        stage(f"上传中 {pct:.0f}%", 0.05 + pct / 100 * 0.9)
                elif BVID.search(line) or "投稿成功" in line:
                    log(line)
    finally:
        code = proc.wait()

    output = "\n".join(tail)
    bvid_m = BVID.search(output)
    bvid = bvid_m.group(1) if bvid_m else ""
    if code != 0:
        friendly = ""
        for key, msg in FRIENDLY.items():
            if key in output:
                friendly = msg
                break
        raise RuntimeError(friendly or f"biliup 退出码 {code}：{output[-500:]}")
    if not bvid:
        log("biliup 输出中没找到 BV 号，请到B站创作中心确认稿件状态")
    return {"bvid": bvid, "aid": None, "raw": output[-1200:]}


def available() -> bool:
    return probe()["available"]
