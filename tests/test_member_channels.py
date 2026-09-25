"""会员 Telegram 与企业微信插件的独立行为测试。"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from mediaclaw_plugins.sdk import PluginCallbackRequest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


telegram_module = _load(
    "market_member_telegram", ROOT / "plugins" / "member-telegram-bot" / "main.py"
)
wecom_module = _load(
    "market_enterprise_wecom", ROOT / "plugins" / "enterprise-wecom" / "main.py"
)


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self) -> dict:
        return self._payload


class FakeData:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    def read_json(self, key: str):
        return self.store.get(key)

    def write_json(self, key: str, value) -> None:
        self.store[key] = value


class FakeLogger:
    def info(self, *_args) -> None:
        pass

    def warning(self, *_args) -> None:
        pass

    def error(self, *_args) -> None:
        pass


class FakeMembers:
    def __init__(self) -> None:
        self.member = {
            "id": 7,
            "username": "alice",
            "nickname": "Alice",
            "allow_subscribe": True,
        }
        self.created: list[tuple[int, str]] = []

    async def get_by_username(self, username: str):
        return self.member if username == "alice" else None

    async def search_titles(self, member_id: int, query: str, *, limit: int):
        assert member_id == 7 and query == "沙丘" and limit == 5
        return [
            {
                "title_ref": "tmdb:movie:438631",
                "title": "沙丘",
                "year": 2021,
                "kind": "movie",
                "rating": 7.8,
            }
        ]

    async def create_subscription(self, member_id: int, title_ref: str):
        self.created.append((member_id, title_ref))
        return {"title": "沙丘"}

    async def list_subscriptions(self, member_id: int, *, limit: int):
        assert member_id == 7 and limit == 12
        return [{"title": "沙丘", "imported": 1, "total": 2}]


class FakeTelegramHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.fail_updates = False

    async def post(self, url: str, *, json: dict):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        if method == "getUpdates":
            if self.fail_updates:
                raise RuntimeError("临时网络故障")
            return FakeResponse({"ok": True, "result": self.updates})
        if method == "getMe":
            return FakeResponse({"ok": True, "result": {"id": 1, "username": "mc_bot"}})
        return FakeResponse({"ok": True, "result": True})


def _telegram(config: dict | None = None):
    plugin = telegram_module.MemberTelegramBot()
    http = FakeTelegramHttp()
    host = SimpleNamespace(
        config=SimpleNamespace(
            get=lambda: config
            or {"bot_token": "token", "member_bindings": "100=alice"}
        ),
        data=FakeData(),
        http=http,
        members=FakeMembers(),
        logger=FakeLogger(),
    )
    plugin.host = host
    return plugin, host


@pytest.mark.asyncio
async def test_telegram_unbound_user_receives_binding_id() -> None:
    plugin, host = _telegram({"bot_token": "token", "member_bindings": ""})
    await plugin._handle_update(
        {
            "message": {
                "chat": {"id": 99, "type": "private"},
                "from": {"id": 100},
                "text": "/start",
            }
        }
    )
    method, payload = host.http.calls[-1]
    assert method == "sendMessage"
    assert "100" in payload["text"] and "尚未绑定" in payload["text"]


@pytest.mark.asyncio
async def test_telegram_search_and_callback_create_subscription() -> None:
    plugin, host = _telegram()
    await plugin._handle_update(
        {
            "message": {
                "chat": {"id": 99, "type": "private"},
                "from": {"id": 100},
                "text": "/search 沙丘",
            }
        }
    )
    search_message = host.http.calls[-1][1]
    assert search_message["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == (
        "sub|tmdb:movie:438631"
    )

    await plugin._handle_update(
        {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 100},
                "message": {"chat": {"id": 99}},
                "data": "sub|tmdb:movie:438631",
            }
        }
    )
    assert host.members.created == [(7, "tmdb:movie:438631")]
    assert any(method == "answerCallbackQuery" for method, _payload in host.http.calls)


@pytest.mark.asyncio
async def test_telegram_poll_persists_offset_and_backs_off() -> None:
    plugin, host = _telegram()
    host.http.updates = [{"update_id": 12, "message": {}}]
    await plugin._poll(force=True)
    assert host.data.store["state"]["offset"] == 13

    host.http.fail_updates = True
    await plugin._poll(force=True)
    assert host.data.store["state"]["status"] == "连接失败"
    assert plugin._failures == 1 and plugin._next_poll_at > 0


class FakeWeComHttp:
    def __init__(self, send_results: list[dict] | None = None) -> None:
        self.get_calls = 0
        self.post_calls: list[tuple[str, dict]] = []
        self.send_results = list(send_results or [{"errcode": 0, "errmsg": "ok"}])

    async def get(self, _url: str, **_kwargs):
        self.get_calls += 1
        return FakeResponse(
            {"errcode": 0, "access_token": f"token-{self.get_calls}", "expires_in": 7200}
        )

    async def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        payload = self.send_results.pop(0) if self.send_results else {"errcode": 0}
        return FakeResponse(payload)


def _wecom(*, http=None, config: dict | None = None):
    plugin = wecom_module.EnterpriseWeCom()
    values = {
        "corp_id": "ww-corp",
        "agent_id": "100001",
        "corp_secret": "secret",
        "admin_users": "admin",
        "member_bindings": "admin=alice",
        "callback_token": "callback-token",
        "encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        "control_enabled": True,
    }
    if config:
        values.update(config)
    host = SimpleNamespace(
        config=SimpleNamespace(get=lambda: values),
        data=FakeData(),
        http=http or FakeWeComHttp(),
        members=FakeMembers(),
        logger=FakeLogger(),
    )
    plugin.host = host
    return plugin, host


@pytest.mark.asyncio
async def test_wecom_token_cache_and_single_expiry_retry() -> None:
    http = FakeWeComHttp(
        [
            {"errcode": 40014, "errmsg": "invalid access_token"},
            {"errcode": 0, "errmsg": "ok"},
            {"errcode": 0, "errmsg": "ok"},
        ]
    )
    plugin, _host = _wecom(http=http)
    await plugin._send_text("第一次")
    await plugin._send_text("第二次")
    assert http.get_calls == 2
    assert len(http.post_calls) == 3


def _encrypt_wecom(message: str, corp_id: str, encoding_aes_key: str) -> str:
    key = base64.b64decode(encoding_aes_key + "=")
    raw = os.urandom(16) + struct.pack("!I", len(message.encode())) + message.encode() + corp_id.encode()
    padding = 32 - len(raw) % 32
    padded = raw + bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


@pytest.mark.asyncio
async def test_wecom_callback_verifies_and_decrypts_echo() -> None:
    plugin, host = _wecom()
    config = host.config.get()
    encrypted = _encrypt_wecom("echo-ok", config["corp_id"], config["encoding_aes_key"])
    timestamp, nonce = "1700000000", "nonce"
    signature = hashlib.sha1(
        "".join(sorted([config["callback_token"], timestamp, nonce, encrypted])).encode()
    ).hexdigest()
    response = await plugin.handle_callback(
        PluginCallbackRequest(
            path="wecom",
            method="GET",
            query={
                "echostr": encrypted,
                "timestamp": timestamp,
                "nonce": nonce,
                "msg_signature": signature,
            },
            headers={},
            body=b"",
        )
    )
    assert response.status_code == 200 and response.body == "echo-ok"

    rejected = await plugin.handle_callback(
        PluginCallbackRequest(
            path="wecom",
            method="GET",
            query={"echostr": encrypted, "timestamp": timestamp, "nonce": nonce},
            headers={},
            body=b"",
        )
    )
    assert rejected.status_code == 403


@pytest.mark.asyncio
async def test_wecom_callback_handles_encrypted_admin_message() -> None:
    plugin, host = _wecom()
    config = host.config.get()
    message = (
        "<xml><FromUserName><![CDATA[admin]]></FromUserName>"
        "<Content><![CDATA[帮助]]></Content></xml>"
    )
    encrypted = _encrypt_wecom(message, config["corp_id"], config["encoding_aes_key"])
    timestamp, nonce = "1700000001", "nonce-2"
    signature = hashlib.sha1(
        "".join(sorted([config["callback_token"], timestamp, nonce, encrypted])).encode()
    ).hexdigest()
    body = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>".encode()
    response = await plugin.handle_callback(
        PluginCallbackRequest(
            path="wecom",
            method="POST",
            query={"timestamp": timestamp, "nonce": nonce, "msg_signature": signature},
            headers={"content-type": "text/xml"},
            body=body,
        )
    )
    assert response.status_code == 200 and response.body == "success"
    assert host.http.post_calls[-1][1]["json"]["touser"] == "admin"
    assert "搜索 片名" in host.http.post_calls[-1][1]["json"]["text"]["content"]


@pytest.mark.asyncio
async def test_wecom_rejects_user_outside_admin_whitelist() -> None:
    plugin, _host = _wecom()
    with pytest.raises(PermissionError, match="白名单"):
        await plugin._handle_message({"FromUserName": "outsider", "Content": "帮助"})
