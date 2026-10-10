"""图片处理管线：EXIF 方向纠正、HEIC 解码、缩略图 / 预览图磁盘缓存。

为什么要有这一层
----------------
* 浏览器不能显示 HEIC（iPhone 默认格式），必须服务端转码 —— 家庭相册里这类文件很多。
* 直接把 30MB 的原图喂给电视浏览器既慢又浪费；统一缩到「屏幕够用」的尺寸。
* 缩略图网格一次要几百张，必须缓存，否则每次进管理页都要重新解码一遍。

缓存键包含了源文件的 mtime 与 size，所以文件被替换后缓存会自动失效。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger("picframe.images")

# 管线版本：改了下面的处理逻辑就把它加一，旧缓存会自动失效
PIPELINE_VERSION = "2"

# 允许很大的全景图，但仍然防解压炸弹
Image.MAX_IMAGE_PIXELS = 500_000_000

_heif_available = False
try:  # pillow-heif 是可选依赖，装不上也不影响其它格式
    import pillow_heif

    pillow_heif.register_heif_opener()
    _heif_available = True
except Exception:  # noqa: BLE001
    log.warning("pillow-heif 不可用，HEIC/HEIF 照片将无法解码")

# 并发解码上限：NAS 上通常是低功耗 CPU，别一次开太多
_DECODE_SEMAPHORE = threading.Semaphore(max(2, min(8, (os.cpu_count() or 2))))


class ImageError(Exception):
    pass


def heif_available() -> bool:
    return _heif_available


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


class ImageCache:
    """把缩略图/预览图落在磁盘上，按需生成。"""

    def __init__(self, cache_dir: Path, *, quality: int = 85):
        self.cache_dir = Path(cache_dir)
        self.quality = quality
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._stats_cache: tuple[float, dict] | None = None
        self._stats_lock = threading.Lock()

    # ------------------------------------------------------------- 缓存键
    def _key(self, src: Path, width: int, variant: str, stat) -> str:
        """键里同时带上 mtime_ns / ctime_ns / size。

        只用秒级 mtime + size 是不够的：`cp -p`、`rsync -t`、解压归档都会保留 mtime，
        把一张同尺寸的图原地换掉就会命中旧缓存，用户会一直看到错的那张。
        ctime（inode 变更时间）在内容或元数据被改写时一定会变，正好补上这个洞。
        """
        raw = (
            f"{PIPELINE_VERSION}|{variant}|{width}|{src}"
            f"|{stat.st_mtime_ns}|{stat.st_ctime_ns}|{stat.st_size}"
        )
        return hashlib.sha1(raw.encode("utf-8", "surrogateescape")).hexdigest()

    def _dest(self, key: str) -> Path:
        return self.cache_dir / key[:2] / key[2:4] / f"{key}.jpg"

    # ------------------------------------------------------------- 主入口
    def get(self, src: Path, width: int, *, variant: str = "thumb", quality: int | None = None) -> Path:
        """返回缓存文件路径，必要时生成它。"""
        try:
            st = src.stat()
        except OSError as exc:
            raise ImageError(f"无法读取文件: {src}") from exc

        key = self._key(src, width, variant, st)
        dest = self._dest(key)
        if dest.exists() and dest.stat().st_size > 0:
            return dest

        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=str(dest.parent), prefix=".tmp-", suffix=".jpg", delete=False
        ) as fh:
            tmp = Path(fh.name)
        try:
            if variant == "video":
                self._render_video_poster(src, width, tmp)
            else:
                self._render_image(src, width, tmp, quality or self.quality)
            os.replace(tmp, dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return dest

    # ------------------------------------------------------------- 渲染
    def _render_image(self, src: Path, width: int, out: Path, quality: int) -> None:
        with _DECODE_SEMAPHORE:
            try:
                with Image.open(src) as img:
                    # JPEG 快速降采样：让解码器直接按目标尺寸解码，大图能快好几倍
                    try:
                        img.draft("RGB", (width, width))
                    except Exception:  # noqa: BLE001 - 非 JPEG 会忽略 draft
                        pass

                    # 动图取第一帧
                    try:
                        if getattr(img, "n_frames", 1) > 1:
                            img.seek(0)
                    except Exception:  # noqa: BLE001
                        pass

                    img = ImageOps.exif_transpose(img)
                    img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img
                    if img.mode != "RGB":
                        if img.mode in ("RGBA", "LA", "P"):
                            background = Image.new("RGB", img.size, (0, 0, 0))
                            rgba = img.convert("RGBA")
                            background.paste(rgba, mask=rgba.split()[-1])
                            img = background
                        else:
                            img = img.convert("RGB")

                    img.thumbnail((width, width), Image.Resampling.LANCZOS)
                    img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
            except UnidentifiedImageError as exc:
                raise ImageError(f"无法识别的图片格式: {src.name}") from exc
            except Image.DecompressionBombError as exc:
                raise ImageError(f"图片尺寸异常，已拒绝解码: {src.name}") from exc
            except MemoryError as exc:
                raise ImageError(f"图片过大，内存不足: {src.name}") from exc
            except OSError as exc:
                # 截断 / 未拷贝完 / 坏块：NAS 上很常见。
                # 必须接住，否则会变成 HTTP 500，而文档承诺的是「坏文件不影响整体」。
                raise ImageError(f"图片已损坏或读取失败: {src.name}（{exc}）") from exc

    def _render_video_poster(self, src: Path, width: int, out: Path) -> None:
        """用 ffmpeg 抓一帧视频画面当封面。ffmpeg 不存在就报错，由调用方兜底。"""
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            raise ImageError("未安装 ffmpeg，无法生成视频封面")
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-ss", "2", "-i", str(src),
            "-frames:v", "1",
            "-vf", f"scale={width}:-2:force_original_aspect_ratio=decrease",
            "-y", str(out),
        ]
        # 和图片解码共用信号量：否则并发请求一个视频目录会拉起几十个 ffmpeg 进程把 CPU 打满
        with _DECODE_SEMAPHORE:
            proc = self._run_ffmpeg(cmd, src)
            if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                # 有些视频前 2 秒没有关键帧，退回到第 0 秒再试一次
                cmd[cmd.index("-ss") + 1] = "0"
                proc = self._run_ffmpeg(cmd, src)
                if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                    raise ImageError(f"生成视频封面失败: {src.name}")

    @staticmethod
    def _run_ffmpeg(cmd: list[str], src: Path) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired as exc:
            # 重试那次也要接住，否则 TimeoutExpired 会越过 ImageError 变成 HTTP 500
            raise ImageError(f"生成视频封面超时: {src.name}") from exc

    # ------------------------------------------------------------- 维护
    def stats(self) -> dict:
        """缓存体积。带 TTL：全量 os.walk 在大缓存上要好几秒，不能每次进管理页都跑。"""
        now = time.time()
        with self._stats_lock:
            cached = self._stats_cache
        if cached and now - cached[0] < 60:
            return dict(cached[1])

        files = 0
        total = 0
        for dirpath, _dirnames, filenames in os.walk(self.cache_dir):
            for name in filenames:
                if name.startswith(".tmp-"):
                    continue
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                    files += 1
                except OSError:
                    continue
        out = {"files": files, "bytes": total}
        with self._stats_lock:
            self._stats_cache = (now, out)
        return dict(out)

    def purge(self) -> int:
        """清空缓存，返回删除的文件数。"""
        removed = 0
        for dirpath, _dirnames, filenames in os.walk(self.cache_dir):
            for name in filenames:
                try:
                    os.unlink(os.path.join(dirpath, name))
                    removed += 1
                except OSError:
                    continue
        with self._stats_lock:
            self._stats_cache = None
        return removed
