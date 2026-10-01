"""截图式企微图文：真实协议载荷与展示信息校验，不向企业微信发送消息。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_member_channels import _wecom


def articles(host):
    return [
        args["json"]["news"]["articles"][0]
        for _, args in host.http.post_calls
        if args["json"]["msgtype"] == "news"
    ]


@pytest.mark.asyncio
async def test_import_uses_backdrop_and_current_unit_inventory():
    plugin, host = _wecom(config={"public_base_url": "https://media.example"})
    host.control = SimpleNamespace(
        media_summary=AsyncMock(
            return_value={
                "rating": 8.5,
                "overview": "本地已有简介",
                "libraries": ["国产剧"],
                "quality": ["WEB-DL 2160p"],
                "file_count": 2,
                "size_bytes": 3 * 1024**3,
            }
        )
    )
    await plugin.on_event(
        "subscription.fulfilled",
        {
            "media": {
                "item_id": 9,
                "title": "我不是大师",
                "year": 2026,
                "type": "series",
                "backdrop_path": "/banner.jpg",
                "poster_path": "/poster.jpg",
            },
            "units": [[1, 15], [1, 16]],
            "secret": "do-not-send",
        },
    )
    host.control.media_summary.assert_awaited_once_with(9, units=[[1, 15], [1, 16]])
    card = articles(host)[0]
    assert "S01E15-E16" in card["title"] and "已入库" in card["title"]
    assert card["picurl"].endswith("/w780/banner.jpg")
    assert all(
        value in card["description"]
        for value in (
            "8.5",
            "国产剧",
            "WEB-DL 2160p",
            "2 个文件",
            "3.0 GB",
            "本地已有简介",
        )
    )
    assert "do-not-send" not in str(card)
    assert card["url"] == "https://media.example/library"


@pytest.mark.asyncio
async def test_download_includes_safe_torrent_details_and_unknowns_stay_unknown():
    plugin, host = _wecom(config={"public_base_url": "https://media.example"})
    await plugin.on_event(
        "subscription.download_started",
        {
            "media": {"title": "沙丘", "kind": "movie", "backdrop_path": "/banner.jpg"},
            "torrent": {
                "site_id": "测试站",
                "title": "Dune",
                "spec": "WEB-DL 2160p",
                "seeders": 0,
                "promotion": "免费",
                "hit_and_run": False,
                "size_bytes": 3 * 1024**3,
                "download_url": "https://site/passkey=secret",
            },
        },
    )
    card = articles(host)[0]
    assert "开始下载" in card["title"]
    assert all(
        text in card["description"]
        for text in ("测试站", "3.0 GB", "WEB-DL 2160p", "做种：0", "免费", "H&R：否")
    )
    assert "passkey" not in str(card) and "评分" not in card["description"]


@pytest.mark.asyncio
async def test_search_image_precedes_user_bound_button_and_subscription_result_is_visual():
    plugin, host = _wecom(config={"public_base_url": "https://media.example"})
    host.members.search_titles = AsyncMock(
        return_value=[
            {
                "title_ref": "tmdb:movie:438631",
                "title": "沙丘",
                "year": 2021,
                "kind": "movie",
                "rating": 7.8,
                "backdrop_url": "https://image.tmdb.org/t/p/w780/dune.jpg",
                "overview": "沙漠星球的故事",
            }
        ]
    )
    await plugin._handle_message({"FromUserName": "admin", "Content": "搜索 沙丘"})
    assert "沙漠星球" in articles(host)[0]["description"]
    assert not host.members.created
    key = host.http.post_calls[-1][1]["json"]["template_card"]["button_list"][0]["key"]
    await plugin._handle_message({"FromUserName": "admin", "EventKey": key})
    assert articles(host)[-1]["title"].startswith("已加入订阅")
    assert articles(host)[-1]["picurl"].endswith("dune.jpg")
    with pytest.raises(ValueError, match="已使用"):
        await plugin._handle_message({"FromUserName": "admin", "EventKey": key})
    assert len(host.members.created) == 1


@pytest.mark.asyncio
async def test_brand_fallback_relative_asset_and_utf8_limits():
    plugin, host = _wecom(config={"public_base_url": "https://media.example/base/"})
    await plugin._send_visual("测试" * 200, "中文简介" * 500)
    card = articles(host)[0]
    assert card["picurl"] == "https://media.example/base/backdrop-default.jpg"
    assert (
        len(card["title"].encode()) <= 128 and len(card["description"].encode()) <= 512
    )
    local = plugin._article(
        "本地", "简介", {"backdrop_url": "/images/assets/a.jpg?v=1"}
    )
    assert local["picurl"] == "https://media.example/base/images/assets/a.jpg?v=1"
    assert plugin._public_url("https://example/a?access_token=secret") == ""
    assert plugin._public_url("https://user:pass@example/a") == ""
    assert plugin._public_url("file:///secret") == ""


@pytest.mark.asyncio
async def test_no_public_base_can_use_tmdb_or_report_text_fallback():
    plugin, host = _wecom()
    await plugin._send_visual(
        "沙丘",
        "简介",
        media={
            "title_ref": "tmdb:movie:438631",
            "poster_path": "/poster.jpg",
        },
    )
    assert articles(host)[0]["url"] == "https://www.themoviedb.org/movie/438631"
    await plugin._send_visual("系统", "没有公开图片")
    assert host.http.post_calls[-1][1]["json"]["msgtype"] == "text"
    assert "缺少" in host.data.store["state"]["last_visual_warning"]


@pytest.mark.asyncio
async def test_metadata_failure_still_sends_event_and_filter_still_suppresses_it():
    plugin, host = _wecom(config={"public_base_url": "https://media.example"})
    host.control = SimpleNamespace(
        media_summary=AsyncMock(side_effect=RuntimeError("db down"))
    )
    data = {"media": {"item_id": 2, "title": "沙丘"}}
    await plugin.on_event("subscription.fulfilled", data)
    assert "已入库" in articles(host)[0]["title"]
    host.config.get()["filter_keywords"] = "沙丘"
    await plugin.on_event("subscription.fulfilled", data)
    assert len(articles(host)) == 1


def test_noncontiguous_episodes_do_not_claim_missing_episode():
    plugin, _ = _wecom()
    assert plugin._unit_label([[1, 15], [1, 17]]) == "S01E15 / S01E17"


@pytest.mark.asyncio
async def test_playback_and_subscription_list_also_use_news():
    plugin, host = _wecom(
        config={
            "public_base_url": "https://media.example",
            "event_patterns": "playback.*",
        }
    )
    await plugin.on_event(
        "playback.started",
        {
            "media": {
                "title": "测试剧",
                "type": "episode",
                "season_number": 1,
                "episode_number": 15,
                "backdrop_path": "/banner.jpg",
            },
            "playback": {"position_ms": 3000, "duration_ms": 10000},
        },
    )
    assert "S01E15" in articles(host)[0]["title"]
    assert "播放进度：30.0%" in articles(host)[0]["description"]
    await plugin._handle_message({"FromUserName": "admin", "Content": "我的订阅"})
    assert "我的订阅" in articles(host)[-1]["title"]
    assert "1/2" in articles(host)[-1]["description"]
