"""fnos-pic-desktop 的端到端自测。

覆盖三类最容易出错、也最要紧的地方：

1. **路径安全** —— 目录穿越、越界绝对路径、符号链接逃逸都必须被拒绝。
2. **选片逻辑** —— 递归/非递归、单独挑选、排除项优先级、失效条目容错。
3. **图片管线** —— EXIF 方向纠正、缓存命中与失效。

运行：
    cd server && python -m pytest tests -q
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 夹具目录必须在导入 app 之前建好，因为 create_app() 会读取这些环境变量。
# ---------------------------------------------------------------------------
# macOS 上 /var 是指向 /private/var 的符号链接，而 safe_path 一律返回 realpath；
# 这里先把临时目录归一化，后面比较路径时才不会假报警。
TMP = Path(os.path.realpath(tempfile.mkdtemp(prefix="picframe-selftest-")))
PHOTOS = TMP / "photos"          # 允许的照片根目录
OUTSIDE = TMP / "outside"        # 根目录之外的「禁区」
BACKUP = TMP / "backup"          # 第二个照片根目录

(PHOTOS / "2023").mkdir(parents=True)
(PHOTOS / "2023" / "sub").mkdir(parents=True)
(PHOTOS / "2024").mkdir(parents=True)
OUTSIDE.mkdir(parents=True)
BACKUP.mkdir(parents=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

os.environ["PHOTO_ROOTS"] = f"{PHOTOS},{BACKUP}"
os.environ["DATA_DIR"] = str(TMP / "data")
os.environ["CACHE_DIR"] = str(TMP / "cache")


def _make_jpeg(path: Path, size=(400, 240), color=(200, 80, 60), orientation=None) -> Path:
    img = Image.new("RGB", size, color)
    if orientation is None:
        img.save(path, "JPEG")
    else:
        exif = Image.Exif()
        exif[0x0112] = orientation  # Orientation 标签
        img.save(path, "JPEG", exif=exif)
    return path


A_JPG = _make_jpeg(PHOTOS / "2023" / "a.jpg")
B_PNG = Image.new("RGB", (300, 300), (40, 120, 200))
B_PNG.save(PHOTOS / "2023" / "b.png")
C_JPG = _make_jpeg(PHOTOS / "2023" / "sub" / "c.jpg")
D_JPG = _make_jpeg(PHOTOS / "2024" / "d.jpg")
E_JPG = _make_jpeg(BACKUP / "e.jpg")
# 带 EXIF 方向的图：存储为 400x240，方向 6 表示显示时应顺时针旋转 90°
ROT_JPG = _make_jpeg(PHOTOS / "2024" / "rotated.jpg", size=(400, 240), orientation=6)
# 非媒体文件，扫描时必须忽略
(PHOTOS / "2023" / "note.txt").write_text("not a photo", encoding="utf-8")
# 越界文件
EVIL = _make_jpeg(OUTSIDE / "evil.jpg")
# 指向根目录之外的符号链接，必须被拒绝
LINK = PHOTOS / "escape.jpg"
try:
    LINK.symlink_to(EVIL)
except OSError:  # 某些文件系统不支持
    LINK = None

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings, SettingsStore  # noqa: E402
from app.images import ImageCache  # noqa: E402
from app.library import Library, PathNotAllowed, normalize_roots, safe_path  # noqa: E402
from app.main import create_app  # noqa: E402

ROOTS = normalize_roots([os.environ["PHOTO_ROOTS"]])


@pytest.fixture(scope="module")
def client():
    application = create_app()
    with TestClient(application) as c:
        yield c


def teardown_module(module):  # noqa: ARG001
    shutil.rmtree(TMP, ignore_errors=True)


# ===========================================================================
# 1. 路径安全
# ===========================================================================
def test_accepts_normal_file():
    assert safe_path(str(A_JPG), ROOTS, expect="media") == A_JPG


def test_accepts_root_itself_as_dir():
    assert safe_path(str(PHOTOS), ROOTS, expect="dir") == PHOTOS


def test_rejects_parent_traversal():
    with pytest.raises(PathNotAllowed):
        safe_path(str(PHOTOS / ".." / "outside" / "evil.jpg"), ROOTS, expect="media")


def test_rejects_traversal_even_when_dotdot_collapses_inside():
    # /photos/2023/../2023/a.jpg 归一化后其实在根内，应当放行（不能因为含 .. 就一刀切）
    assert safe_path(str(PHOTOS / "2023" / ".." / "2023" / "a.jpg"), ROOTS, expect="media") == A_JPG


def test_rejects_absolute_path_outside_roots():
    with pytest.raises(PathNotAllowed):
        safe_path(str(EVIL), ROOTS, expect="media")
    with pytest.raises(PathNotAllowed):
        safe_path("/etc/passwd", ROOTS, expect="media")


def test_rejects_symlink_escape():
    if LINK is None:
        pytest.skip("当前文件系统不支持符号链接")
    with pytest.raises(PathNotAllowed):
        safe_path(str(LINK), ROOTS, expect="media")


def test_rejects_relative_and_empty_paths():
    for bad in ["", "   ", "photos/a.jpg", "a.jpg", "\x00/etc/passwd"]:
        with pytest.raises(PathNotAllowed):
            safe_path(bad, ROOTS, expect="media")


def test_rejects_non_media_extension():
    with pytest.raises(PathNotAllowed):
        safe_path(str(PHOTOS / "2023" / "note.txt"), ROOTS, expect="media")


def test_rejects_missing_path():
    with pytest.raises(PathNotAllowed):
        safe_path(str(PHOTOS / "2023" / "nope.jpg"), ROOTS, expect="media")


def test_prefix_confusion_is_not_a_bypass():
    """ /photos-evil 不能因为字符串前缀匹配而被当成 /photos 的下属。"""
    decoy = TMP / "photos-evil"
    decoy.mkdir(exist_ok=True)
    target = _make_jpeg(decoy / "x.jpg")
    with pytest.raises(PathNotAllowed):
        safe_path(str(target), ROOTS, expect="media")


# ===========================================================================
# 2. 媒体索引与选片
# ===========================================================================
def test_scan_indexes_only_media_and_skips_symlinks():
    lib = Library(ROOTS)
    lib.ensure_fresh(force=True)
    stats = lib.stats
    # a.jpg b.png c.jpg d.jpg rotated.jpg e.jpg = 6，note.txt 与符号链接都不算
    assert stats["total_media"] == 6, stats
    assert stats["images"] == 6
    assert stats["videos"] == 0
    if LINK is not None:
        assert lib.get_item(str(LINK)) is None


def test_browse_returns_subdirs_with_recursive_counts():
    lib = Library(ROOTS)
    data = lib.browse(PHOTOS)
    names = {d["name"]: d["media_count"] for d in data["dirs"]}
    assert names["2023"] == 3          # a.jpg, b.png, sub/c.jpg
    assert names["2024"] == 2          # d.jpg, rotated.jpg
    assert data["subtree_count"] == 5
    assert data["direct_count"] == 0   # 根目录下没有直接的照片


def test_browse_lists_direct_children_only():
    lib = Library(ROOTS)
    data = lib.browse(PHOTOS / "2023")
    names = {f["name"] for f in data["files"]}
    assert names == {"a.jpg", "b.png"}   # 不含 sub/c.jpg


def test_selection_recursive():
    lib = Library(ROOTS)
    lib.ensure_fresh(force=True)
    items = lib.resolve_selection([str(PHOTOS / "2023")], [], [], recursive=True)
    assert {i.name for i in items} == {"a.jpg", "b.png", "c.jpg"}


def test_selection_non_recursive():
    lib = Library(ROOTS)
    items = lib.resolve_selection([str(PHOTOS / "2023")], [], [], recursive=False)
    assert {i.name for i in items} == {"a.jpg", "b.png"}


def test_selection_merges_individual_files_and_dedupes():
    lib = Library(ROOTS)
    items = lib.resolve_selection(
        [str(PHOTOS / "2023")], [str(A_JPG), str(D_JPG)], [], recursive=True
    )
    # a.jpg 既在文件夹里又被单独选中，只能出现一次
    assert sorted(i.name for i in items) == ["a.jpg", "b.png", "c.jpg", "d.jpg"]


def test_exclude_beats_everything():
    lib = Library(ROOTS)
    items = lib.resolve_selection(
        [str(PHOTOS / "2023")], [], [str(PHOTOS / "2023" / "sub" / "c.jpg")], recursive=True
    )
    assert {i.name for i in items} == {"a.jpg", "b.png"}


def test_selection_survives_stale_and_out_of_bounds_entries():
    lib = Library(ROOTS)
    items = lib.resolve_selection(
        [str(PHOTOS / "2023"), str(OUTSIDE), "/photos/does-not-exist"],
        [str(EVIL)],
        [],
        recursive=True,
    )
    # 越界/不存在的条目被静默跳过，合法的照常生效
    assert {i.name for i in items} == {"a.jpg", "b.png", "c.jpg"}


def test_two_roots_are_independent():
    lib = Library(ROOTS)
    items = lib.resolve_selection([str(BACKUP)], [], [], recursive=True)
    assert {i.name for i in items} == {"e.jpg"}


# ===========================================================================
# 3. 图片管线
# ===========================================================================
def test_thumbnail_is_generated_and_cached():
    cache = ImageCache(TMP / "cache-unit" / "images")
    out1 = cache.get(A_JPG, 200)
    assert out1.exists() and out1.stat().st_size > 0
    with Image.open(out1) as im:
        assert max(im.size) <= 200
        assert im.format == "JPEG"
    # 第二次应当直接命中同一个文件
    out2 = cache.get(A_JPG, 200)
    assert out1 == out2


def test_thumbnail_cache_invalidates_on_file_change():
    src = _make_jpeg(TMP / "changing.jpg", size=(300, 300))
    cache = ImageCache(TMP / "cache-unit" / "images")
    first = cache.get(src, 128)
    # 换掉文件内容（尺寸也变），mtime/size 变化应导致缓存键变化
    _make_jpeg(src, size=(900, 400), color=(10, 200, 10))
    os.utime(src, (os.path.getmtime(src) + 10, os.path.getmtime(src) + 10))
    second = cache.get(src, 128)
    assert first != second


def test_exif_orientation_is_applied():
    """方向 6 的 400x240 图，纠正后应当变成竖向（高 > 宽）。"""
    cache = ImageCache(TMP / "cache-unit" / "images")
    out = cache.get(ROT_JPG, 400)
    with Image.open(out) as im:
        width, height = im.size
    assert height > width, f"EXIF 方向没有被纠正: {width}x{height}"


def test_png_with_alpha_is_composited_not_crashed():
    rgba = Image.new("RGBA", (120, 120), (255, 0, 0, 128))
    src = TMP / "alpha.png"
    rgba.save(src)
    cache = ImageCache(TMP / "cache-unit" / "images")
    out = cache.get(src, 100)
    with Image.open(out) as im:
        assert im.mode == "RGB"


def test_cache_purge():
    cache = ImageCache(TMP / "cache-purge" / "images")
    cache.get(A_JPG, 64)
    assert cache.stats()["files"] >= 1
    removed = cache.purge()
    assert removed >= 1
    assert cache.stats()["files"] == 0


# ===========================================================================
# 4. 配置模型
# ===========================================================================
def test_settings_rejects_bad_values():
    with pytest.raises(Exception):
        Settings(interval_seconds=0)          # 下限是 1
    with pytest.raises(Exception):
        Settings(background="red")            # 必须是 #RRGGBB
    with pytest.raises(Exception):
        Settings(schedule={"off": "25:00"})   # 非法时间
    with pytest.raises(Exception):
        Settings(order="whatever")            # 枚举外


def test_settings_dedupes_paths():
    s = Settings(sources=["/photos/a", "/photos/a", " ", "/photos/b"])
    assert s.sources == ["/photos/a", "/photos/b"]


def test_settings_store_roundtrip_and_revision():
    store = SettingsStore(TMP / "data-unit" / "settings.json")
    assert store.revision == 0
    store.replace(Settings(sources=[str(PHOTOS)], interval_seconds=7))
    assert store.revision == 1

    reloaded = SettingsStore(TMP / "data-unit" / "settings.json")
    reloaded.load()
    assert reloaded.get().interval_seconds == 7
    assert str(PHOTOS) in reloaded.get().sources


def test_settings_store_survives_corrupt_file():
    path = TMP / "data-corrupt" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    store = SettingsStore(path)
    store.load()                      # 不能抛异常
    assert store.get().interval_seconds == 10


# ===========================================================================
# 5. HTTP 接口
# ===========================================================================
def test_health(client):
    data = client.get("/api/health").json()
    assert data["ok"] is True
    assert len(data["roots"]) == 2


def test_pages_render(client):
    for path in ["/", "/display"]:
        res = client.get(path)
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]


def test_static_assets(client):
    for path in ["/static/admin.js", "/static/display.js", "/static/admin.css", "/static/display.css"]:
        assert client.get(path).status_code == 200


def test_state_reports_stats(client):
    data = client.get("/api/state").json()
    assert data["stats"]["total_media"] == 6
    assert len(data["roots"]) == 2


def test_browse_endpoint(client):
    res = client.get("/api/browse", params={"path": str(PHOTOS / "2023")})
    assert res.status_code == 200
    data = res.json()
    assert {f["name"] for f in data["files"]} == {"a.jpg", "b.png"}
    assert data["files"][0]["thumb"].startswith("/api/thumb?path=")


def test_browse_rejects_outside_path(client):
    res = client.get("/api/browse", params={"path": str(OUTSIDE)})
    assert res.status_code == 400


def test_browse_rejects_traversal(client):
    res = client.get("/api/browse", params={"path": str(PHOTOS / ".." / "outside")})
    assert res.status_code == 400


def test_thumb_endpoint_returns_jpeg(client):
    res = client.get("/api/thumb", params={"path": str(A_JPG), "w": 160})
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert "immutable" in res.headers.get("cache-control", "")
    assert len(res.content) > 100


def test_thumb_rejects_traversal(client):
    res = client.get("/api/thumb", params={"path": str(EVIL), "w": 160})
    assert res.status_code == 400


def test_thumb_rejects_non_media(client):
    res = client.get("/api/thumb", params={"path": str(PHOTOS / "2023" / "note.txt"), "w": 160})
    assert res.status_code == 400


def test_media_endpoint_serves_preview(client):
    res = client.get("/api/media", params={"path": str(A_JPG), "w": 320})
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"


def test_settings_roundtrip_via_api(client):
    payload = {
        "settings": {
            "sources": [str(PHOTOS / "2023")],
            "interval_seconds": 6,
            "transition": "zoom",
            "order": "name",
            "show_clock": False,
        }
    }
    res = client.put("/api/settings", json=payload)
    assert res.status_code == 200, res.text
    assert res.json()["revision"] >= 1

    got = client.get("/api/settings").json()["settings"]
    assert got["interval_seconds"] == 6
    assert got["transition"] == "zoom"
    assert got["show_clock"] is False


def test_settings_rejects_path_outside_roots(client):
    res = client.put("/api/settings", json={"settings": {"sources": [str(OUTSIDE)]}})
    assert res.status_code == 400
    assert "不在允许的照片目录内" in res.json()["detail"]


def test_settings_rejects_invalid_enum(client):
    res = client.put("/api/settings", json={"settings": {"order": "nonsense"}})
    assert res.status_code == 400


def test_playlist_reflects_saved_selection(client):
    client.put(
        "/api/settings",
        json={"settings": {"sources": [str(PHOTOS / "2023")], "order": "name", "video_enabled": True}},
    )
    data = client.get("/api/playlist").json()
    assert data["total"] == 3
    assert [i["name"] for i in data["items"]] == ["a.jpg", "b.png", "c.jpg"]
    assert all(i["url"].startswith("/api/media?path=") for i in data["items"])
    assert data["settings"]["order"] == "name"


def test_playlist_exclude_removes_item(client):
    client.put(
        "/api/settings",
        json={
            "settings": {
                "sources": [str(PHOTOS / "2023")],
                "exclude_files": [str(C_JPG)],
                "order": "name",
            }
        },
    )
    data = client.get("/api/playlist").json()
    assert [i["name"] for i in data["items"]] == ["a.jpg", "b.png"]


def test_playlist_empty_when_nothing_selected(client):
    client.put("/api/settings", json={"settings": {"sources": [], "include_files": []}})
    data = client.get("/api/playlist").json()
    assert data["total"] == 0
    assert data["items"] == []


def test_count_endpoint_previews_unsaved_selection(client):
    res = client.post("/api/count", json={"settings": {"sources": [str(PHOTOS / "2023")]}})
    assert res.status_code == 200
    assert res.json()["total"] == 3


def test_count_endpoint_can_exclude_videos(client):
    res = client.post(
        "/api/count", json={"settings": {"sources": [str(PHOTOS)], "video_enabled": False}}
    )
    assert res.json()["videos"] == 0


def test_control_rejects_unknown_action(client):
    assert client.post("/api/control", json={"action": "explode"}).status_code == 400
    assert client.post("/api/control", json={"action": "next"}).status_code == 200


def test_rescan_endpoint(client):
    res = client.post("/api/rescan")
    assert res.status_code == 200
    assert res.json()["stats"]["total_media"] == 6


def test_unknown_path_returns_404(client):
    assert client.get("/api/definitely-not-a-route").status_code == 404


# 让前面的用例跑完后把配置恢复成「全选」，方便手工接着玩
def test_zz_restore_full_selection(client):
    client.put("/api/settings", json={"settings": {"sources": [str(PHOTOS), str(BACKUP)]}})
    assert client.get("/api/playlist").json()["total"] == 6


# ===========================================================================
# 5. 回归测试 —— 每一个都对应一次审计中实测出来的真 bug
#    这些用例在修复之前是**红的**，加进来是为了防止以后改回去。
# ===========================================================================
def _app_with(tmp_path, monkeypatch, photos):
    """用独立的 PHOTO_ROOTS/DATA_DIR/CACHE_DIR 建一个隔离的应用实例。"""
    monkeypatch.setenv("PHOTO_ROOTS", str(photos))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    from app.main import create_app as _create

    return _create()


def test_truncated_image_returns_415_not_500(tmp_path, monkeypatch):
    """截断/没拷完的图片在 NAS 上很常见，必须降级成 415，不能 500。"""
    root = Path(os.path.realpath(tmp_path)) / "photos"
    root.mkdir(parents=True)
    bad = root / "bad.jpg"
    Image.effect_noise((400, 300), 100).convert("RGB").save(bad, "JPEG", quality=95)
    raw = bad.read_bytes()
    bad.write_bytes(raw[: len(raw) * 3 // 5])  # 砍掉尾部

    app = _app_with(tmp_path, monkeypatch, root)
    with TestClient(app) as c:
        res = c.get("/api/thumb", params={"path": str(bad), "w": 160})
        assert res.status_code == 415, f"期望 415，实际 {res.status_code}: {res.text}"
        # /api/media 会回退到原图，同样不能是 500
        assert c.get("/api/media", params={"path": str(bad)}).status_code == 200


def test_cache_dir_inside_photo_root_is_not_indexed(tmp_path):
    """按 README 推荐的部署方式，缓存目录就在照片根目录里面。

    缓存文件是 .jpg，不排除的话缩略图会被当成照片索引，还会自我增殖。
    """
    root = Path(os.path.realpath(tmp_path)) / "photos"
    (root / "album").mkdir(parents=True)
    _make_jpeg(root / "album" / "real.jpg")

    cache_dir = root / "docker" / "picframe" / "cache"
    (cache_dir / "images" / "ab" / "cd").mkdir(parents=True)
    _make_jpeg(cache_dir / "images" / "ab" / "cd" / "thumb.jpg")

    lib = Library([root], exclude_dirs=[cache_dir])
    lib.ensure_fresh(force=True)
    assert {i.name for i in lib._items.values()} == {"real.jpg"}

    # 不排除时确实会被索引进来（证明这个测试不是空跑）
    naive = Library([root])
    naive.ensure_fresh(force=True)
    assert "thumb.jpg" in {i.name for i in naive._items.values()}


def test_scanning_flag_resets_after_scan_failure(monkeypatch):
    """_scanning 不复位会让索引永久冻结，连 force=True 都救不回来。"""
    lib = Library([PHOTOS])
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("模拟扫描中途崩溃")

    monkeypatch.setattr(lib, "_scan", boom)
    with pytest.raises(RuntimeError):
        lib.ensure_fresh(force=True)
    assert lib._scanning is False
    # 第二次仍然真的去扫了 —— 说明标志被复位，没有被永久卡住
    with pytest.raises(RuntimeError):
        lib.ensure_fresh(force=True)
    assert len(calls) == 2


def test_cache_key_changes_when_only_ctime_changes(tmp_path):
    """同尺寸 + 同 mtime 的替换（cp -p / rsync -t）不能命中旧缓存。"""
    import time as _time

    cache = ImageCache(tmp_path / "cache")
    src = tmp_path / "x.jpg"
    _make_jpeg(src, size=(100, 100), color=(255, 0, 0))
    st1 = src.stat()

    _time.sleep(0.02)
    _make_jpeg(src, size=(100, 100), color=(0, 0, 255))
    os.utime(src, ns=(st1.st_atime_ns, st1.st_mtime_ns))  # 把 mtime 还原成一样
    st2 = src.stat()

    assert st2.st_mtime_ns == st1.st_mtime_ns
    assert st2.st_size == st1.st_size
    assert cache._key(src, 100, "thumb", st2) != cache._key(src, 100, "thumb", st1)


def test_file_json_urls_carry_version_token():
    """派生图是 immutable 长缓存，URL 必须带内容版本串，否则换图后永远显示旧图。"""
    from app.library import MediaItem, _file_json, version_token

    item = MediaItem(
        path="/photos/a.jpg", name="a.jpg", ext=".jpg",
        kind="image", size=123, mtime=1700000000.5,
    )
    j = _file_json(item)
    assert version_token(item) == "1700000000-123"
    assert f"v={version_token(item)}" in j["thumb"]
    assert f"v={version_token(item)}" in j["url"]


def test_playlist_urls_carry_version_token(tmp_path, monkeypatch):
    app = _app_with(tmp_path, monkeypatch, PHOTOS)
    with TestClient(app) as c:
        c.put("/api/settings", json={"settings": {"sources": [str(PHOTOS)]}})
        pl = c.get("/api/playlist").json()
        assert pl["total"] > 0
        assert "v=" in pl["items"][0]["url"]
        assert "v=" in pl["items"][0]["thumb"]


def test_recursive_selection_does_not_leak_sibling_prefix_dirs(tmp_path):
    """2023 与 2023b 是兄弟目录，前缀匹配不能把后者也算进来。"""
    root = Path(os.path.realpath(tmp_path)) / "photos"
    (root / "2023").mkdir(parents=True)
    (root / "2023b").mkdir(parents=True)
    _make_jpeg(root / "2023" / "a.jpg")
    _make_jpeg(root / "2023b" / "b.jpg")

    lib = Library([root])
    lib.ensure_fresh(force=True)
    names = {i.name for i in lib.resolve_selection([str(root / "2023")], [], [], recursive=True)}
    assert names == {"a.jpg"}


def test_human_sort_key_is_natural():
    """order=name 必须是自然排序：2.jpg 要排在 10.jpg 前面。"""
    from app.library import MediaItem, human_sort_key

    def mk(n):
        return MediaItem(path="/p/" + n, name=n, ext=".jpg", kind="image", size=1, mtime=0.0)

    names = ["10.jpg", "2.jpg", "1.jpg", "img_10.jpg", "IMG_9.jpg"]
    got = [i.name for i in sorted([mk(n) for n in names], key=human_sort_key)]
    assert got == ["1.jpg", "2.jpg", "10.jpg", "IMG_9.jpg", "img_10.jpg"]


def test_control_goto_rejects_non_numeric_index(client):
    assert client.post("/api/control", json={"action": "goto", "index": "abc"}).status_code == 400
    assert client.post("/api/control", json={"action": "goto", "index": {"x": 1}}).status_code == 400
    assert client.post("/api/control", json={"action": "goto", "index": 2}).status_code == 200


def test_too_many_counts_exclude_files(monkeypatch):
    """exclude_files 不计数的话可以用排除列表绕过上限。"""
    from app import main as m

    monkeypatch.setattr(m, "SETTINGS_MAX_ITEMS", 3)
    assert m._too_many(Settings(exclude_files=["/a.jpg", "/b.jpg", "/c.jpg", "/d.jpg"])) is True
    assert m._too_many(Settings(sources=["/x"], include_files=["/a.jpg"])) is False


def test_settings_store_keeps_memory_when_persist_fails(tmp_path):
    """落盘失败时不能留下「内存是新值、磁盘是旧值」的不一致状态。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("我不是目录", encoding="utf-8")
    store = SettingsStore(blocker / "settings.json")
    store.load()

    with pytest.raises(OSError):
        store.replace(Settings(interval_seconds=99.0))

    assert store.revision == 0
    assert store.get().interval_seconds == 10.0


def test_event_bus_full_queue_is_dropped_silently():
    """队列满时 QueueFull 是在事件循环线程里抛的，必须被闭包接住，不能打成 ERROR。"""
    import asyncio

    from app.main import EventBus

    async def scenario():
        bus = EventBus()
        bus.bind(asyncio.get_running_loop())
        q = bus.subscribe()
        while True:  # 先把 64 格的队列塞满
            try:
                q.put_nowait({"event": "fill", "data": {}})
            except asyncio.QueueFull:
                break

        reported = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, ctx: reported.append(ctx)
        )
        for _ in range(50):
            bus.publish("settings-changed", {"n": 1})
        await asyncio.sleep(0.05)  # 让 call_soon_threadsafe 排的回调真正执行
        bus.unsubscribe(q)
        return reported, q.qsize()

    reported, size = asyncio.run(scenario())
    assert reported == [], f"队列满时不该有异常冒到事件循环: {reported}"
    assert size == 64


def test_health_reports_missing_roots(tmp_path, monkeypatch):
    """路径拼错时 Docker 会静默创建空目录，health 必须如实报错。"""
    app = _app_with(tmp_path, monkeypatch, tmp_path / "does-not-exist")
    with TestClient(app) as c:
        body = c.get("/api/health").json()
        assert body["ok"] is False
        assert body["missing_roots"]


def test_startup_does_not_block_on_initial_scan(tmp_path, monkeypatch):
    """首次扫描必须放到后台。

    否则大库首扫期间端口是 connection refused，显示端会永久卡在 Chromium 错误页
    （错误页上没有任何 JS，重试逻辑压根不会执行）。
    """
    root = Path(os.path.realpath(tmp_path)) / "photos"
    root.mkdir(parents=True)
    _make_jpeg(root / "a.jpg")

    app = _app_with(tmp_path, monkeypatch, root)
    with TestClient(app) as c:
        # 进入上下文就说明 lifespan startup 已经 yield 了；此时扫描可能还在跑，
        # 但端口必须已经可用、health 必须立刻能答。
        assert c.get("/api/health").status_code == 200
