"""内置官方接口通道 —— 直接对B站投稿接口，唯一上传通道。

底层用 bilibili-api-python 的 video_uploader，它实现了预上传、分块、
封面、稿件提交整条链路，也支持多分P与定时发布。
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any, Callable, Dict, List, Tuple

from .. import quota

COVER_TIMEOUT = 90      # 封面上传卡住时的超时
SUBMIT_TIMEOUT = 90     # 稿件提交卡住时的超时
STALL_TIMEOUT = 180     # 进度停滞多久就放弃本轮（秒）
START_TIMEOUT = 1500    # 整条上传链路的兜底超时（25 分钟）

# B站实际还提供海外上传线路，但 bilibili-api 的 Lines 枚举只写了 bda2/bldsa/qn/ws。
# 这里把海外线路注册成自定义线路字典（os/upcdn/probe_version 三个字段与库内格式一致）。
EXTRA_LINES: Dict[str, Dict[str, Any]] = {
    # 腾讯云 EdgeOne 海外 —— 实测落地新加坡，跨境链路最好
    "txa": {"os": "upos", "upcdn": "txa", "probe_version": 20221109},
    # 阿里云海外 —— 落地香港
    "alia": {"os": "upos", "upcdn": "alia", "probe_version": 20221109},
    # 百度云海外 —— 域名经常解析不到 A 记录，仅作兜底
    "bda": {"os": "upos", "upcdn": "bda", "probe_version": 20221109},
}
_LINE_PATCHED = False


def _patch_lines(vu) -> None:
    """让库接受自定义线路字典，并放宽 endpoint 里的 upcdn 替换规则。"""
    global _LINE_PATCHED
    if _LINE_PATCHED:
        return
    orig_choose = vu._choose_line

    async def choose_line(line):
        if isinstance(line, dict):
            return dict(line)
        return await orig_choose(line)

    vu._choose_line = choose_line

    @staticmethod
    def switch_endpoint(preupload, line=None):
        ep = (preupload or {}).get("endpoint") or ""
        if line is not None and "upcdn" in ep:
            preupload["endpoint"] = re.sub(
                r"upcdn[a-z0-9]+", f'upcdn{line["upcdn"]}', ep, count=1)
        return preupload

    vu.VideoUploader._switch_upload_endpoint = switch_endpoint
    _LINE_PATCHED = True


async def _publish(task: Dict[str, Any], cfg: Dict[str, Any],
                   log: Callable[[str], None],
                   stage: Callable[[str, float], None]) -> Dict[str, Any]:
    from bilibili_api import video_uploader as vu

    cred = quota.credential_from_cfg(cfg)
    files: List[Dict[str, Any]] = task.get("files") or []
    if not files:
        raise ValueError("没有待上传的视频文件")
    for f in files:
        if not os.path.isfile(f["path"]):
            raise FileNotFoundError(f"文件不存在：{f['path']}")

    total_pages = len(files)
    pages = [
        vu.VideoUploaderPage(
            path=f["path"],
            title=(f.get("part_title") or f.get("name") or f"P{i + 1}")[:80],
            description="",
        )
        for i, f in enumerate(files)
    ]

    cover = task.get("cover_path") or ""
    if not (cover and os.path.isfile(cover)):
        raise ValueError("缺少封面文件")

    delay = None
    if task.get("submit_dtime"):
        delay = int(float(task["submit_dtime"]))

    topic_id = int(task.get("topic_id") or 0)
    mission_id = int(task.get("mission_id") or 0)

    # 库的 VideoMeta.__dict__ 把 no_disturbance 写死为 0，用子类覆盖才能让
    # B站网页端「此稿件不生成更新推送」这个开关真正生效（仅加入合集时有意义）
    class _Meta(vu.VideoMeta):
        _no_disturbance = 0
        _is_only_self = 0

        def __dict__(self):
            d = super().__dict__()
            if self._no_disturbance:
                d["no_disturbance"] = 1
            if self._is_only_self:
                d["is_only_self"] = 1
            return d

    meta = _Meta(
        tid=int(task.get("tid") or 21),
        title=task["title"][:80],
        desc=(task.get("desc") or "")[:2000],
        cover=cover,
        tags=[str(t).strip() for t in (task.get("tags") or []) if str(t).strip()],
        original=int(task.get("copyright") or 1) == 1,
        no_reprint=bool(task.get("no_reprint")),
        dynamic=(task.get("dynamic") or None),
        delay_time=delay,
        topic=topic_id or None,
        mission_id=mission_id or None,
    )
    # 勾了就提交 no_disturbance=1：B站只在加入合集时保证该标记生效，
    # 单稿也照样塞进去，是否被采纳由用户自行验证
    meta._no_disturbance = 1 if int(task.get("no_disturbance") or 0) else 0
    if meta._no_disturbance:
        log("已勾选「此稿件不生成更新推送」" + ("（合集内）" if int(task.get("season_id") or 0) else "（单稿，尝试提交该标记）"))
    # 仅自己可见：字段名 is_only_self，库里 VideoMeta 没有这个字段，同样靠覆盖 __dict__ 注入
    meta._is_only_self = 1 if int(task.get("is_only_self") or 0) else 0
    if meta._is_only_self:
        log("已勾选「仅自己可见」，稿件提交后不公开")

    # 库里的分区白名单是下载时缓存的旧数据，缺一级分区（如 游戏 4 / 生活 160），
    # 分区合法性已由 partitions 模块按B站 archive/pre 校验，这里直接放行。
    async def _skip_tid_check(self):
        return True

    vu.VideoMeta._check_tid = _skip_tid_check

    if topic_id:
        # 普通话题不在「分区活动话题」列表里，库的 topic→mission 校验会直接报错；
        # 这里跳过该校验（mission_id 置 0），话题 ID 仍会随稿件一起提交。
        # 直接返回库里取不到的 mission_id（活动 ID），普通话题为 0 只挂话题
        async def _use_topic_mission(self):
            return mission_id

        vu.VideoMeta._check_topic_to_mission = _use_topic_mission
        tail = f"，活动 id {mission_id}" if mission_id else "（仅话题，未参与活动）"
        log(f"已选择话题「{task.get('topic_name') or topic_id}」（id {topic_id}）{tail}")

    # 上传线路：库的自动测速可能选到高丢包节点（跨境链路会卡在固定进度）
    _patch_lines(vu)
    line_name = str(cfg.get("upload_line") or "bda2").strip().lower()
    line = None
    if line_name and line_name != "auto":
        if line_name in EXTRA_LINES:
            line = dict(EXTRA_LINES[line_name])
        else:
            line = getattr(vu.Lines, line_name.upper(), None)
        log(f"上传线路：{line_name}" + ("" if line else "（未识别，回退自动测速）"))
    fb_name = "bda2" if line_name != "bda2" else "txa"
    fb_line = dict(EXTRA_LINES[fb_name]) if fb_name in EXTRA_LINES else getattr(vu.Lines, fb_name.upper())

    uploader = vu.VideoUploader(pages, meta, cred, cover=cover, line=line)

    # 上传进度与实时速度：库按分块回调，用「已传字节 / 总字节」算百分比
    page_sizes: Dict[str, int] = {}
    for pg in pages:
        try:
            page_sizes[pg.path] = os.path.getsize(pg.path)
        except OSError:
            page_sizes[pg.path] = 0
    total_bytes = sum(page_sizes.values()) or 1
    chunks: Dict[str, Dict[str, Any]] = {}
    marks: List[Tuple[float, int]] = []   # (时间, 已传字节)，用于算瞬时速度
    state = {"pages_seen": 0, "done_bytes": 0}

    def _speed_mbps() -> float:
        now = time.time()
        marks.append((now, state["done_bytes"]))
        while len(marks) > 2 and now - marks[0][0] > 8:
            marks.pop(0)
        if len(marks) < 2:
            return 0.0
        dt = marks[-1][0] - marks[0][0]
        if dt <= 0.2:
            return 0.0
        return max(0.0, (marks[-1][1] - marks[0][1]) / dt / 1048576)

    def _progress() -> float:
        frac = min(1.0, state["done_bytes"] / total_bytes)
        return max(0.10, min(0.88, 0.10 + 0.78 * frac))

    def on_preupload(data):
        state["pages_seen"] += 1
        stage(f"上传分P {state['pages_seen']}/{total_pages}", _progress())
        log(f"开始上传分P {state['pages_seen']}/{total_pages}")

    def on_pre_chunk(data):
        key = getattr(data.get("page"), "path", "")
        chunks[key] = {"size": page_sizes.get(key, 0),
                       "total": max(1, int(data.get("total_chunk_count") or 1)),
                       "done": 0}

    def on_after_chunk(data):
        key = getattr(data.get("page"), "path", "")
        st = chunks.setdefault(key, {"size": page_sizes.get(key, 0),
                                     "total": max(1, int(data.get("total_chunk_count") or 1)),
                                     "done": 0})
        st["done"] = max(st["done"], int(data.get("chunk_number") or 0) + 1)
        state["done_bytes"] = int(sum(c["size"] * min(1.0, c["done"] / c["total"])
                                      for c in chunks.values()))
        speed = _speed_mbps()
        frac = min(1.0, state["done_bytes"] / total_bytes)
        stage(f"上传 {frac * 100:.0f}% · {speed:.1f} MB/s", _progress(), speed)

    def on_preupload_failed(data):
        log("预上传失败，可能是文件格式或网络问题")
        stage("预上传失败", 0.1)

    def on_pre_cover(_):
        stage("上传封面", 0.05)

    def on_after_cover(data):
        log(f"封面已上传：{data.get('url', '')}")

    def on_pre_submit(_):
        stage("提交稿件", 0.92)

    def on_after_submit(_):
        stage("稿件已提交，等待审核", 0.98)

    def on_failed(data):
        log(f"上传失败：{data.get('err')}")

    uploader.add_event_listener(vu.VideoUploaderEvents.PREUPLOAD.value, on_preupload)
    handlers = [
        (vu.VideoUploaderEvents.PRE_CHUNK, on_pre_chunk),
        (vu.VideoUploaderEvents.AFTER_CHUNK, on_after_chunk),
        (vu.VideoUploaderEvents.PREUPLOAD_FAILED, on_preupload_failed),
        (vu.VideoUploaderEvents.PRE_COVER, on_pre_cover),
        (vu.VideoUploaderEvents.AFTER_COVER, on_after_cover),
        (vu.VideoUploaderEvents.PRE_SUBMIT, on_pre_submit),
        (vu.VideoUploaderEvents.AFTER_SUBMIT, on_after_submit),
        (vu.VideoUploaderEvents.FAILED, on_failed),
    ]

    def _bind(up):
        for ev, cb in handlers:
            up.add_event_listener(ev.value, cb)
        return up

    _bind(uploader)

    log("校验投稿参数（分区 / 标签 / 封面 / 定时）")
    await meta.verify(cred)

    def _reset_progress():
        # 换线路重传时进度要从头算，否则百分比会直接跳满
        chunks.clear()
        marks.clear()
        state["pages_seen"] = 0
        state["done_bytes"] = 0

    def _wrap_timeouts(up):
        # 封面/提交的底层 HTTP 请求默认没有超时，连接挂死会无限等待，这里强制兜底
        orig_cover = up._upload_cover
        async def cover_with_timeout():
            return await asyncio.wait_for(orig_cover(), COVER_TIMEOUT)
        up._upload_cover = cover_with_timeout

        orig_submit = up._submit
        async def submit_with_timeout(*a, **k):
            return await asyncio.wait_for(orig_submit(*a, **k), SUBMIT_TIMEOUT)
        up._submit = submit_with_timeout

    async def _run_with_stall_watch(up):
        # 分块上传没有超时，遇到坏节点会长时间卡在固定进度，这里按「进度停滞」判定放弃
        run = asyncio.create_task(up.start())
        last_bytes = state["done_bytes"]
        last_change = time.time()
        try:
            while True:
                done, _pending = await asyncio.wait({run}, timeout=10)
                if run in done:
                    return run.result()
                if state["done_bytes"] != last_bytes:
                    last_bytes = state["done_bytes"]
                    last_change = time.time()
                elif time.time() - last_change > STALL_TIMEOUT:
                    run.cancel()
                    raise TimeoutError(f"上传停滞超过 {STALL_TIMEOUT} 秒（卡在 {last_bytes} 字节），放弃本轮")
        except asyncio.CancelledError:
            run.cancel()
            raise

    async def _start(line_obj):
        up = uploader if line_obj is None else _bind(
            vu.VideoUploader(pages, meta, cred, cover=cover, line=line_obj))
        _wrap_timeouts(up)
        return await asyncio.wait_for(_run_with_stall_watch(up), START_TIMEOUT)

    try:
        result = await _start(None)
    except Exception as exc:
        msg = str(exc)
        # 节点证书过期 / 链路停滞：换一条备用线路整条重传一次，避免整条投稿白等
        if ("certificate has expired" in msg or "SSL certificate" in msg
                or isinstance(exc, (asyncio.TimeoutError, TimeoutError))):
            log(f"{line_name} 线路不可用（{msg[:80]}），换 {fb_name} 线路重传")
            _reset_progress()
            result = await _start(fb_line)
        else:
            raise
    bvid = ""
    aid = None
    if isinstance(result, dict):
        # 三元表达式的优先级比 or 低：旧写法在 data 不是 dict 时会把整个表达式
        # 求值为 None，连 result 顶层的 aid/bvid 一起丢掉（合集挂不上、账本对不上）。
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        bvid = result.get("bvid") or data.get("bvid") or ""
        aid = result.get("aid") or data.get("aid")
    return {"bvid": bvid, "aid": aid, "raw": result}


def publish(task: Dict[str, Any], cfg: Dict[str, Any],
            log: Callable[[str], None],
            stage: Callable[[str, float], None]) -> Dict[str, Any]:
    """同步入口：worker 线程调用。"""
    return asyncio.run(_publish(task, cfg, log, stage))


def available() -> bool:
    try:
        import bilibili_api  # noqa: F401
        return True
    except Exception:
        return False
