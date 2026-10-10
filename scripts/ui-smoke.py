#!/usr/bin/env python3
"""前端冒烟测试：用真实浏览器打开管理端和显示端，断言它们确实工作。

服务端逻辑由 ``server/tests/test_picframe.py`` 覆盖；这个脚本补上它测不到的部分 ——
前端 JS 在真实浏览器里有没有报错、有没有真的把照片渲染出来、点保存有没有生效。

为什么用 DevTools Protocol 而不是 ``chrome --screenshot``：
显示页有持续运行的 CSS 动画（Ken Burns）和定时器，会让 Chrome 的
``--virtual-time-budget`` 死锁；不加预算又会在 JS 执行完之前就截图。
CDP 可以真实等待并直接求值，是唯一可靠的做法。

用法：
    python scripts/ui-smoke.py http://127.0.0.1:18080
    python scripts/ui-smoke.py http://127.0.0.1:18080 --chrome "/path/to/chrome"

脚本会在测试前后**快照并还原**服务端的播放配置，所以可以安全地对着正在使用的相册跑。
（测试过程中会临时改动来源选择，完成后恢复。）

依赖：pip install websocket-client
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

try:
    import websocket
except ImportError:
    sys.exit("缺少依赖，请先执行: pip install websocket-client")

CHROME_CANDIDATES = [
    os.environ.get("CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
    shutil.which("chromium-browser") or "",
]

PASS, FAIL = "  \033[32m✓\033[0m", "  \033[31m✗\033[0m"
_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{PASS if ok else FAIL} {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def _get(url: str):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def _put_settings(base: str, settings: dict) -> dict:
    body = json.dumps({"settings": settings}).encode()
    req = urllib.request.Request(base + "/api/settings", data=body,
                                 headers={"Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def ensure_configured(base: str) -> tuple[bool, int]:
    """显示端只有在选中了来源时才会渲染照片。

    服务端刚装好时 sources 是空的（播放列表 0 项），这时显示端正确地显示「没有照片」。
    为了让冒烟测试不依赖前置状态，这里自动选中第一个照片根目录。
    返回 (是否改动了配置, 播放列表项数)。
    """
    total = _get(base + "/api/playlist").get("total", 0)
    if total:
        return False, total

    roots = _get(base + "/api/health").get("roots") or []
    if not roots:
        sys.exit("服务端没有配置照片根目录（PHOTO_ROOTS），无法测试显示端")

    _put_settings(base, {"sources": [roots[0]]})

    req = urllib.request.Request(base + "/api/rescan", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        json.load(r)

    return True, _get(base + "/api/playlist").get("total", 0)


class Browser:
    """一个最小的 CDP 客户端。"""

    def __init__(self, chrome: str, port: int = 19341):
        self.dir = tempfile.mkdtemp(prefix="picframe-ui-")
        self.proc = subprocess.Popen(
            [
                chrome,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                f"--user-data-dir={self.dir}",
                "--no-first-run",
                "--hide-scrollbars",
                "--window-size=1560,1040",
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.port = port
        self.ws = None
        self._mid = 0
        self.events: list[dict] = []
        self._connect()

    def _connect(self) -> None:
        last: Exception | None = None
        for _ in range(80):
            target = None
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as r:
                    for t in json.load(r):
                        if t.get("type") == "page":
                            target = t
                            break
            except Exception as exc:
                last = exc
            if target:
                try:
                    self.ws = websocket.create_connection(
                        target["webSocketDebuggerUrl"], timeout=20, suppress_origin=True
                    )
                    break
                except Exception as exc:
                    last = exc  # DevTools HTTP 端口先就绪、WebSocket 还没起来
            time.sleep(0.25)
        if self.ws is None:
            raise RuntimeError(f"无法连接 Chrome 的 DevTools 端口 {self.port}: {last}")
        for method in ("Runtime.enable", "Page.enable", "Log.enable"):
            self.send(method)
        # 同时捕获 error 和 unhandledrejection。
        # async 函数里同步抛出的异常（比如 TDZ 的 ReferenceError）只会变成
        # promise rejection，window.onerror 完全看不到 —— 之前正是这样漏掉了一个
        # 「显示端整页空白、却一条报错都没有」的 bug。
        self.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": (
                "window.__errs=[];"
                "window.addEventListener('error',function(e){"
                "window.__errs.push('error: '+e.message)});"
                "window.addEventListener('unhandledrejection',function(e){"
                "window.__errs.push('rejection: '+((e.reason&&e.reason.message)||String(e.reason)))});"
            )},
        )

    def send(self, method: str, params: dict | None = None) -> dict:
        self._mid += 1
        self.ws.send(json.dumps({"id": self._mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._mid:
                return msg
            self.events.append(msg)

    def js(self, expression: str):
        r = self.send("Runtime.evaluate",
                      {"expression": expression, "returnByValue": True, "awaitPromise": True})
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return {"__js_error__": str(res["exceptionDetails"].get("text"))}
        return res.get("result", {}).get("value")

    def open(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})

    def wait_for(self, expression: str, timeout: float = 20.0):
        end = time.time() + timeout
        while time.time() < end:
            v = self.js(expression)
            if v:
                return v
            time.sleep(0.4)
        return None

    def console_errors(self) -> list[str]:
        out = []
        for e in self.events:
            if e.get("method") == "Log.entryAdded":
                entry = e["params"]["entry"]
                if entry.get("level") == "error" and "favicon" not in entry.get("url", ""):
                    out.append(entry.get("text", ""))
        return out

    def close(self) -> None:
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.proc.kill()
        self.proc.wait(timeout=10)
        shutil.rmtree(self.dir, ignore_errors=True)


def test_display(b: Browser, base: str) -> None:
    print("\n\033[1m显示端 /display\033[0m")
    b.open(base + "/display")

    ok = b.wait_for(
        "(()=>{const i=document.querySelector('img.fg');"
        "return !!(i&&i.complete&&i.naturalWidth>0)})()",
        timeout=30,
    )
    check("照片已加载并解码", bool(ok))

    src = b.js("(document.querySelector('img.fg')||{}).getAttribute?.('src') || ''")
    check("图片地址指向相册服务", bool(src) and "/api/media?" in src, (src or "")[:64] + "…")

    check("两个图层（交叉淡入淡出）", b.js("document.querySelectorAll('.layer').length") == 2)
    check("至少一个图层可见", (b.js("document.querySelectorAll('.layer.visible').length") or 0) >= 1)
    check("Ken Burns 已启用", b.js("!!document.querySelector('.frame.kb')") is True)
    check("模糊背景层已生成", b.js("!!document.querySelector('img.bg')") is True)

    # 时钟是 setInterval 驱动的，第一次 tick 在 1 秒后，所以要等
    clock = b.wait_for("(document.getElementById('clock')||{}).textContent || ''", timeout=6)
    check("时钟已渲染", bool(clock), (clock or "").strip())

    caption = b.js("(document.getElementById('caption')||{}).textContent || ''")
    check("照片说明已渲染", bool(caption), (caption or "").strip())

    # 必须查**计算样式**，不能只查 hidden 属性。
    # #notice 自己声明了 display:flex，会盖掉 UA 样式表里的 [hidden]{display:none}
    # （作者样式整体优先于 UA 样式，跟选择器权重无关），于是属性是 true、
    # 元素却照样铺满整屏。之前就是因为只断言了属性，漏掉了这个
    # 「照片解码正常但整屏被『正在连接…』遮住」的致命 bug。
    notice_visible = b.js(
        "(()=>{const n=document.getElementById('notice');"
        "if(!n||getComputedStyle(n).display==='none')return false;"
        "const r=n.getBoundingClientRect();return r.width>100&&r.height>100;})()"
    )
    check("空库提示真的不可见（不只是设了 hidden）", notice_visible is False)
    check(
        "照片没有被提示层遮挡",
        b.js(
            "(()=>{const i=document.querySelector('img.fg');if(!i)return false;"
            "const r=i.getBoundingClientRect();"
            "const el=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);"
            "return !!(el&&(el===i||el.contains(i)||i.contains(el)));})()"
        ) is True,
    )

    # #banner 曾经只加 .show 类、从不摘掉 hidden，导致所有操作反馈都看不见
    b.js("flashBanner('冒烟测试')")
    time.sleep(0.3)
    check(
        "反馈提示真的可见",
        b.js(
            "(()=>{const n=document.getElementById('banner');"
            "if(!n||getComputedStyle(n).display==='none')return false;"
            "const r=n.getBoundingClientRect();return r.width>0&&r.height>0;})()"
        ) is True,
    )

    attrs = b.js("[document.body.dataset.transition, document.body.dataset.fit].join('/')")
    check("播放设置已应用", bool(attrs), str(attrs))
    check("页面无 JS 错误", b.js("window.__errs") == [])
    check("无控制台错误", not b.console_errors(), "; ".join(b.console_errors())[:120])


def test_admin(b: Browser, base: str) -> None:
    print("\n\033[1m管理端 /\033[0m")
    # 从「什么都没选」的干净状态开始，点击语义才是确定的：
    # 如果来源里已经有某个上层目录，再点子目录会变成「排除」而不是「勾选」。
    _put_settings(base, {"sources": []})
    b.events.clear()
    b.open(base + "/")

    ok = b.wait_for("document.querySelectorAll('.navroot').length > 0", timeout=20)
    check("照片根目录已列出", bool(ok))
    check("目录树已渲染", (b.js("document.querySelectorAll('#dirlist .dirrow').length") or 0) >= 1)
    check("保存按钮初始为禁用", b.js("document.getElementById('btn-save').disabled") is True)

    # 勾选当前目录
    b.js("document.querySelector('#dirlist .dirrow input[type=checkbox]').click()")
    time.sleep(0.6)
    check("勾选后保存按钮变为可用", b.js("document.getElementById('btn-save').disabled") is False)
    check("已选列表出现条目", (b.js("document.querySelectorAll('#chips-sources .chip').length") or 0) == 1)

    count = b.wait_for("document.getElementById('status-count').textContent || ''", timeout=8)
    check("实时预估播放数量", bool(count), (count or "").strip())

    # 保存
    b.js("document.getElementById('btn-save').click()")
    toast = b.wait_for(
        "(()=>{const t=document.getElementById('toast');return (t&&!t.hidden)?t.textContent:null})()",
        timeout=15,
    )
    check("保存成功并提示", bool(toast), str(toast))

    saved = b.js("fetch('/api/playlist').then(r=>r.json()).then(d=>d.total)")
    check("服务端播放列表已生效", (saved or 0) > 0, f"共 {saved} 项")

    # 设置页
    b.js("document.querySelector('.tab[data-tab=settings]').click()")
    time.sleep(0.4)
    check("设置面板可切换", b.js("document.getElementById('panel-settings').classList.contains('active')") is True)
    fields = b.js("document.querySelectorAll('[data-path]').length") or 0
    check("设置项已渲染", fields >= 20, f"{fields} 个字段")

    # 保存竞态：PUT 在途时继续改设置，那些改动不能被一并标成「已保存」。
    # 修复前 state.saved 是在 await 之后才读 state.settings，于是在途改动被标记为
    # 已保存、但从未发给服务端 —— 改动静默丢失，保存按钮还变灰了，
    # 用户不会再去点第二次。这里给 PUT 注入 1.2 秒延迟来稳定复现。
    b.js(
        "(()=>{const orig=window.fetch;"
        "window.fetch=function(u,o){"
        "if(o&&o.method==='PUT'&&String(u).indexOf('/api/settings')!==-1)"
        "return new Promise(r=>setTimeout(r,1200)).then(()=>orig.call(window,u,o));"
        "return orig.call(window,u,o);};})()"
    )
    b.js("document.querySelector('[data-path=ken_burns]').click()")
    time.sleep(0.2)
    b.js("document.getElementById('btn-save').click()")
    time.sleep(0.3)
    b.js("document.querySelector('[data-path=show_clock]').click()")  # 保存期间又改一处
    time.sleep(3.0)
    check(
        "保存期间的改动不会被误标为已保存",
        b.js(
            "(state.saved.ken_burns===false && state.saved.show_clock===true"
            " && state.settings.show_clock===false"
            " && document.getElementById('btn-save').disabled===false)"
        ) is True,
    )
    # 把这两项改回默认值再存一次，别把状态留给后面的用例
    b.js("document.querySelector('[data-path=ken_burns]').click()")
    b.js("document.querySelector('[data-path=show_clock]').click()")
    time.sleep(0.2)
    b.js("document.getElementById('btn-save').click()")
    time.sleep(2.0)

    check("页面无 JS 错误", b.js("window.__errs") == [])
    errs = b.console_errors()
    check("无控制台错误", not errs, "; ".join(errs)[:120])


def main() -> int:
    ap = argparse.ArgumentParser(description="fnos-pic-desktop 前端冒烟测试")
    ap.add_argument("base", help="相册服务地址，例如 http://127.0.0.1:18080")
    ap.add_argument("--chrome", help="Chrome / Chromium 可执行文件路径")
    ap.add_argument("--port", type=int, default=19341, help="DevTools 调试端口")
    args = ap.parse_args()

    chrome = args.chrome or next((c for c in CHROME_CANDIDATES if c and os.path.exists(c)), None)
    if not chrome:
        sys.exit("找不到 Chrome / Chromium，请用 --chrome 指定路径")

    base = args.base.rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
            health = json.load(r)
    except Exception as exc:
        sys.exit(f"连不上相册服务 {base}: {exc}")
    print(f"相册服务 {base}  版本 {health.get('version')}  HEIC={health.get('heif')}  ffmpeg={health.get('ffmpeg')}")
    if not health.get("roots"):
        print("警告: 服务端没有配置任何照片根目录，显示端会是空的")

    changed, total = ensure_configured(base)
    if changed:
        print(f"服务端尚未选中任何来源，已自动选中第一个照片根目录（播放列表 {total} 项）")
    else:
        print(f"播放列表已有 {total} 项，沿用现有配置")

    # 快照，测完还原 —— 冒烟测试不该弄乱用户已经调好的播放配置
    original = _get(base + "/api/settings")["settings"]

    b = Browser(chrome, args.port)
    try:
        test_display(b, base)
        test_admin(b, base)
    finally:
        b.close()
        try:
            _put_settings(base, original)
            print("\n已还原原有播放配置")
        except Exception as exc:
            print(f"\n警告: 还原原有配置失败（{exc}），请到管理界面确认一下")

    print()
    if _failures:
        print(f"\033[31m失败 {len(_failures)} 项:\033[0m " + "; ".join(_failures))
        return 1
    print("\033[32m全部通过\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
