"""MediaClaw 会员 Telegram 机器人。

借鉴 DaquanClaw 会员机器人的菜单、限流和轮询退避，但成员、影视搜索和订阅
都通过 MediaClaw 插件 SDK 完成，不复制会员数据库或订阅流程。
"""

from __future__ import annotations

import html
import time

from mediaclaw_plugins.sdk import PluginBase

_MENU = {
    "keyboard": [
        [{"text": "搜索影视"}, {"text": "我的订阅"}],
        [{"text": "会员信息"}, {"text": "帮助"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


class MemberTelegramBot(PluginBase):
    """轮询 Telegram Bot API，并把命令映射到 MediaClaw 成员能力。"""

    def __init__(self) -> None:
        super().__init__()
        self._running = False
        self._failures = 0
        self._next_poll_at = 0.0
        self._rate_limits: dict[str, list[float]] = {}

    def on_enable(self) -> None:
        if self.host is None:
            return
        config = self.host.config.get()
        if not str(config.get("bot_token") or "").strip():
            raise ValueError("请先配置 Telegram Bot Token")
        interval = self._bounded_int(config.get("poll_interval_seconds"), 5, 3, 60)
        self.host.scheduler.register(
            "poll",
            self._poll,
            title="会员 Telegram 机器人轮询",
            trigger_type="interval",
            interval_seconds=interval,
            description="接收会员指令并执行 MediaClaw 搜索与订阅操作",
        )
        self.host.logger.info("会员 Telegram 机器人已启用，轮询间隔 %d 秒", interval)

    async def run(self) -> None:
        """“立即运行”用于验证 Token、同步命令并立即拉取一次消息。"""
        me = await self._telegram("getMe")
        await self._telegram(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "打开会员机器人"},
                    {"command": "search", "description": "搜索影视"},
                    {"command": "request", "description": "搜索并创建订阅"},
                    {"command": "my", "description": "查看我的订阅"},
                    {"command": "account", "description": "查看会员信息"},
                    {"command": "help", "description": "查看帮助"},
                ]
            },
        )
        self._write_state(
            status="连接正常",
            bot_username=str(me.get("username") or ""),
            last_test_at=self._now(),
            last_error="",
        )
        if self.host is not None:
            self.host.logger.info("Telegram 连接成功：@%s", me.get("username") or me.get("id"))
        await self._poll(force=True)

    def page(self) -> dict:
        state = self._state()
        bindings = self._bindings()
        return {
            "elements": [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "success" if state.get("status") == "连接正常" else "info",
                        "title": state.get("status") or "等待首次连接",
                        "text": (
                            f"Bot：@{state.get('bot_username') or '-'}；"
                            f"已绑定 {len(bindings)} 个成员；"
                            f"最近轮询：{state.get('last_poll_at') or '-'}"
                        ),
                    },
                },
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning" if state.get("last_error") else "info",
                        "title": "最近状态",
                        "text": state.get("last_error") or "暂无错误。点击“立即运行”可测试连接并同步命令菜单。",
                    },
                },
            ]
        }

    async def _poll(self, force: bool = False) -> None:
        if self._running or (not force and time.monotonic() < self._next_poll_at):
            return
        self._running = True
        try:
            state = self._state()
            updates = await self._telegram(
                "getUpdates",
                {
                    "offset": int(state.get("offset") or 0),
                    "limit": 50,
                    "timeout": 0,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
            offset = int(state.get("offset") or 0)
            for update in updates if isinstance(updates, list) else []:
                offset = max(offset, int(update.get("update_id") or 0) + 1)
                # 先持久化 offset 再执行 115 等外部动作，避免轮询重试重复转存。
                self._write_state(offset=offset)
                try:
                    await self._handle_update(update)
                except Exception as exc:  # noqa: BLE001 -- 单条坏消息不能阻断后续轮询
                    if self.host is not None:
                        self.host.logger.error("处理 Telegram 消息失败：%s", exc)
            self._failures = 0
            self._next_poll_at = 0.0
            self._write_state(
                status="连接正常",
                offset=offset,
                last_poll_at=self._now(),
                last_error="",
            )
        except Exception as exc:  # noqa: BLE001 -- 调度边界需记录失败并执行退避
            self._failures += 1
            delay = min(300, 5 * (2 ** min(self._failures - 1, 6)))
            self._next_poll_at = time.monotonic() + delay
            self._write_state(status="连接失败", last_error=str(exc), last_poll_at=self._now())
            if self.host is not None:
                self.host.logger.error("Telegram 轮询失败，%d 秒后重试：%s", delay, exc)
        finally:
            self._running = False

    async def _handle_update(self, update: dict) -> None:
        callback = update.get("callback_query") or {}
        if callback:
            await self._handle_callback(callback)
            return
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("type") != "private" or not chat.get("id") or not sender.get("id"):
            return
        chat_id = str(chat["id"])
        member = await self._member_for(str(sender["id"]))
        if member is None:
            await self._send(
                chat_id,
                "尚未绑定 MediaClaw 成员账号。\n"
                f"你的 Telegram 用户 ID：<code>{html.escape(str(sender['id']))}</code>\n"
                "请让管理员把“用户ID=成员登录名”加入插件配置。",
            )
            return
        text = str(message.get("text") or "").strip()
        if text and await self._dispatch_resource(
            chat_id,
            text,
            source_ref=f"update:{int(update.get('update_id') or 0)}",
        ):
            return
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        if command in {"/start", "/help"} or text == "帮助":
            await self._send(
                chat_id,
                "<b>MediaClaw 会员机器人</b>\n"
                "/search 片名 - 搜索影视\n"
                "/request 片名 - 搜索后选择订阅\n"
                "/my - 查看我的订阅\n"
                "/account - 查看会员信息\n"
                "也可以直接发送 ED2K 或 115 分享链接。",
            )
        elif command in {"/search", "/request"}:
            if not argument.strip():
                await self._send(chat_id, "请在命令后填写片名，例如：<code>/search 沙丘</code>")
            else:
                await self._search(chat_id, member, argument.strip())
        elif command == "/my" or text == "我的订阅":
            await self._subscriptions(chat_id, member)
        elif command == "/account" or text == "会员信息":
            await self._send(
                chat_id,
                "<b>会员信息</b>\n"
                f"账号：{html.escape(member['username'])}\n"
                f"昵称：{html.escape(member.get('nickname') or '-')}\n"
                f"订阅权限：{'已开启' if member.get('allow_subscribe') else '未开启'}",
            )
        elif text == "搜索影视":
            await self._send(chat_id, "请发送 <code>/search 片名</code>，例如：<code>/search 星际穿越</code>")
        elif text:
            await self._search(chat_id, member, text)

    async def _dispatch_resource(
        self, chat_id: str, text: str, *, source_ref: str
    ) -> bool:
        """把疑似资源交给统一接收器；普通影视名称继续走原搜索流程。"""
        if self.host is None:
            return False
        looks_like_resource = "ed2k://" in text.lower() or any(
            marker in text.lower()
            for marker in ("115.com/", "115cdn.com/", "anxia.com/", "115://")
        )
        try:
            result = await self.host.netdisk.dispatch_resource(
                text,
                source="telegram",
                source_ref=source_ref,
            )
        except RuntimeError as exc:
            if not looks_like_resource:
                return False
            await self._send(chat_id, f"资源接收不可用：{html.escape(str(exc))}")
            return True
        except Exception as exc:  # noqa: BLE001 -- 资源错误需要回传给当前成员
            await self._send(chat_id, f"资源接收失败：{html.escape(str(exc))}")
            return True
        if not result.get("accepted"):
            return False
        lines = ["<b>资源接收结果</b>"]
        labels = {"offline": "离线下载", "transfer": "分享转存"}
        for item in result.get("items") or []:
            label = labels.get(item.get("type"), "资源")
            lines.append(
                f"• {label}：{html.escape(str(item.get('message') or item.get('status') or '-'))}"
            )
        await self._send(chat_id, "\n".join(lines))
        return True

    async def _handle_callback(self, callback: dict) -> None:
        sender = callback.get("from") or {}
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        member = await self._member_for(str(sender.get("id") or ""))
        if member is None or not data.startswith("sub|"):
            await self._answer_callback(callback_id, "绑定失效或操作无效")
            return
        try:
            result = await self.host.members.create_subscription(member["id"], data[4:])
        except Exception as exc:  # noqa: BLE001 -- 权限和业务错误需原样反馈给成员
            await self._answer_callback(callback_id, f"订阅失败：{str(exc)[:120]}", alert=True)
            return
        await self._answer_callback(callback_id, "已加入订阅")
        await self._send(
            str(chat.get("id") or sender.get("id")),
            f"已加入订阅：<b>{html.escape(result['title'])}</b>",
        )

    async def _search(self, chat_id: str, member: dict, query: str) -> None:
        if self._limited(str(member["id"]), "search", 12, 60):
            await self._send(chat_id, "搜索过于频繁，请稍后再试。")
            return
        try:
            limit = self._bounded_int(self._config().get("search_limit"), 5, 1, 8)
            rows = await self.host.members.search_titles(member["id"], query, limit=limit)
        except Exception as exc:  # noqa: BLE001 -- 权限和 TMDB 错误需转成聊天提示
            await self._send(chat_id, f"搜索失败：{html.escape(str(exc))}")
            return
        if not rows:
            await self._send(chat_id, "没有找到相关影视，请换一个关键词。")
            return
        for row in rows:
            title = html.escape(str(row.get("title") or "未知标题"))
            year = f" ({row['year']})" if row.get("year") else ""
            kind = "电影" if row.get("kind") == "movie" else "电视剧"
            rating = f" · {row['rating']:.1f}分" if isinstance(row.get("rating"), (int, float)) else ""
            await self._send(
                chat_id,
                f"<b>{title}{year}</b>\n{kind}{rating}",
                reply_markup={
                    "inline_keyboard": [[{"text": "加入订阅", "callback_data": f"sub|{row['title_ref']}"}]]
                },
            )

    async def _subscriptions(self, chat_id: str, member: dict) -> None:
        try:
            rows = await self.host.members.list_subscriptions(member["id"], limit=12)
        except Exception as exc:  # noqa: BLE001 -- 查询失败需转成聊天提示
            await self._send(chat_id, f"读取订阅失败：{html.escape(str(exc))}")
            return
        if not rows:
            await self._send(chat_id, "你还没有创建或关注订阅。")
            return
        lines = ["<b>我的订阅</b>"]
        for row in rows:
            lines.append(
                f"• {html.escape(row['title'])} · {row['imported']}/{row['total']} 已入库"
            )
        await self._send(chat_id, "\n".join(lines))

    async def _member_for(self, telegram_user_id: str) -> dict | None:
        username = self._bindings().get(telegram_user_id)
        if not username or self.host is None:
            return None
        return await self.host.members.get_by_username(username)

    def _bindings(self) -> dict[str, str]:
        raw = str(self._config().get("member_bindings") or "")
        result: dict[str, str] = {}
        for item in raw.replace("\n", ",").replace(";", ",").split(","):
            user_id, separator, username = item.strip().partition("=")
            if separator and user_id.isdigit() and username.strip():
                result[user_id] = username.strip()
        return result

    async def _send(self, chat_id: str, text: str, *, reply_markup: dict | None = None) -> None:
        await self._telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": reply_markup or _MENU,
            },
        )

    async def _answer_callback(self, callback_id: str, text: str, *, alert: bool = False) -> None:
        if callback_id:
            await self._telegram(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": text[:180], "show_alert": alert},
            )

    async def _telegram(self, method: str, payload: dict | None = None):
        if self.host is None:
            raise RuntimeError("插件宿主尚未初始化")
        token = str(self._config().get("bot_token") or "").strip()
        if not token:
            raise ValueError("请配置 Telegram Bot Token")
        response = await self.host.http.post(
            f"https://api.telegram.org/bot{token}/{method}", json=payload or {}
        )
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Telegram 返回了无法解析的响应（HTTP {response.status_code}）") from exc
        if not response.is_success or not result.get("ok"):
            raise RuntimeError(str(result.get("description") or f"Telegram HTTP {response.status_code}"))
        return result.get("result")

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

    def _limited(self, member_id: str, action: str, limit: int, seconds: int) -> bool:
        key = f"{member_id}:{action}"
        now = time.monotonic()
        recent = [value for value in self._rate_limits.get(key, []) if now - value < seconds]
        if len(recent) >= limit:
            return True
        recent.append(now)
        self._rate_limits[key] = recent
        return False

    @staticmethod
    def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")


plugin = MemberTelegramBot
