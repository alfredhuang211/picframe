"""照片库：路径安全、目录扫描、媒体索引、选片解析。

安全模型
--------
容器里只读挂载了若干个「照片根目录」（环境变量 PHOTO_ROOTS）。所有来自 HTTP 的
路径都是**不可信输入**，必须满足：

1. 必须是绝对路径且不含 NUL；
2. ``os.path.realpath`` 归一化后必须落在某个根目录之内（realpath 会解开符号链接，
   所以指向根目录之外的软链会被拒绝，而不只是拦住 ``..``）；
3. 做文件操作时还要求目标存在、是普通文件、扩展名在允许范围内。

只检查 ``..`` 是不够的，这里用的是「归一化 + 前缀边界 + realpath」三重判断。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from urllib.parse import quote

log = logging.getLogger("picframe.library")

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".heic", ".heif", ".avif",
}
VIDEO_EXTS = {".mp4", ".m4v", ".webm", ".mov", ".mkv"}

# 扫描时跳过的目录名：缩略图缓存、回收站、系统目录
SKIP_DIR_NAMES = {
    "@eadir", "#recycle", "$recycle.bin", ".thumbnails", ".cache",
    "lost+found", "system volume information", ".git", "node_modules",
}


class PathNotAllowed(Exception):
    """请求的路径不在允许的照片根目录内，或不是合法的媒体文件。"""


@dataclass(frozen=True)
class MediaItem:
    path: str          # 容器内绝对路径（已 realpath 归一化）
    name: str
    ext: str
    kind: str          # "image" | "video"
    size: int
    mtime: float

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "ext": self.ext,
            "kind": self.kind,
            "size": self.size,
            "mtime": self.mtime,
        }


def kind_for_ext(ext: str) -> str | None:
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def normalize_roots(raw: Sequence[str]) -> list[Path]:
    """把 PHOTO_ROOTS 环境变量解析成去重、realpath 归一化后的根目录列表。"""
    roots: list[Path] = []
    seen: set[str] = set()
    for entry in raw:
        for piece in str(entry).split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                real = os.path.realpath(piece)
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            roots.append(Path(real))
    return roots


def is_inside(path: Path, root: Path) -> bool:
    """path 是否等于 root 或位于 root 之下。两边都必须是已归一化的绝对路径。"""
    if path == root:
        return True
    try:
        return path.is_relative_to(root)
    except AttributeError:  # Python < 3.9 兜底
        return str(path).startswith(str(root).rstrip(os.sep) + os.sep)


def safe_path(
    raw: str,
    roots: Sequence[Path],
    *,
    must_exist: bool = True,
    expect: str | None = None,
) -> Path:
    """把不可信输入解析成一个安全的、位于允许根目录内的绝对路径。

    expect: None | "dir" | "file" | "media"
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PathNotAllowed("路径为空")
    if "\x00" in raw:
        raise PathNotAllowed("路径包含非法字符")

    candidate = Path(raw.strip())
    if not candidate.is_absolute():
        raise PathNotAllowed("必须是绝对路径")

    real = Path(os.path.realpath(candidate))

    if not any(is_inside(real, root) for root in roots):
        raise PathNotAllowed(f"路径不在允许的照片目录内: {raw}")

    if must_exist and not real.exists():
        raise PathNotAllowed(f"路径不存在: {raw}")

    if expect == "dir" and must_exist and not real.is_dir():
        raise PathNotAllowed(f"不是目录: {raw}")
    if expect == "file" and must_exist and not real.is_file():
        raise PathNotAllowed(f"不是文件: {raw}")
    # 注意：这里不需要再查 is_symlink —— real 已经过 os.path.realpath，
    # 指向根目录之外的软链在前面就被拒绝了，指向根内合法文件的软链是允许的。

    if expect == "media":
        if not real.is_file():
            raise PathNotAllowed(f"不是文件: {raw}")
        if kind_for_ext(real.suffix) is None:
            raise PathNotAllowed(f"不是支持的媒体类型: {real.suffix}")

    return real


class Library:
    """对照片根目录做一份带 TTL 的内存索引。

    扫描是一次性的目录树遍历（os.scandir），同时预先算好每个目录的**递归**媒体数量，
    这样管理界面浏览目录时不用反复遍历大目录树。
    """

    def __init__(
        self,
        roots: Sequence[Path],
        ttl_seconds: float = 60.0,
        exclude_dirs: Sequence[Path] = (),
    ):
        self.roots = list(roots)
        self.ttl = ttl_seconds
        # 必须排除的目录（配置目录、缩略图缓存目录）。
        #
        # 为什么非要有这个：按 README 推荐的部署方式，照片根目录是整个 /vol1/1000，
        # 而缓存默认落在 /vol1/1000/docker/picframe/cache —— 正好在照片根目录**里面**。
        # 缓存文件是 .jpg，如果不排除，缩略图会被当成照片索引进来，而且会自我增殖：
        # 每张缓存图又会生成新的缩略图。靠目录名黑名单（"cache"）不可靠，
        # 用户完全可能真有一个叫 cache 的相册目录，所以这里按绝对路径排除。
        self.exclude_dirs = [Path(os.path.realpath(p)) for p in exclude_dirs]
        self._lock = threading.RLock()
        self._items: dict[str, MediaItem] = {}
        self._dirs: set[str] = set()
        self._subtree_counts: dict[str, int] = {}
        self._direct_counts: dict[str, int] = {}
        self._scanned_at = 0.0
        self._scanning = False
        self._last_error: str | None = None

    def _is_excluded(self, path: str) -> bool:
        for ex in self.exclude_dirs:
            exs = str(ex)
            if path == exs or path.startswith(exs + os.sep):
                return True
        return False

    # ------------------------------------------------------------------ 扫描
    def _scan(self) -> None:
        items: dict[str, MediaItem] = {}
        dirs: set[str] = set()
        direct: dict[str, int] = {}
        subtree: dict[str, int] = {}
        error: str | None = None

        for root in self.roots:
            if not root.exists():
                log.warning("照片根目录不存在，已跳过: %s", root)
                error = f"照片根目录不存在: {root}"
                continue
            dirs.add(str(root))
            stack: list[Path] = [root]
            while stack:
                current = stack.pop()
                try:
                    with os.scandir(current) as it:
                        for entry in it:
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    if entry.name.lower() in SKIP_DIR_NAMES:
                                        continue
                                    if self._is_excluded(entry.path):
                                        continue
                                    # 不跟随目录软链，避免越界与环路
                                    dirs.add(entry.path)
                                    stack.append(Path(entry.path))
                                    continue
                                if not entry.is_file(follow_symlinks=False):
                                    continue
                                if self._is_excluded(entry.path):
                                    continue
                                kind = kind_for_ext(Path(entry.name).suffix)
                                if kind is None:
                                    continue
                                st = entry.stat(follow_symlinks=False)
                                items[entry.path] = MediaItem(
                                    path=entry.path,
                                    name=entry.name,
                                    ext=Path(entry.name).suffix.lower(),
                                    kind=kind,
                                    size=st.st_size,
                                    mtime=st.st_mtime,
                                )
                                parent = os.path.dirname(entry.path)
                                direct[parent] = direct.get(parent, 0) + 1
                            except OSError:
                                continue  # 单个文件坏了不影响整体
                except OSError as exc:
                    log.warning("无法读取目录 %s: %s", current, exc)
                    continue

        # 把直接计数累加到所有祖先目录，得到递归计数
        for directory, count in direct.items():
            node = directory
            while True:
                subtree[node] = subtree.get(node, 0) + count
                if any(node == str(r) for r in self.roots):
                    break
                parent = os.path.dirname(node)
                if parent == node or not any(is_inside(Path(node), r) for r in self.roots):
                    break
                node = parent

        with self._lock:
            self._items = items
            self._dirs = dirs
            self._direct_counts = direct
            self._subtree_counts = subtree
            self._scanned_at = time.time()
            self._last_error = error
            self._scanning = False

    def ensure_fresh(self, *, force: bool = False) -> None:
        with self._lock:
            fresh = (time.time() - self._scanned_at) < self.ttl and self._scanned_at > 0
            if fresh and not force:
                return
            if self._scanning:
                # 已经有一个线程在扫了，让它扫完就行（force 也一样，避免并发重复扫）
                return
            self._scanning = True
        try:
            self._scan()
        finally:
            # 必须无条件复位：否则任何一次意外异常（MemoryError、扫描中途的怪错误）
            # 都会让 _scanning 永久为 True，此后连 force=True 和 /api/rescan 都失效，
            # 索引永远停在旧快照上，只能重启进程。invalidate() 也救不回来。
            with self._lock:
                self._scanning = False

    def invalidate(self) -> None:
        with self._lock:
            self._scanned_at = 0.0

    # ------------------------------------------------------------------ 查询
    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "roots": [str(r) for r in self.roots],
                "total_media": len(self._items),
                "images": sum(1 for i in self._items.values() if i.kind == "image"),
                "videos": sum(1 for i in self._items.values() if i.kind == "video"),
                "directories": len(self._dirs),
                "scanned_at": self._scanned_at,
                "last_error": self._last_error,
            }

    def get_item(self, path: str) -> MediaItem | None:
        with self._lock:
            return self._items.get(path)

    def browse(self, directory: Path, *, page: int = 1, page_size: int = 200) -> dict:
        """列出某个目录下的子目录与直接子级媒体文件。"""
        self.ensure_fresh()
        with self._lock:
            dirs = [d for d in self._dirs if os.path.dirname(d) == str(directory)]
            dirs.sort(key=lambda p: os.path.basename(p).lower())
            children = [i for i in self._items.values() if os.path.dirname(i.path) == str(directory)]
            children.sort(key=lambda i: i.name.lower())
            sub_info = [
                {
                    "name": os.path.basename(d),
                    "path": d,
                    "media_count": self._subtree_counts.get(d, 0),
                }
                for d in dirs
            ]
            total = len(children)
            start = max(0, (page - 1) * page_size)
            files = [_file_json(i) for i in children[start : start + page_size]]
            direct = self._direct_counts.get(str(directory), 0)

        parent = os.path.dirname(str(directory))
        parent_path = None
        if parent and parent != str(directory) and any(is_inside(Path(parent), r) for r in self.roots):
            parent_path = parent

        with self._lock:
            subtree = self._subtree_counts.get(str(directory), 0)

        return {
            "path": str(directory),
            "name": os.path.basename(str(directory)) or str(directory),
            "parent": parent_path,
            "dirs": sub_info,
            "files": files,
            "direct_count": direct,
            "subtree_count": subtree,
            "total_files": total,
            "page": page,
            "page_size": page_size,
        }

    def roots_json(self) -> list[dict]:
        self.ensure_fresh()
        out = []
        for root in self.roots:
            with self._lock:
                count = self._subtree_counts.get(str(root), 0)
            out.append(
                {
                    "name": root.name or str(root),
                    "path": str(root),
                    "media_count": count,
                    "exists": root.exists(),
                }
            )
        return out

    # ------------------------------------------------------------------ 选片
    def resolve_selection(
        self,
        sources: Iterable[str],
        include_files: Iterable[str],
        exclude_files: Iterable[str],
        *,
        recursive: bool = True,
    ) -> list[MediaItem]:
        """把「选中的文件夹 + 单独挑的照片 - 排除的照片」解析成最终媒体列表。

        无法解析的条目（目录被删、越界）会被静默跳过并记日志——配置过期不应该
        让整个幻灯片崩掉。
        """
        self.ensure_fresh()

        selected: dict[str, MediaItem] = {}

        for raw in sources:
            try:
                directory = safe_path(raw, self.roots, expect="dir")
            except PathNotAllowed as exc:
                log.warning("忽略无效的来源目录 %s: %s", raw, exc)
                continue
            prefix = str(directory)
            sub_prefix = prefix + os.sep
            with self._lock:
                for item in self._items.values():
                    # 这里用字符串前缀而不是 Path.is_relative_to：这是整个后端最热的循环，
                    # 大库下 Path 构造的开销会让管理端每改一次选择就卡好几秒。
                    # 两边都是同一次 scandir 走出来的、已归一化的绝对路径，前缀比较是安全的。
                    if recursive:
                        if item.path.startswith(sub_prefix):
                            selected[item.path] = item
                    else:
                        if os.path.dirname(item.path) == prefix:
                            selected[item.path] = item

        for raw in include_files:
            try:
                path = safe_path(raw, self.roots, expect="media")
            except PathNotAllowed as exc:
                log.warning("忽略无效的单选照片 %s: %s", raw, exc)
                continue
            item = self.get_item(str(path))
            if item is None:
                # 索引可能过期，现场补一个
                try:
                    st = path.stat()
                except OSError:
                    continue
                item = MediaItem(
                    path=str(path),
                    name=path.name,
                    ext=path.suffix.lower(),
                    kind=kind_for_ext(path.suffix) or "image",
                    size=st.st_size,
                    mtime=st.st_mtime,
                )
            selected[item.path] = item

        for raw in exclude_files:
            try:
                path = str(safe_path(raw, self.roots))
            except PathNotAllowed:
                continue
            selected.pop(path, None)

        return list(selected.values())


def human_sort_key(item: MediaItem) -> tuple:
    """自然排序：把连续数字当数字比，这样 2.jpg 排在 10.jpg 前面。

    之前只做了 name.lower()，函数叫 human 但实际是字典序：
    10.jpg 排在 2.jpg 前、img_10.jpg 排在 IMG_9.jpg 前，跟用户预期不符。
    """
    return (
        tuple(
            (1, int(part)) if part.isdigit() else (0, part)
            for part in re.split(r"(\d+)", item.name.lower())
        ),
        item.path,
    )


def date_sort_key(item: MediaItem) -> float:
    return item.mtime


def version_token(item: MediaItem) -> str:
    """拼进取图 URL 的版本串。

    派生图/视频的响应带 `Cache-Control: immutable, max-age=1y`。如果 URL 里只有 path，
    把一张照片原地替换掉（重新导出、修图后覆盖）URL 完全不变，浏览器就会**永远**
    显示旧图，只有手动清缓存才恢复。把 mtime/size 拼进查询串，内容一变 URL 就变，
    长缓存才是成立的。
    """
    return f"{int(item.mtime)}-{item.size}"


def _file_json(item: MediaItem) -> dict:
    """给浏览接口用：附上取图和缩略图的 URL，前端不用自己拼。"""
    q = quote(item.path, safe="")
    v = version_token(item)
    return {
        **item.to_json(),
        "thumb": f"/api/thumb?path={q}&w=320&v={v}",
        "url": f"/api/media?path={q}&v={v}",
    }
