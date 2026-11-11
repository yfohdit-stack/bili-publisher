"""视频探测与封面生成（依赖 ffmpeg / ffprobe）。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

VIDEO_EXTS = {".mp4", ".flv", ".mkv", ".mov", ".avi", ".webm", ".ts", ".m4v", ".wmv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def probe(path: str) -> Dict[str, Any]:
    """读取视频时长、分辨率、大小。失败时给出 0 值而不是抛异常。"""
    info: Dict[str, Any] = {
        "path": path,
        "exists": os.path.isfile(path),
        "size": os.path.getsize(path) if os.path.isfile(path) else 0,
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "error": "",
    }
    if not info["exists"]:
        info["error"] = "文件不存在"
        return info
    if not shutil.which("ffprobe"):
        info["error"] = "未安装 ffprobe，无法读取时长（不影响上传）"
        return info
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout or "{}")
        streams = data.get("streams") or [{}]
        fmt = data.get("format") or {}
        info["width"] = int(streams[0].get("width") or 0)
        info["height"] = int(streams[0].get("height") or 0)
        info["duration"] = round(float(fmt.get("duration") or 0), 2)
    except Exception as e:  # 探测失败不影响投稿
        info["error"] = f"探测失败: {e}"
    return info


def probe_many(paths: List[str], max_workers: int = 4) -> List[Dict[str, Any]]:
    """并行探测多个视频的时长/分辨率。

    一次拖入多个视频时，原来逐个起 ffprobe 子进程串行等待；这些调用互不
    依赖，并行后总耗时接近最慢的那一个。单文件时不建线程池，避免无谓开销。
    """
    if not paths:
        return []
    if len(paths) == 1:
        return [probe(paths[0])]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(paths))) as pool:
        return list(pool.map(probe, paths))


def extract_cover(video_path: str, out_path: str, at_ratio: float = 0.15,
                  duration: Optional[float] = None) -> Optional[str]:
    """从视频抽一帧做封面。B站封面建议 16:9，这里缩到 1146x717 以内。

    duration 可由调用方传入（投稿接口已经并行探测过一次）：不传才会再起
    一个 ffprobe 子进程。旧实现每次抽帧都无条件重跑 ffprobe，多分P 时是
    双倍的进程启动开销。

    单个时间点抽帧很容易失败：关键帧稀疏的容器在 15% 处 seek 可能直接报错，
    时间戳异常的视频也一样。旧实现一次失败就返回 None，调用方只能把整条
    投稿判失败。这里改成多档时间点重试，最后一档连 scale 滤镜都去掉，
    尽量把能出图的都试出来；仍然失败则交给 fallback_cover 兜底。
    """
    if not shutil.which("ffmpeg") or not os.path.isfile(video_path):
        return None
    if duration is None:
        duration = probe(video_path).get("duration") or 0
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    ratios = [at_ratio, 0.5, 0.03]
    cands = []
    for r in ratios:
        ts = duration * r if duration else 0.0
        ts = max(0.0, min(ts, max(duration - 0.5, 0.0)))
        cands.append((ts, True))
    cands.append((0.0, False))       # 片头 + 不加滤镜：兜底避开异常宽高比
    for ts, use_scale in cands:
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{ts:.2f}", "-i", video_path,
               "-frames:v", "1"]
        if use_scale:
            cmd += ["-vf", "scale=1146:-2"]
        cmd += ["-q:v", "3", out_path]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120, check=True)
        except Exception:
            continue
        try:
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                return out_path
        except OSError:
            pass
    return None


def fallback_cover(out_path: str) -> Optional[str]:
    """抽帧彻底失败时的兜底封面：生成一张纯色图，别让封面把投稿卡死。

    封面在B站是可选项，抽不到画面不代表视频不能投。宁可先用占位图把稿子
    发出去（B站后台上传封面即可替换），也不要因为 ffmpeg 抽帧失败把整条
    任务判为失败、白等一个投稿窗口。
    """
    if not shutil.which("ffmpeg"):
        return None
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "color=c=0x1b2430:s=1146x717", "-frames:v", "1", "-q:v", "3", out_path],
            capture_output=True, timeout=60, check=True,
        )
    except Exception:
        return None
    try:
        return out_path if os.path.getsize(out_path) > 0 else None
    except OSError:
        return None


def make_thumb(image_path: str, out_path: str, width: int = 480) -> Optional[str]:
    if not shutil.which("ffmpeg") or not os.path.isfile(image_path):
        return None
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", image_path,
             "-vf", f"scale={width}:-2", "-q:v", "4", out_path],
            capture_output=True, timeout=60, check=True,
        )
    except Exception:
        return None
    return out_path if os.path.isfile(out_path) else None


def normalize_image(image_path: str, out_path: str) -> Optional[str]:
    """把用户传的封面临时图统一转成 png（B站封面接口只收 png/jpg）。"""
    if not shutil.which("ffmpeg") or not os.path.isfile(image_path):
        return image_path if os.path.isfile(image_path) else None
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", image_path, out_path],
            capture_output=True, timeout=60, check=True,
        )
    except Exception:
        return image_path
    return out_path if os.path.isfile(out_path) else image_path


def human_size(n: int) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} TB"


def video_title_from_filename(name: str) -> str:
    base = os.path.splitext(os.path.basename(name))[0]
    return base.replace("_", " ").strip()[:80] or "未命名稿件"
