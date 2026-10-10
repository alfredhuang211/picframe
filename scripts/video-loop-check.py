#!/usr/bin/env python3
"""视频「死循环」回归测试。

背景（实测出来的 bug）：
显示端遇到浏览器**播不了**的视频时会立刻跳下一张。如果播放列表里全是这种视频，
就退化成每秒上千次的跳转 —— 实测 3 个坏 .mkv + 1 张图时约 1.3 次/秒，
全是坏视频时达到 **4100 请求/秒**，浏览器主线程被彻底打满，连 CDP 求值都超时。

修复后：坏视频记进 ``S.failed``，1500ms 后才前进；``advance()`` 会跳过已失败的项；
全部失败时停下并显示提示。

这个脚本用**真实浏览器 + 真实服务端**复现该场景，断言请求数在合理范围内。
修复前它会失败（请求数千次），修复后应在 60 次以内。

用法：
    .venv/bin/python scripts/video-loop-check.py
    CHROME=/path/to/chrome .venv/bin/python scripts/video-loop-check.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SERVER = REPO / "server"

# ui-smoke.py 里有现成的 CDP 客户端，直接复用。
# 文件名带连字符没法直接 import，只能走 importlib。
_spec = importlib.util.spec_from_file_location("ui_smoke", HERE / "ui-smoke.py")
ui_smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui_smoke)
Browser = ui_smoke.Browser
CHROME_CANDIDATES = ui_smoke.CHROME_CANDIDATES

# 在页面里数 /api/media 的请求次数。
# 用 PerformanceObserver 而不是 Network 域：img/video 的加载不走 fetch，
# 只有资源计时条目能真实反映「浏览器到底发起了多少次取图请求」。
COUNTER_JS = """
window.__mediaCount = 0;
try {
  new PerformanceObserver(function (list) {
    list.getEntries().forEach(function (e) {
      if (e.name.indexOf('/api/media') !== -1) window.__mediaCount++;
    });
  }).observe({ type: 'resource', buffered: true });
} catch (err) {
  window.__mediaCount = -1;
}
"""

SAMPLE_SECONDS = 15
# 15 秒内合理的上限。正常应该是十几次（3 个坏视频各一次 + 图片每 2 秒重挂一次），
# 修复前这里是几千次，所以 60 这个阈值有 100 倍余量、不会误报。
MAX_REQUESTS = 60


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if candidate and os.path.exists(candidate):
            return candidate
    sys.exit("找不到 Chrome/Chromium，请设置环境变量 CHROME=/path/to/chrome")


def main() -> int:
    from PIL import Image

    tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="picframe-videoloop-")))
    photos = tmp / "photos"
    photos.mkdir(parents=True)

    # 一张正常的图 + 三个「扩展名是 .mkv、内容是垃圾」的视频
    Image.new("RGB", (800, 600), (30, 90, 160)).save(photos / "ok.jpg", "JPEG")
    for i in range(3):
        (photos / f"broken{i}.mkv").write_bytes(b"\x00" * 4096 + b"not a real video" * 200)

    port = free_port()
    env = dict(os.environ)
    env.update(PHOTO_ROOTS=str(photos), DATA_DIR=str(tmp / "data"), CACHE_DIR=str(tmp / "cache"))

    logpath = tmp / "server.log"
    logfile = logpath.open("wb")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(SERVER), env=env, stdout=logfile, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    browser = None
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(("  \033[32m✓\033[0m " if ok else "  \033[31m✗\033[0m ") + label + (f"   {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    try:
        ready = False
        for _ in range(120):
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                    json.load(r)
                ready = True
                break
            except Exception:
                if server.poll() is not None:
                    break
                time.sleep(0.25)
        if not ready:
            logfile.flush()
            sys.exit("服务端没有就绪:\n" + logpath.read_text(errors="replace")[-2000:])

        # order=name 让顺序确定：broken0/1/2.mkv 在前，ok.jpg 最后
        body = json.dumps(
            {"settings": {"sources": [str(photos)], "order": "name", "interval_seconds": 2}}
        ).encode()
        req = urllib.request.Request(
            base + "/api/settings", data=body,
            headers={"Content-Type": "application/json"}, method="PUT",
        )
        urllib.request.urlopen(req, timeout=30).read()

        with urllib.request.urlopen(base + "/api/playlist", timeout=30) as r:
            playlist = json.load(r)
        check("播放列表含 4 项（3 个坏视频 + 1 张图）", playlist["total"] == 4, f"实际 {playlist['total']}")

        browser = Browser(find_chrome(), port=free_port())
        browser.send("Page.addScriptToEvaluateOnNewDocument", {"source": COUNTER_JS})
        browser.open(base + "/display")

        deadline = time.time() + SAMPLE_SECONDS
        while time.time() < deadline:
            browser.js("1")  # 顺便把 websocket 里的消息排空
            time.sleep(0.25)

        count = browser.js("window.__mediaCount")
        stalled = browser.js("typeof S !== 'undefined' ? S.stalled : null")
        failed = browser.js("typeof S !== 'undefined' && S.failed ? S.failed.size : -1")
        notice = browser.js(
            "(function(){var n=document.querySelector('#notice');"
            "return n && getComputedStyle(n).display !== 'none' "
            "? n.innerText.trim().replace(/\\s+/g,' ').slice(0,80) : '';})()"
        )

        print(f"\n  {SAMPLE_SECONDS} 秒内 /api/media 请求数: {count}")
        print(f"  已标记失败的视频数: {failed}   是否已停止: {stalled}")
        if notice:
            print(f"  屏幕提示: {notice}\n")
        else:
            print()

        check(f"/api/media 请求数 < {MAX_REQUESTS}（没有死循环）", isinstance(count, int) and 0 <= count < MAX_REQUESTS,
              f"实际 {count}")
        check("三个坏视频都被标记为失败", failed == 3, f"实际 {failed}")
        check("播放列表没有被清空（图片仍在播）", browser.js("typeof S !== 'undefined' && S.items.length === 4"))
    finally:
        if browser is not None:
            closer = getattr(browser, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
            else:
                try:
                    browser.proc.terminate()
                except Exception:
                    pass
            shutil.rmtree(getattr(browser, "dir", ""), ignore_errors=True)
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()
        logfile.close()
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\n\033[31m失败 {len(failures)} 项:\033[0m " + "；".join(failures))
        return 1
    print("\n\033[32m视频死循环回归通过\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
