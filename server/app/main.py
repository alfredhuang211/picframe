"""HTTP 服务：目录浏览、选片、播放列表、缩略图、实时事件。

同一个进程同时服务两拨客户端：

* **管理端**（手机 / 电脑浏览器，``/``）——选文件夹、选照片、调参数。
* **显示端**（HDMI 上的 kiosk 浏览器，``/display``）——只做全屏幻灯片。

两端通过 SSE (``/api/events``) 联动：管理端一保存，显示端立刻重新拉取播放列表。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .config import Settings, SettingsStore
from .images import ImageCache, ImageError, ffmpeg_path, heif_available
from .library import (
    Library,
    PathNotAllowed,
    date_sort_key,
    human_sort_key,
    normalize_roots,
    safe_path,
    version_token,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("picframe")

APP_VERSION = "1.0.0"

# 一次最多下发多少张给显示端。大库靠「定时重新拉取 + 随机」来滚动覆盖。
PLAYLIST_MAX = int(os.environ.get("PLAYLIST_MAX", "3000"))
SETTINGS_MAX_ITEMS = int(os.environ.get("SETTINGS_MAX_ITEMS", "20000"))

MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------- 事件总线
class EventBus:
    """极简 SSE 广播。publish 可以在任意线程调用。"""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: str, data: dict | None = None) -> None:
        loop = self._loop
        if loop is None:
            return
        payload = {"event": event, "data": data or {}}
        for q in list(self._subs):
            # put_nowait 是在**事件循环线程**里执行的，队列满时抛的 QueueFull 发生在
            # 回调内部，外面这层 try 根本抓不到，只会被 loop 的异常处理器打成一段
            # ERROR traceback。所以必须包一层闭包自己接住。
            def _put(queue: asyncio.Queue = q, item: dict = payload) -> None:
                try:
                    queue.put_nowait(item)
                except asyncio.QueueFull:
                    pass  # 订阅者跟不上了就丢这一条，绝不能拖垮发事件的请求
            try:
                loop.call_soon_threadsafe(_put)
            except RuntimeError:
                return  # 事件循环已关闭


bus = EventBus()


# ---------------------------------------------------------------- 应用装配
def create_app() -> FastAPI:
    roots = normalize_roots([os.environ.get("PHOTO_ROOTS", "/photos")])
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    cache_dir = Path(os.environ.get("CACHE_DIR", "/cache"))
    data_dir.mkdir(parents=True, exist_ok=True)

    # 配置目录与缓存目录必须排除在扫描之外。
    # 按 README 推荐的部署方式，照片根目录是整个 /vol1/1000，而缓存默认落在
    # /vol1/1000/docker/picframe/cache —— 就在照片根目录里面。缓存文件是 .jpg，
    # 不排除的话缩略图会被当成照片索引，还会自我增殖（缓存图又生成新的缩略图）。
    library = Library(roots, exclude_dirs=[data_dir, cache_dir])
    store = SettingsStore(data_dir / "settings.json")
    store.load()
    cache = ImageCache(cache_dir / "images")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        bus.bind(asyncio.get_running_loop())
        log.info("照片根目录: %s", ", ".join(str(r) for r in roots) or "(无)")
        if not roots:
            log.error("没有配置任何照片根目录，请在 docker-compose 里设置 PHOTO_ROOTS 与挂载")
        for root in roots:
            if not root.exists():
                log.error("照片根目录不存在（是不是 .env 里的路径写错了？）: %s", root)

        # 首次扫描放到后台任务里，**不能**在这里 await。
        #
        # uvicorn 是「跑完 lifespan startup 才创建监听 socket」的，如果在这里同步等扫描，
        # 大库首扫的几分钟里端口是 connection refused：显示端等不到健康检查，
        # 会启动 Chromium 加载出一个错误页 —— 而错误页上没有任何 JS，重试逻辑压根不会执行，
        # 电视就永久卡在那里了。
        async def initial_scan() -> None:
            try:
                await run_in_threadpool(library.ensure_fresh, force=True)
                log.info("索引完成: %s", library.stats)
            except Exception:  # noqa: BLE001
                log.exception("首次扫描失败")
            finally:
                # 扫描期间显示端拿到的是空列表，靠这个事件才会自动切到真正的照片
                bus.publish("library-changed", {"stats": library.stats})

        scan_task = asyncio.create_task(initial_scan())
        try:
            yield
        finally:
            scan_task.cancel()

    app = FastAPI(
        title="fnos-pic-desktop",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.state.roots = roots
    app.state.library = library
    app.state.store = store
    app.state.cache = cache

    # ------------------------------------------------------------ 页面
    @app.get("/", response_class=HTMLResponse)
    def admin_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "admin.html", media_type="text/html")

    @app.get("/display", response_class=HTMLResponse)
    def display_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "display.html", media_type="text/html")

    # ------------------------------------------------------------ 状态
    @app.get("/api/health")
    def health() -> dict:
        stats = library.stats
        missing = [str(r) for r in roots if not r.exists()]
        return {
            # 路径写错时不能还报 ok：Docker 会自动创建不存在的宿主目录，
            # 于是「拼错的路径」会得到一个健康的空相册，排查时很容易往别处找。
            "ok": bool(roots) and not missing,
            "version": APP_VERSION,
            "roots": [str(r) for r in roots],
            "missing_roots": missing,
            "heif": heif_available(),
            "ffmpeg": bool(ffmpeg_path()),
        }

    @app.get("/api/state")
    def state() -> dict:
        library.ensure_fresh()
        return {
            "version": APP_VERSION,
            "revision": store.revision,
            "settings": store.get().model_dump(mode="json"),
            "stats": library.stats,
            "roots": library.roots_json(),
            "cache": cache.stats(),
            "capabilities": {"heif": heif_available(), "ffmpeg": bool(ffmpeg_path())},
            "playlist_max": PLAYLIST_MAX,
        }

    @app.post("/api/rescan")
    async def rescan() -> dict:
        await run_in_threadpool(library.ensure_fresh, force=True)
        bus.publish("library-changed", {"stats": library.stats})
        return {"ok": True, "stats": library.stats}

    @app.post("/api/cache/purge")
    async def purge_cache() -> dict:
        removed = await run_in_threadpool(cache.purge)
        return {"ok": True, "removed": removed}

    # ------------------------------------------------------------ 目录浏览
    @app.get("/api/browse")
    def browse(path: str | None = None, page: int = 1, page_size: int = 200) -> dict:
        if not roots:
            raise HTTPException(500, "未配置照片根目录")
        target_raw = path or str(roots[0])
        try:
            directory = safe_path(target_raw, roots, expect="dir")
        except PathNotAllowed as exc:
            raise HTTPException(400, str(exc)) from exc
        page = max(1, page)
        page_size = max(1, min(1000, page_size))
        return library.browse(directory, page=page, page_size=page_size)

    # ------------------------------------------------------------ 设置
    @app.get("/api/settings")
    def get_settings() -> dict:
        return {"revision": store.revision, "settings": store.get().model_dump(mode="json")}

    @app.put("/api/settings")
    async def put_settings(payload: dict) -> dict:
        raw = payload.get("settings", payload) if isinstance(payload, dict) else {}
        try:
            incoming = Settings.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - pydantic 的错误信息直接给前端看
            raise HTTPException(400, f"配置校验失败: {exc}") from exc

        # 上限检查必须放在最前面：否则几万条路径会先被逐条 realpath+stat 走一遍，
        # 整个事件循环被卡住，最后才返回 400。
        if _too_many(incoming):
            raise HTTPException(400, f"选中条目过多（上限 {SETTINGS_MAX_ITEMS}）")

        problems = await run_in_threadpool(_validate_selection, incoming, roots)
        if problems:
            raise HTTPException(400, "；".join(problems[:10]))

        revision = await run_in_threadpool(store.replace, incoming)
        bus.publish("settings-changed", {"revision": revision})
        return {"ok": True, "revision": revision}

    # ------------------------------------------------------------ 播放列表
    @app.get("/api/playlist")
    def playlist() -> dict:
        library.ensure_fresh()
        settings = store.get()
        items = library.resolve_selection(
            settings.sources,
            settings.include_files,
            settings.exclude_files,
            recursive=settings.recursive,
        )
        if not settings.video_enabled:
            items = [i for i in items if i.kind == "image"]

        items = _order_items(items, settings)
        total_available = len(items)
        if total_available > PLAYLIST_MAX:
            items = items[:PLAYLIST_MAX]

        payload = []
        for item in items:
            q = quote(item.path, safe="")
            v = version_token(item)
            payload.append(
                {
                    **item.to_json(),
                    # 带上 v：内容一变 URL 就变，派生图的 immutable 长缓存才成立
                    "thumb": f"/api/thumb?path={q}&w=320&v={v}",
                    "url": f"/api/media?path={q}&v={v}",
                }
            )

        return {
            "revision": store.revision,
            "generated_at": time.time(),
            "total": len(payload),
            "total_available": total_available,
            "truncated": total_available > len(payload),
            "settings": settings.model_dump(mode="json"),
            "items": payload,
        }

    # ------------------------------------------------------------ 选片数量预估
    @app.post("/api/count")
    def count(payload: dict) -> dict:
        """按「还没保存的」配置算一下会播放多少项，用于界面上实时反馈。

        刻意写成**同步**函数：FastAPI 会把它丢进线程池，不阻塞事件循环。
        写成 async 的话，大库下一次选择点击就能把整个服务冻住好几秒 ——
        连 SSE 的 keep-alive 都发不出去。
        """
        raw = payload.get("settings", payload) if isinstance(payload, dict) else {}
        try:
            candidate = Settings.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"配置校验失败: {exc}") from exc

        if _too_many(candidate):
            raise HTTPException(400, f"选中条目过多（上限 {SETTINGS_MAX_ITEMS}）")

        items = library.resolve_selection(
            candidate.sources,
            candidate.include_files,
            candidate.exclude_files,
            recursive=candidate.recursive,
        )
        if not candidate.video_enabled:
            items = [i for i in items if i.kind == "image"]
        return {
            "total": len(items),
            "images": sum(1 for i in items if i.kind == "image"),
            "videos": sum(1 for i in items if i.kind == "video"),
            "playable": min(len(items), PLAYLIST_MAX),
        }

    # ------------------------------------------------------------ 取图
    @app.get("/api/thumb")
    async def thumb(
        path: str,
        w: int = Query(default=320, ge=32, le=1600),
        v: str | None = None,  # 内容版本串，只用于让 URL 随内容变化
    ) -> FileResponse:
        return await _serve_derived(library, cache, path, w, variant="thumb", quality=80)

    @app.get("/api/media")
    async def media(
        path: str,
        w: int | None = Query(default=None, ge=64, le=8192),
        v: str | None = None,  # 内容版本串，只用于让 URL 随内容变化
    ) -> FileResponse:
        settings = store.get()
        try:
            src = safe_path(path, roots, expect="media")
        except PathNotAllowed as exc:
            raise HTTPException(400, str(exc)) from exc

        item = library.get_item(str(src))
        kind = item.kind if item else None
        if kind is None:
            from .library import kind_for_ext

            kind = kind_for_ext(src.suffix) or "image"

        # 带了 v 的 URL 内容一变就变，可以放心长缓存；不带 v 的（手工访问）走短缓存
        long_cache = bool(v)

        # 视频一律原样输出，交给浏览器播放
        if kind == "video":
            return _file_response(src, cache_forever=long_cache)

        # HEIC/HEIF 浏览器解不了，即使勾了「用原图」也必须服务端转码，否则直接显示不出来
        if settings.use_original and src.suffix.lower() not in (".heic", ".heif"):
            return _file_response(src, cache_forever=long_cache)

        width = w or settings.max_long_edge
        try:
            dest = await run_in_threadpool(cache.get, src, width, variant="preview", quality=88)
        except ImageError as exc:
            log.warning("预览图生成失败 %s: %s；回退原图", src, exc)
            return _file_response(src, cache_forever=long_cache)
        return _file_response(dest, cache_forever=True)

    # ------------------------------------------------------------ 实时事件
    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        queue = bus.subscribe()

        async def gen():
            try:
                yield _sse("hello", {"revision": store.revision, "version": APP_VERSION})
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield _sse(msg["event"], msg["data"])
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    # ------------------------------------------------------------ 遥控
    @app.post("/api/control")
    async def control(payload: dict) -> dict:
        action = str((payload or {}).get("action", "")).strip()
        allowed = {"next", "prev", "pause", "play", "toggle", "reload", "goto"}
        if action not in allowed:
            raise HTTPException(400, f"不支持的操作: {action!r}；可用: {sorted(allowed)}")
        data = {"action": action}
        if action == "goto":
            try:
                data["index"] = int((payload or {}).get("index", 0))
            except (TypeError, ValueError):
                raise HTTPException(400, "goto 需要整数 index") from None
        bus.publish("control", data)
        return {"ok": True, "action": action}

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


# ---------------------------------------------------------------- 辅助
def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _too_many(settings: Settings) -> bool:
    """选中条目数是否超上限。exclude_files 也要算进去，否则可以用排除列表绕过。"""
    return (
        len(settings.sources) + len(settings.include_files) + len(settings.exclude_files)
        > SETTINGS_MAX_ITEMS
    )


def _validate_selection(settings: Settings, roots: list[Path]) -> list[str]:
    """逐条确认选中的路径真实存在且在允许范围内。

    纯同步、可能很慢（每条一次 realpath + stat），所以调用方必须用 run_in_threadpool，
    不能在事件循环线程上直接跑。
    """
    problems: list[str] = []
    for raw_path in settings.sources:
        try:
            safe_path(raw_path, roots, expect="dir")
        except PathNotAllowed as exc:
            problems.append(f"文件夹 {raw_path}: {exc}")
    for raw_path in list(settings.include_files) + list(settings.exclude_files):
        try:
            safe_path(raw_path, roots, expect="media")
        except PathNotAllowed as exc:
            problems.append(f"照片 {raw_path}: {exc}")
    return problems


def _file_response(path: Path, *, cache_forever: bool = False) -> FileResponse:
    try:
        stat = path.stat()
    except OSError as exc:
        raise HTTPException(404, "文件不存在") from exc
    headers: dict[str, str] = {}
    if cache_forever:
        # 只有「URL 里带了内容版本串（v=）」时才用长缓存，否则会永远显示旧图
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        headers["Cache-Control"] = "private, max-age=60"
    # ETag 只作为代理/调试参考：当前 Starlette 的 FileResponse 只处理 Range 请求，
    # 不评估 If-None-Match / If-Modified-Since，所以它并不会真的产生 304。
    # 客户端缓存的正确性靠 URL 里的 v 参数（见 library.version_token）。
    headers["ETag"] = f'"{int(stat.st_mtime)}-{stat.st_size}"'
    media_type = MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type, headers=headers)


async def _serve_derived(
    library: Library,
    cache: ImageCache,
    raw_path: str,
    width: int,
    *,
    variant: str,
    quality: int,
) -> FileResponse:
    roots = library.roots
    try:
        src = safe_path(raw_path, roots, expect="media")
    except PathNotAllowed as exc:
        raise HTTPException(400, str(exc)) from exc

    item = library.get_item(str(src))
    if item is not None and item.kind == "video":
        try:
            dest = await run_in_threadpool(cache.get, src, width, variant="video")
            return _file_response(dest, cache_forever=True)
        except ImageError:
            # 没有 ffmpeg 或视频损坏：返回 1x1 透明图，前端会显示占位
            raise HTTPException(415, "无法生成视频封面")

    try:
        dest = await run_in_threadpool(cache.get, src, width, variant=variant, quality=quality)
    except ImageError as exc:
        raise HTTPException(415, str(exc)) from exc
    return _file_response(dest, cache_forever=True)


def _order_items(items: list, settings: Settings) -> list:
    order = settings.order
    if order == "shuffle":
        random.shuffle(items)
    elif order == "shuffle-daily":
        # 同一天内顺序稳定，跨天自动换一批
        rng = random.Random(date.today().isoformat())
        rng.shuffle(items)
    elif order == "name":
        items.sort(key=human_sort_key)
    elif order == "date":
        items.sort(key=date_sort_key)
    elif order == "date-desc":
        items.sort(key=date_sort_key, reverse=True)
    return items


app = create_app()
