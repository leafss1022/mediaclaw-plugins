"""内置媒体库封面插件的选择范围、生成结果和错误反馈回归。"""

import asyncio
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "cover_plugin",
    Path(__file__).resolve().parents[1] / "plugins/media-cover-generator/main.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def instance(config=None, generate=None):
    plugin = module.MediaCoverGenerator()
    plugin.plugin_id = "media-cover-generator"
    store = {}
    calls = []

    async def libraries():
        return [{"id": 1, "name": "电影"}, {"id": 2, "name": "剧集"}]

    async def generated(library_id, **kwargs):
        calls.append((library_id, kwargs))
        return {"applied": True, "cover_url": f"/libraries/{library_id}/cover?v=test"}

    plugin.host = SimpleNamespace(
        config=SimpleNamespace(get=lambda: config or {}),
        library=SimpleNamespace(
            list_libraries=libraries, generate_library_cover=generate or generated
        ),
        data=SimpleNamespace(read_json=store.get, write_json=store.__setitem__),
        logger=logging.getLogger("cover-test"),
    )
    return plugin, store, calls


def test_selected_builtin_libraries_are_applied_and_recorded():
    plugin, store, calls = instance(
        {"include_libraries": [2], "cover_style": "static_3"}
    )
    asyncio.run(plugin.refresh())
    assert calls == [(2, {"style": "static_3", "apply": True, "replace_custom": False})]
    assert store["history"][0]["status"] == "已应用"
    assert store["summary"]["generated_count"] == 1
    assert plugin.page()["elements"]


def test_failures_are_recorded_and_reported_while_other_libraries_continue():
    async def generate(library_id, **kwargs):
        if library_id == 1:
            raise OSError("海报损坏")
        return {"applied": False, "reason": "已保留手动封面"}

    plugin, store, _calls = instance(generate=generate)
    with pytest.raises(RuntimeError, match="1 个媒体库"):
        asyncio.run(plugin.refresh())
    assert store["summary"]["failed_count"] == 1
    assert store["summary"]["skipped_count"] == 1
    assert [row["status"] for row in store["history"]] == ["失败", "已跳过"]
    assert plugin._running is False


def test_old_host_reports_missing_generation_capability():
    plugin, _store, _calls = instance()
    del plugin.host.library.generate_library_cover
    with pytest.raises(RuntimeError, match="先更新主程序"):
        asyncio.run(plugin.refresh())


@pytest.mark.parametrize("ids", [[999], ["invalid"], [-1]])
def test_invalid_selection_never_generates_for_all_libraries(ids):
    plugin, _store, calls = instance({"include_libraries": ids})
    with pytest.raises(ValueError):
        asyncio.run(plugin.refresh())
    assert calls == []


def test_clear_records_does_not_call_library_operations():
    plugin, store, calls = instance()
    asyncio.run(plugin.refresh())
    assert len(calls) == 2
    assert plugin.clear_page() is True
    assert store["history"] == []
    assert len(calls) == 2


def test_page_uses_mediaclaw_builtin_preview_images_without_mp_copy():
    plugin, _store, _calls = instance()

    page = plugin.page()
    rendered = str(page)

    for index in range(1, 5):
        assert f"/images/library-covers/static_{index}.webp" in rendered
    assert "MoviePilot" not in rendered
    assert " MP " not in f" {rendered} "
