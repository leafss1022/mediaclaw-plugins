"""企微可靠性回归：全部使用本地假传输，不发送真实消息、不创建真实订阅。"""

import asyncio
import hashlib
import time

import pytest
from mediaclaw_plugins.sdk import PluginCallbackRequest
from test_member_channels import FakeWeComHttp, _encrypt_wecom, _wecom


def callback(plugin, *, user="admin", content="帮助", msg_id="100", timestamp=None):
    config = plugin._config()
    xml = f"<xml><FromUserName>{user}</FromUserName><MsgType>text</MsgType><MsgId>{msg_id}</MsgId><Content>{content}</Content></xml>"
    encrypted = _encrypt_wecom(xml, config["corp_id"], config["encoding_aes_key"])
    stamp, nonce = str(timestamp or int(time.time())), "test-nonce"
    signature = hashlib.sha1(
        "".join(sorted([config["callback_token"], stamp, nonce, encrypted])).encode()
    ).hexdigest()
    return PluginCallbackRequest(
        path="wecom",
        method="POST",
        query={"timestamp": stamp, "nonce": nonce, "msg_signature": signature},
        headers={},
        body=f"<xml><Encrypt>{encrypted}</Encrypt></xml>".encode(),
    )


@pytest.mark.asyncio
async def test_callback_ack_does_not_wait_for_business_and_deduplicates():
    plugin, _ = _wecom()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def slow(message):
        calls.append(message)
        started.set()
        await release.wait()

    plugin._handle_message = slow
    request = callback(plugin)
    assert (
        await asyncio.wait_for(plugin.handle_callback(request), 0.5)
    ).status_code == 200
    await started.wait()
    assert (await plugin.handle_callback(request)).status_code == 200
    assert len(calls) == 1
    release.set()
    await plugin._worker
    assert (await plugin.handle_callback(request)).status_code == 200
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_old_signature_and_wrong_user_do_not_enter_queue():
    plugin, host = _wecom()
    assert (
        await plugin.handle_callback(
            callback(plugin, timestamp=int(time.time()) - 3600)
        )
    ).status_code == 403
    assert (
        await plugin.handle_callback(callback(plugin, user="outsider"))
    ).status_code == 403
    assert not host.members.created
    assert not host.http.post_calls


@pytest.mark.asyncio
async def test_saved_pending_message_recovers_and_duplicate_survives_restart():
    plugin, host = _wecom()
    plugin._spawn = lambda factory: None
    request = callback(plugin)
    assert (await plugin.handle_callback(request)).status_code == 200
    replacement, _ = _wecom()
    replacement.host = host
    replacement._load_delivery()
    replacement._start_worker()
    await replacement._worker
    count = len(host.http.post_calls)
    assert count == 1
    assert (await replacement.handle_callback(request)).status_code == 200
    assert len(host.http.post_calls) == count


@pytest.mark.asyncio
async def test_failed_persistence_is_not_acknowledged_or_deduplicated():
    plugin, host = _wecom()
    original = host.data.write_json

    def broken(key, value):
        if key == "delivery":
            raise OSError("disk full")
        original(key, value)

    host.data.write_json = broken
    request = callback(plugin)
    assert (await plugin.handle_callback(request)).status_code == 400
    assert not plugin._delivery["pending"] and not plugin._delivery["seen"]
    host.data.write_json = original
    assert (await plugin.handle_callback(request)).status_code == 200
    await plugin._worker


@pytest.mark.asyncio
async def test_disable_cancels_worker_without_losing_pending_message():
    plugin, _ = _wecom()
    started = asyncio.Event()

    async def waiting(message):
        started.set()
        await asyncio.Event().wait()

    plugin._handle_message = waiting
    await plugin.handle_callback(callback(plugin))
    await started.wait()
    plugin.on_disable()
    with pytest.raises(asyncio.CancelledError):
        await plugin._worker
    assert len(plugin._delivery["pending"]) == 1


@pytest.mark.asyncio
async def test_utf8_messages_are_split_without_losing_characters():
    plugin, host = _wecom()
    text = "电影🎬" * 800
    await plugin._send_text(text)
    parts = [args["json"]["text"]["content"] for _, args in host.http.post_calls]
    assert len(parts) > 1 and "".join(part.split("\n", 1)[1] for part in parts) == text
    assert all(len(part.encode()) <= 2000 for part in parts)


@pytest.mark.asyncio
async def test_invalid_recipients_and_later_failure_update_state():
    plugin, host = _wecom(http=FakeWeComHttp([{"errcode": 0, "invaliduser": "admin"}]))
    with pytest.raises(RuntimeError, match="接收对象无效"):
        await plugin.run()
    assert host.data.store["state"]["status"] == "连接失败"
    await plugin.run()
    assert host.data.store["state"]["status"] == "连接正常"
    host.http.send_results = [{"errcode": 60020}]
    await plugin.on_event("subscription.fulfilled", {"media": {"title": "沙丘"}})
    assert host.data.store["state"]["status"] == "发送失败"
    assert "可信 IP" in host.data.store["state"]["last_error"]


@pytest.mark.asyncio
async def test_token_requests_coalesce_and_credential_change_invalidates_cache():
    class SlowHttp(FakeWeComHttp):
        async def get(self, *args, **kwargs):
            await asyncio.sleep(0)
            return await super().get(*args, **kwargs)

    plugin, host = _wecom(http=SlowHttp())
    tokens = await asyncio.gather(*(plugin._access_token() for _ in range(5)))
    assert len(set(tokens)) == 1 and host.http.get_calls == 1
    host.config.get()["corp_secret"] = "changed"
    await plugin._access_token()
    assert host.http.get_calls == 2


@pytest.mark.asyncio
async def test_search_requires_selection_and_button_is_user_bound_and_single_use():
    plugin, host = _wecom(
        config={
            "admin_users": "admin,other",
            "member_bindings": "admin=alice,other=alice",
        }
    )
    await plugin._handle_message({"FromUserName": "admin", "Content": "订阅 沙丘"})
    assert host.members.created == []
    card = host.http.post_calls[-1][1]["json"]["template_card"]
    assert "2021" in card["sub_title_text"]
    key = card["button_list"][0]["key"]
    with pytest.raises(PermissionError):
        await plugin._handle_message({"FromUserName": "other", "EventKey": key})
    await plugin._handle_message({"FromUserName": "admin", "EventKey": key})
    assert len(host.members.created) == 1
    with pytest.raises(ValueError, match="已使用"):
        await plugin._handle_message({"FromUserName": "admin", "EventKey": key})
    assert len(host.members.created) == 1


@pytest.mark.asyncio
async def test_expired_selection_does_not_subscribe():
    plugin, host = _wecom()
    await plugin._handle_message({"FromUserName": "admin", "Content": "搜索 沙丘"})
    token = next(iter(plugin._choices))
    plugin._choices[token]["expires"] = time.time() - 1
    with pytest.raises(ValueError, match="过期"):
        await plugin._handle_message(
            {"FromUserName": "admin", "EventKey": "mc:sub:" + token}
        )
    assert not host.members.created


@pytest.mark.asyncio
async def test_menu_fingerprint_skips_unchanged_and_disable_removes_menu():
    plugin, host = _wecom()
    await plugin._sync_menu()
    await plugin._sync_menu()
    assert len(host.http.post_calls) == 1
    menu = host.http.post_calls[0][1]["json"]
    assert [row["name"] for row in menu["button"]] == [
        "控制中心",
        "影视服务",
        "更多功能",
    ]
    before = host.http.get_calls
    host.config.get()["control_enabled"] = False
    await plugin._sync_menu()
    assert host.http.get_calls == before + 1
    assert host.data.store["state"]["menu_status"] == "已移除"


@pytest.mark.asyncio
async def test_retry_is_bounded_and_manual_expired_token_has_clear_error(monkeypatch):
    plugin, host = _wecom(http=FakeWeComHttp([{"errcode": 45009}] * 4))

    async def fast(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", fast)
    with pytest.raises(RuntimeError, match="频率限制"):
        await plugin._send_text("测试")
    assert len(host.http.post_calls) == 3
    plugin, host = _wecom(
        http=FakeWeComHttp([{"errcode": 42001}]), config={"access_token": "manual"}
    )
    with pytest.raises(RuntimeError, match="已过期"):
        await plugin._send_text("测试")
    assert len(host.http.post_calls) == 1 and host.http.get_calls == 0


@pytest.mark.asyncio
async def test_actual_subscription_payload_formats_filters_and_redacts():
    plugin, host = _wecom()
    payload = {
        "media": {"title": "沙丘", "year": 2021},
        "secret": "should-not-send",
        "message": "https://private.example/token?secret=abc",
        "units": [[1, 1], [1, 2]],
    }
    await plugin.on_event("subscription.fulfilled", payload)
    text = host.http.post_calls[-1][1]["json"]["text"]["content"]
    assert "沙丘 (2021)" in text and "2 集" in text
    assert "should-not-send" not in text and "private.example" not in text
    host.config.get()["filter_keywords"] = "沙丘"
    await plugin.on_event("subscription.fulfilled", payload)
    assert len(host.http.post_calls) == 1
    host.config.get().update(filter_keywords="", event_patterns="")
    await plugin.on_event("subscription.fulfilled", payload)
    assert len(host.http.post_calls) == 1


def test_history_is_bounded_and_clear_preserves_deduplication():
    plugin, host = _wecom()
    for index in range(120):
        plugin._record("接收", "admin", "测试", "成功", str(index))
    assert len(host.data.store["history"]) == 100
    host.data.write_json("delivery", {"seen": {"key": time.time()}, "pending": []})
    assert plugin.clear_page()
    assert host.data.store["history"] == []
    assert host.data.store["delivery"]["seen"]


def test_config_user_lists_proxy_and_bindings_are_validated():
    plugin, host = _wecom()
    assert plugin._users("admin，other|third\nfourth") == [
        "admin",
        "other",
        "third",
        "fourth",
    ]
    host.config.get()["proxy_url"] = "https://trusted.example/wecom"
    plugin.validate_config(host.config.get())
    assert (
        plugin._base_url(host.config.get()) == "https://trusted.example/wecom/cgi-bin"
    )
    for url in ["http://plain.example", "https://user:pass@host", "file:///tmp"]:
        with pytest.raises(ValueError):
            plugin.validate_config({**host.config.get(), "proxy_url": url})
    with pytest.raises(ValueError, match="成员绑定"):
        plugin.validate_config({**host.config.get(), "member_bindings": "bad"})
