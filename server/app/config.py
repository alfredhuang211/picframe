"""相册的配置模型与持久化。

配置存成单个 JSON 文件（默认 /data/settings.json），写入用「临时文件 + os.replace」
保证原子性，避免掉电时把配置写坏。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Transition = Literal["fade", "none", "slide", "zoom"]
Order = Literal["shuffle", "shuffle-daily", "name", "date", "date-desc"]
Fit = Literal["contain", "cover"]
CaptionMode = Literal["none", "date", "name", "date-name"]

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class Schedule(BaseModel):
    """夜间息屏窗口。跨零点（如 23:30 -> 07:00）是支持的。"""

    enabled: bool = False
    off: str = "23:30"
    on: str = "07:00"

    @field_validator("off", "on")
    @classmethod
    def _validate_hhmm(cls, v: str) -> str:
        if not _HHMM.match(v or ""):
            raise ValueError("时间格式必须是 HH:MM（24 小时制）")
        return v


class Settings(BaseModel):
    """所有面向用户的相册配置。

    `sources` / `include_files` / `exclude_files` 里存的是**容器内的绝对路径**，
    由 API 层负责校验它们确实位于允许的照片根目录之下。
    """

    # ---- 选片 ----
    sources: list[str] = Field(default_factory=list, description="选中的文件夹（含子目录）")
    include_files: list[str] = Field(default_factory=list, description="单独挑中的照片")
    exclude_files: list[str] = Field(default_factory=list, description="单独排除的照片（优先级最高）")
    recursive: bool = True

    # ---- 播放 ----
    interval_seconds: float = Field(default=10.0, ge=1.0, le=3600.0)
    transition: Transition = "fade"
    transition_ms: int = Field(default=1200, ge=0, le=10000)
    order: Order = "shuffle"

    fit: Fit = "contain"
    matte: bool = Field(default=True, description="contain 模式下用模糊放大的同一张图做背景")

    ken_burns: bool = True
    ken_burns_amount: float = Field(default=1.08, ge=1.0, le=1.5)

    # ---- 叠加信息 ----
    show_clock: bool = True
    clock_24h: bool = True
    show_caption: bool = True
    caption_mode: CaptionMode = "date"
    background: str = "#000000"

    # ---- 画质 / 性能 ----
    max_long_edge: int = Field(default=2560, ge=640, le=8192, description="幻灯片取图的最长边")
    use_original: bool = Field(default=False, description="勾选后幻灯片直接取原图（吃带宽）")
    preload_count: int = Field(default=3, ge=1, le=20)

    # ---- 视频 ----
    video_enabled: bool = True
    videos_max_seconds: int = Field(default=60, ge=1, le=3600)

    # ---- 运行 ----
    schedule: Schedule = Field(default_factory=Schedule)
    muted: bool = True
    reload_playlist_minutes: int = Field(default=30, ge=0, le=1440)

    @field_validator("background")
    @classmethod
    def _validate_color(cls, v: str) -> str:
        v = (v or "").strip()
        if not re.match(r"^#[0-9a-fA-F]{6}$", v):
            raise ValueError("背景色必须是 #RRGGBB 形式")
        return v.lower()

    @field_validator("sources", "include_files", "exclude_files")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in v or []:
            item = (item or "").strip()
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out


class SettingsStore:
    """线程安全的设置读写。每次 save 会把 revision 加一。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._settings = Settings()
        self._revision = 0

    # ---- 读 ----
    def get(self) -> Settings:
        with self._lock:
            return self._settings.model_copy(deep=True)

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    # ---- 写 ----
    def replace(self, settings: Settings) -> int:
        with self._lock:
            revision = self._revision + 1
            # 先落盘、再改内存。反过来做的话，写盘失败（磁盘满、挂载变只读）会留下
            # 「内存里已经是新配置、磁盘上还是旧的、SSE 也没发」的不一致状态：
            # 管理界面显示新值、显示端继续用旧值、重启后改动全丢。
            self._persist(settings, revision)
            self._settings = settings
            self._revision = revision
            return revision

    def load(self) -> Settings:
        """从磁盘读取。文件不存在或损坏时退回默认值（不抛异常，保证服务能起来）。"""
        with self._lock:
            if not self.path.exists():
                self._settings = Settings()
                self._revision = 0
                return self.get()
            try:
                raw = json.loads(self.path.read_text("utf-8"))
                self._settings = Settings.model_validate(raw.get("settings", raw))
                self._revision = int(raw.get("revision", 0)) if isinstance(raw, dict) else 0
            except Exception:  # noqa: BLE001 - 配置坏了也要能启动
                self._settings = Settings()
                self._revision = 0
            return self.get()

    def _persist(self, settings: Settings, revision: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"revision": revision, "settings": settings.model_dump(mode="json")}
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
