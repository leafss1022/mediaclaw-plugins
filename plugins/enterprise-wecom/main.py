"""MediaClaw 企业微信通知与安全远程控制插件。

借鉴 DaquanClaw 的应用消息、Token 缓存、白名单、菜单同步和回调验签流程；
宿主业务操作全部经 MediaClaw 插件 SDK 执行。
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import json
import struct
import time
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from mediaclaw_plugins.sdk import (
    PluginBase,
    PluginCallbackRequest,
    PluginCallbackResponse,
)

_API = "https://qyapi.weixin.qq.com/cgi-bin"


class EnterpriseWeCom(PluginBase):
    """企业微信自建应用通道，所有入站控制均经过签名和用户白名单。"""

    def __init__(self) -> None:
        super().__init__()
        self._token = ""
        self._token_expires_at = 0.0

    def on_enable(self) -> None:
        config = self._config()
        self._validate_connection_config(config)
        if config.get("control_enabled"):
            self._validate_callback_config(config)
        if self.host is not None:
            self.host.logger.info(
                "企业微信插件已启用，通知用户 %d 人，远程控制：%s",
                len(self._users(config.get("admin_users"))),
                "开启" if config.get("control_enabled") else "关闭",
            )

    async def run(self) -> None:
        """测试连接、发送测试消息；开启控制时同时同步应用菜单。"""
        config = self._config()
        self._validate_connection_config(config)
        started = time.monotonic()
        await self._send_text(
            "MediaClaw 企业微信连接成功\n"
            f"应用 ID：{config.get('agent_id')}\n"
            f"接收用户：{'、'.join(self._users(config.get('admin_users')))}"
        )
        if config.get("control_enabled"):
            self._validate_callback_config(config)
            await self._sync_menu()
        self._write_state(
            status="连接正常",
            last_test_at=self._now(),
            latency_ms=round((time.monotonic() - started) * 1000),
            last_error="",
        )
        if self.host is not None:
            self.host.logger.info("企业微信连接测试成功")

    async def on_event(self, event: str, data: dict) -> None:
        """把宿主领域事件按配置筛选后发送给管理员白名单。"""
        if not any(fnmatch.fnmatch(event, pattern) for pattern in self._event_patterns()):
            return
        try:
            body = json.dumps(data, ensure_ascii=False, default=str)
            await self._send_text(f"MediaClaw 通知\n事件：{event}\n{body[:1600]}")
            self._write_state(last_event=event, last_event_at=self._now(), last_error="")
        except Exception as exc:  # noqa: BLE001 -- 事件通知失败不能中断宿主事件总线
            self._write_state(last_error=str(exc))
            if self.host is not None:
                self.host.logger.error("发送企业微信事件通知失败（%s）：%s", event, exc)

    def page(self) -> dict:
        config = self._config()
        state = self._state()
        base = str(config.get("public_base_url") or "").rstrip("/")
        callback_url = f"{base}/api/v1/plugin-callbacks/enterprise-wecom/wecom" if base else "请先配置外部访问地址"
        return {
            "elements": [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "success" if state.get("status") == "连接正常" else "info",
                        "title": state.get("status") or "等待连接测试",
                        "text": (
                            f"通知用户：{len(self._users(config.get('admin_users')))} 人；"
                            f"远程控制：{'开启' if config.get('control_enabled') else '关闭'}；"
                            f"最近测试：{state.get('last_test_at') or '-'}"
                        ),
                    },
                },
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "title": "企业微信回调地址",
                        "text": callback_url,
                    },
                },
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning" if state.get("last_error") else "info",
                        "title": "最近状态",
                        "text": state.get("last_error") or "暂无错误。点击“立即运行”可测试连接并同步应用菜单。",
                    },
                },
            ]
        }

    async def handle_callback(
        self, request: PluginCallbackRequest
    ) -> PluginCallbackResponse | None:
        if request.path.strip("/") != "wecom":
            return None
        config = self._config()
        if not config.get("control_enabled"):
            return PluginCallbackResponse("remote control disabled", status_code=403)
        try:
            self._validate_callback_config(config)
            if request.method == "GET":
                echo = request.query.get("echostr", "")
                self._verify_signature(request.query, echo, config)
                return PluginCallbackResponse(self._decrypt(echo, config))
            encrypted = self._encrypted_xml(request.body)
            self._verify_signature(request.query, encrypted, config)
            message = self._parse_xml(self._decrypt(encrypted, config).encode("utf-8"))
            await self._handle_message(message)
            return PluginCallbackResponse("success")
        except PermissionError as exc:
            if self.host is not None:
                self.host.logger.warning("企业微信回调被拒绝：%s", exc)
            return PluginCallbackResponse("invalid signature", status_code=403)
        except Exception as exc:  # noqa: BLE001 -- 回调边界统一转为平台可识别的响应
            if self.host is not None:
                self.host.logger.error("企业微信回调处理失败：%s", exc)
            return PluginCallbackResponse("callback failed", status_code=400)

    async def _handle_message(self, message: dict[str, str]) -> None:
        user_id = message.get("FromUserName", "")
        if user_id not in self._users(self._config().get("admin_users")):
            raise PermissionError("发送用户不在管理员白名单")
        event = message.get("Event", "").lower()
        text = (message.get("Content") or message.get("EventKey") or "").strip()
        if event == "click":
            text = {
                "mc_help": "帮助",
                "mc_subscriptions": "我的订阅",
            }.get(text, text)
        if text in {"帮助", "菜单", "/help"}:
            await self._send_text(
                "MediaClaw 企业微信助手\n"
                "搜索 片名 - 搜索影视\n"
                "订阅 片名 - 搜索并订阅首个结果\n"
                "我的订阅 - 查看处理进度",
                recipients=[user_id],
            )
            return
        member = await self._member_for(user_id)
        if member is None:
            await self._send_text(
                f"企业微信 UserID {user_id} 尚未绑定 MediaClaw 成员，请管理员配置成员绑定。",
                recipients=[user_id],
            )
            return
        if text == "我的订阅":
            rows = await self.host.members.list_subscriptions(member["id"], limit=12)
            lines = ["我的订阅"] + [
                f"{row['title']} · {row['imported']}/{row['total']} 已入库" for row in rows
            ]
            await self._send_text("\n".join(lines) if rows else "你还没有创建或关注订阅。", recipients=[user_id])
            return
        command, _, query = text.partition(" ")
        if command not in {"搜索", "订阅"} or not query.strip():
            await self._send_text("无法识别命令，发送“帮助”查看可用操作。", recipients=[user_id])
            return
        rows = await self.host.members.search_titles(member["id"], query.strip(), limit=5)
        if not rows:
            await self._send_text("没有找到相关影视。", recipients=[user_id])
            return
        if command == "搜索":
            lines = [f"搜索：{query.strip()}"]
            for index, row in enumerate(rows, 1):
                year = f" ({row['year']})" if row.get("year") else ""
                lines.append(f"{index}. {row['title']}{year} · {row.get('kind') or '-'}")
            lines.append("发送“订阅 片名”可创建订阅。")
            await self._send_text("\n".join(lines), recipients=[user_id])
            return
        result = await self.host.members.create_subscription(member["id"], rows[0]["title_ref"])
        await self._send_text(f"已加入订阅：{result['title']}", recipients=[user_id])

    async def _member_for(self, user_id: str) -> dict | None:
        if self.host is None:
            return None
        username = self._bindings().get(user_id)
        return await self.host.members.get_by_username(username) if username else None

    async def _sync_menu(self, *, _retried: bool = False) -> None:
        token = await self._access_token()
        config = self._config()
        response = await self.host.http.post(
            f"{_API}/menu/create",
            params={"access_token": token, "agentid": str(config["agent_id"])},
            json={
                "button": [
                    {"type": "click", "name": "我的订阅", "key": "mc_subscriptions"},
                    {"type": "click", "name": "使用帮助", "key": "mc_help"},
                ]
            },
        )
        await self._wecom_result(
            response,
            retry=lambda: self._sync_menu(_retried=True),
            allow_retry=not _retried,
        )

    async def _send_text(
        self,
        text: str,
        *,
        recipients: list[str] | None = None,
        _retried: bool = False,
    ) -> None:
        config = self._config()
        users = recipients or self._users(config.get("admin_users"))
        if not users:
            raise ValueError("请配置至少一个企业微信管理员用户 ID")
        token = await self._access_token()
        response = await self.host.http.post(
            f"{_API}/message/send",
            params={"access_token": token},
            json={
                "touser": "|".join(users),
                "msgtype": "text",
                "agentid": int(config["agent_id"]),
                "text": {"content": text[:2000]},
                "safe": 0,
            },
        )
        await self._wecom_result(
            response,
            retry=lambda: self._send_text(text, recipients=users, _retried=True),
            allow_retry=not _retried,
        )

    async def _access_token(self, *, force: bool = False) -> str:
        config = self._config()
        manual = str(config.get("access_token") or "").strip()
        if manual:
            return manual
        if not force and self._token and time.monotonic() < self._token_expires_at:
            return self._token
        response = await self.host.http.get(
            f"{_API}/gettoken",
            params={"corpid": config["corp_id"], "corpsecret": config["corp_secret"]},
        )
        result = response.json()
        if not response.is_success or int(result.get("errcode") or 0) != 0:
            raise RuntimeError(self._error(result, response.status_code))
        self._token = str(result.get("access_token") or "")
        if not self._token:
            raise RuntimeError("企业微信未返回 Access Token")
        expires = max(60, int(result.get("expires_in") or 7200) - 60)
        self._token_expires_at = time.monotonic() + expires
        return self._token

    async def _wecom_result(self, response, *, retry, allow_retry: bool) -> dict:
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError(f"企业微信返回无法解析的响应（HTTP {response.status_code}）") from exc
        code = int(result.get("errcode") or 0)
        if response.is_success and code == 0:
            return result
        if (
            allow_retry
            and code in {40014, 42001}
            and not self._config().get("access_token")
        ):
            self._token = ""
            self._token_expires_at = 0.0
            await self._access_token(force=True)
            return await retry()
        raise RuntimeError(self._error(result, response.status_code))

    def _verify_signature(self, query: dict[str, str], encrypted: str, config: dict) -> None:
        signature = query.get("msg_signature") or query.get("signature") or ""
        timestamp = query.get("timestamp") or ""
        nonce = query.get("nonce") or ""
        expected = hashlib.sha1(
            "".join(sorted([str(config["callback_token"]), timestamp, nonce, encrypted])).encode()
        ).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            raise PermissionError("消息签名不匹配")

    def _decrypt(self, encrypted: str, config: dict) -> str:
        try:
            key = base64.b64decode(str(config["encoding_aes_key"]) + "=")
            ciphertext = base64.b64decode(encrypted)
        except (ValueError, TypeError) as exc:
            raise ValueError("企业微信加密消息格式无效") from exc
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        padding = padded[-1]
        if padding < 1 or padding > 32 or padded[-padding:] != bytes([padding]) * padding:
            raise ValueError("企业微信消息填充无效")
        plain = padded[:-padding]
        if len(plain) < 20:
            raise ValueError("企业微信消息长度无效")
        length = struct.unpack("!I", plain[16:20])[0]
        message = plain[20 : 20 + length]
        receiver = plain[20 + length :].decode("utf-8")
        if receiver != str(config["corp_id"]):
            raise PermissionError("企业微信消息接收方不匹配")
        return message.decode("utf-8")

    def _encrypted_xml(self, body: bytes) -> str:
        return self._parse_xml(body).get("Encrypt", "")

    @staticmethod
    def _parse_xml(body: bytes) -> dict[str, str]:
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise ValueError("不允许包含外部实体的 XML")
        if len(body) > 1024 * 1024:
            raise ValueError("XML 消息过大")
        root = ET.fromstring(body)
        return {child.tag: str(child.text or "") for child in root}

    def _validate_connection_config(self, config: dict) -> None:
        if not str(config.get("corp_id") or "").strip():
            raise ValueError("请填写企业微信企业 ID")
        if not str(config.get("agent_id") or "").isdigit():
            raise ValueError("企业微信应用 ID 只能填写数字")
        if not config.get("corp_secret") and not config.get("access_token"):
            raise ValueError("请填写企业微信应用 Secret 或 Access Token")
        if not self._users(config.get("admin_users")):
            raise ValueError("请填写至少一个企业微信管理员用户 ID")

    @staticmethod
    def _validate_callback_config(config: dict) -> None:
        token = str(config.get("callback_token") or "")
        aes_key = str(config.get("encoding_aes_key") or "")
        if not token:
            raise ValueError("启用远程控制前请填写回调 Token")
        if len(aes_key) != 43 or not aes_key.isalnum():
            raise ValueError("消息加密密钥必须是 43 位字母或数字")

    def _bindings(self) -> dict[str, str]:
        result: dict[str, str] = {}
        raw = str(self._config().get("member_bindings") or "")
        for item in raw.replace("\n", ",").replace(";", ",").split(","):
            user_id, separator, username = item.strip().partition("=")
            if separator and user_id and username.strip():
                result[user_id] = username.strip()
        return result

    def _event_patterns(self) -> list[str]:
        patterns = self._users(self._config().get("event_patterns"))
        return patterns or ["subscription.*"]

    @staticmethod
    def _users(value) -> list[str]:
        return list(
            dict.fromkeys(
                item.strip()
                for item in str(value or "").replace("\n", ",").replace(";", ",").split(",")
                if item.strip()
            )
        )

    @staticmethod
    def _error(result: dict, status: int) -> str:
        message = str(result.get("errmsg") or "").strip()
        return f"企业微信：{message}" if message and message != "ok" else f"企业微信 HTTP {status}"

    def _config(self) -> dict:
        return self.host.config.get() if self.host is not None else {}

    def _state(self) -> dict:
        if self.host is None:
            return {}
        value = self.host.data.read_json("state")
        return value if isinstance(value, dict) else {}

    def _write_state(self, **values) -> None:
        if self.host is None:
            return
        state = self._state()
        state.update(values)
        self.host.data.write_json("state", state)

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")


plugin = EnterpriseWeCom
