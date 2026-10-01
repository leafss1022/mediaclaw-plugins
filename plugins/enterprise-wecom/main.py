"""企业微信通知与控制台：参考 DaquanClaw 的菜单、卡片及审计交互，业务走宿主 SDK。

回调只做校验和持久化，后台串行消费。重复投递不重复执行业务；通知及命令
记录均脱敏且有数量上限，停用后保留收件箱供下次启用恢复。
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from mediaclaw_plugins.sdk import (
    PluginBase,
    PluginCallbackRequest,
    PluginCallbackResponse,
)

_MENU = {
    "button": [
        {
            "name": "控制中心",
            "sub_button": [
                {"type": "click", "name": label, "key": "mc:" + key}
                for key, label in [
                    ("menu", "功能首页"),
                    ("status", "系统状态"),
                    ("tasks", "任务中心"),
                    ("libraries", "媒体库"),
                ]
            ],
        },
        {
            "name": "影视服务",
            "sub_button": [
                {"type": "click", "name": label, "key": "mc:" + key}
                for key, label in [
                    ("search", "搜索影视"),
                    ("subscriptions", "我的订阅"),
                ]
            ],
        },
        {
            "name": "更多功能",
            "sub_button": [
                {"type": "click", "name": label, "key": "mc:" + key}
                for key, label in [("notifications", "通知状态"), ("help", "使用帮助")]
            ],
        },
    ]
}
_STATUS = {
    "queued": "排队中",
    "running": "执行中",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "waiting": "等待中",
    "pending": "待执行",
    "success": "成功",
}
_EVENT_LABELS = {
    "subscription.download_started": "开始下载 ⬇️",
    "subscription.fulfilled": "已入库 ✅",
    "playback.started": "开始播放 ▶️",
    "playback.stopped": "停止播放 ⏹️",
    "playback.completed": "播放完成 ✅",
    "playback.progress": "播放进度",
    "playback.marked_played": "标记已看",
    "playback.marked_unplayed": "取消已看",
    "item.favorited": "已收藏 ⭐",
    "item.unfavorited": "取消收藏",
}


class EnterpriseWeCom(PluginBase):
    """管理员白名单是控制边界，成员绑定继续受宿主搜索和订阅权限约束。"""

    def __init__(self):
        super().__init__()
        self._token = ""
        self._token_key = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._menu_lock = asyncio.Lock()
        self._tasks = set()
        self._worker = None
        self._stopped = False
        self._choices = {}
        self._delivery = None

    def validate_config(self, config):
        self._validate_connection_config(config)
        if config.get("control_enabled"):
            self._validate_callback_config(config)
        self._base_url(config)
        if config.get("public_base_url"):
            parsed = urlparse(str(config["public_base_url"]))
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "外部访问地址应为完整的 http(s) 地址，不包含账号或查询参数"
                )
        users = self._users(config.get("admin_users"))
        if any(not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", user) for user in users):
            raise ValueError("管理员白名单包含无效 UserID")
        self._bindings(config)

    def on_enable(self):
        self.validate_config(self._config())
        self._stopped = False
        self._load_delivery()
        self._start_worker()
        self._start_menu_sync()

    def on_disable(self):
        self._stopped = True
        self._choices.clear()
        for task in list(self._tasks):
            task.cancel()

    def on_config_changed(self):
        self._choices.clear()
        self._write_state(status="待测试", last_error="")
        self._start_menu_sync()

    def _spawn(self, factory):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        if self._stopped:
            return None
        task = loop.create_task(factory())
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._write_state(last_error=self._safe_error(task.exception()))
            self.host.logger.error(
                "企业微信后台处理失败：%s", self._safe_error(task.exception())
            )

    def _start_menu_sync(self):
        if self._config().get("control_enabled") or self._state().get(
            "menu_fingerprint"
        ):
            self._spawn(self._background_menu)

    async def _background_menu(self):
        try:
            await self._sync_menu()
        except Exception as exc:  # noqa: BLE001 -- 后台边界留痕，不吞任务异常。
            self._write_state(menu_status="同步失败", last_error=self._safe_error(exc))
            self._record("菜单", "系统", "同步菜单", "失败", self._safe_error(exc))

    async def run(self):
        # 兼容旧的「立即运行」入口；测试消息与菜单同步改为独立操作。
        await self.run_action("test_connection")

    async def run_action(self, action):
        if action not in {"test_connection", "sync_menu"}:
            raise ValueError("不支持的企业微信操作")
        try:
            self.validate_config(self._config())
            if action == "sync_menu":
                await self._sync_menu(force=True)
                return
            started = time.monotonic()
            await self._send_visual(
                "MediaClaw 连接测试",
                "企业微信连接测试成功\n请确认已收到本条图文测试消息。",
            )
            self._write_state(
                status="连接正常",
                last_test_at=self._now(),
                last_error="",
                latency_ms=round((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 -- 管理员操作必须留下失败状态。
            error = self._safe_error(exc)
            self._write_state(
                status="连接失败", last_error=error, last_test_at=self._now()
            )
            self._record("测试", "管理员", action, "失败", error)
            raise RuntimeError(error) from None

    async def on_event(self, event, data):
        config = self._config()
        if not config.get("notifications_enabled", True):
            return
        patterns = self._users(config.get("event_patterns", "subscription.*"))
        if not any(fnmatch.fnmatch(event, pattern) for pattern in patterns):
            return
        text = self._event_text(event, data)
        filters = str(config.get("filter_keywords") or "").splitlines()
        if any(
            word.strip() and word.strip().casefold() in text.casefold()
            for word in filters
        ):
            return
        try:
            async with asyncio.timeout(25):
                media = dict(data.get("media") or {})
                media = await self._enrich_media(
                    media,
                    units=data.get("units", [])
                    if event == "subscription.fulfilled"
                    else None,
                )
                title, detail = self._event_visual(event, data, media)
                await self._send_visual(title, detail, media=media)
            self._write_state(last_event=event, last_event_at=self._now())
        except Exception as exc:  # noqa: BLE001 -- 通知失败不能打断业务总线。
            self._write_state(status="发送失败", last_error=self._safe_error(exc))

    def _event_text(self, event, data):
        category = {
            "subscription": "订阅",
            "download": "下载",
            "library": "媒体库",
            "job": "任务",
            "playback": "播放",
        }.get(event.split(".")[0], "系统")
        lines = [f"🔔 MediaClaw · {category}通知", "━━━━━━━━━━━━━━━━", f"事件：{event}"]
        media = data.get("media") if isinstance(data.get("media"), dict) else {}
        if media.get("title"):
            lines.append(
                f"影片：{self._safe_error(media['title'])} ({media.get('year') or '-'})"
            )
        units = data.get("units")
        if isinstance(units, list) and units:
            lines.append(f"涉及集数：{len(units)} 集")
        # 仅取展示字段，避免把下载链接、令牌和原始业务对象发送到外部通道。
        for key, label in [
            ("title", "名称"),
            ("name", "名称"),
            ("status", "状态"),
            ("progress", "进度"),
            ("message", "说明"),
            ("error", "错误"),
        ]:
            value = data.get(key)
            if isinstance(value, (str, int, float)) and value != "":
                lines.append(f"{label}：{self._safe_error(value)}")
        lines.append(f"时间：{self._now()}")
        return "\n".join(lines)

    async def _enrich_media(self, media, *, units=None):
        """补全本地已有档案；补全失败仍发送事件事实，不让图片查询影响业务通知。"""
        summary = getattr(getattr(self.host, "control", None), "media_summary", None)
        if summary and media.get("item_id"):
            try:
                extra = await summary(int(media["item_id"]), units=units)
                return {**media, **{k: v for k, v in extra.items() if v is not None}}
            except Exception as exc:  # noqa: BLE001 -- 元数据非通知必需字段。
                self.host.logger.warning(
                    "企微图文信息补全失败：%s", self._safe_error(exc)
                )
        return media

    @staticmethod
    def _unit_label(units):
        """按真实连续区间展示季集；E15、E17 不得写成 E15-E17。"""
        seasons = {}
        for season, episode in units:
            seasons.setdefault(int(season), set()).add(int(episode))
        labels = []
        for season, episodes in sorted(seasons.items()):
            ordered = sorted(episodes)
            start = end = ordered[0]
            for episode in ordered[1:] + [None]:
                if episode is not None and episode == end + 1:
                    end = episode
                    continue
                labels.append(
                    f"S{season:02}E{start:02}" + (f"-E{end:02}" if end != start else "")
                )
                start = end = episode
        return " / ".join(labels)

    @staticmethod
    def _size(value):
        size = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024

    def _media_title(self, media):
        title = str(media.get("title") or "MediaClaw")
        if media.get("year"):
            title += f" ({media['year']})"
        return self._safe_error(title)

    def _media_description(self, media):
        parts = []
        if media.get("rating") is not None:
            parts.append(f"⭐评分：{media['rating']}")
        kind = media.get("kind") or media.get("type")
        if kind:
            parts.append(
                "🎬类型："
                + {
                    "movie": "电影",
                    "tv": "电视剧",
                    "series": "电视剧",
                    "episode": "电视剧",
                }.get(kind, str(kind))
            )
        if media.get("genres"):
            parts.append("类型标签：" + "、".join(media["genres"]))
        if media.get("libraries"):
            parts.append("📁媒体库：" + "、".join(media["libraries"]))
        if media.get("quality"):
            parts.append("📦质量：" + " / ".join(media["quality"]))
        if media.get("file_count") is not None:
            parts.append(f"📄当前相关库存：{media['file_count']} 个文件")
        if media.get("size_bytes"):
            parts.append("💾大小：" + self._size(media["size_bytes"]))
        return "\n".join(parts)

    def _event_visual(self, event, data, media):
        units = data.get("units") or []
        if not units and media.get("episode_number") is not None:
            units = [[media.get("season_number", 0), media["episode_number"]]]
        label = _EVENT_LABELS.get(event, self._safe_error(event))
        title = f"🎬《{self._media_title(media)}》 {self._unit_label(units)} {label}".strip()
        lines = []
        torrent = data.get("torrent") or {}
        for key, name in [
            ("site_name", "🌐站点"),
            ("spec", "📦质量"),
            ("title", "🧲种子"),
            ("publish_time", "🕒发布时间"),
            ("seeders", "🌱做种"),
            ("promotion", "⚡促销"),
            ("free_deadline", "免费截止"),
            ("subtitle", "📝资源描述"),
        ]:
            value = torrent.get(key)
            if key == "site_name":
                value = value or torrent.get("site_id")
            if value is not None and value != "":
                lines.append(f"{name}：{self._safe_error(value)}")
        if torrent.get("size_bytes"):
            lines.insert(1, "💾大小：" + self._size(torrent["size_bytes"]))
        if torrent.get("hit_and_run") is not None:
            lines.append("H&R：" + ("是" if torrent["hit_and_run"] else "否"))
        if units:
            lines.append(f"涉及 {len(units)} 集")
        lines.append(self._media_description(media))
        playback = data.get("playback") or {}
        if playback.get("duration_ms") and playback.get("position_ms") is not None:
            percent = min(
                100, max(0, playback["position_ms"] / playback["duration_ms"] * 100)
            )
            lines.append(f"播放进度：{percent:.1f}%")
        for key in ("message", "error"):
            if data.get(key):
                lines.append(self._safe_error(data[key]))
        if media.get("overview"):
            lines.append("📝简介：" + self._safe_error(media["overview"]))
        return title, "\n".join(line for line in lines if line)

    @staticmethod
    def _clip_bytes(text, limit):
        """企微 news 标题/摘要限制按 UTF-8 字节计算，截断不拆分汉字。"""
        encoded = str(text).encode("utf-8")
        if len(encoded) <= limit:
            return str(text)
        return encoded[: limit - 3].decode("utf-8", errors="ignore") + "…"

    def _public_url(self, value):
        value = str(value or "").strip()
        if value.startswith("/") and not value.startswith("//"):
            base = str(self._config().get("public_base_url") or "").rstrip("/")
            value = base + value if base else ""
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return ""
        # 图片和详情链接不携带访问凭据，不把播放器签名或站点下载令牌外发。
        if re.search(
            r"(?:token|secret|password|passkey|authorization|api_key)=",
            parsed.query,
            re.IGNORECASE,
        ):
            return ""
        return value

    def _article(self, title, detail, media):
        image = ""
        for field in ("backdrop_url", "backdrop_path", "poster_url", "poster_path"):
            value = media.get(field)
            if (
                field.endswith("_path")
                and value
                and re.fullmatch(r"/[\w.-]+", str(value))
            ):
                value = "https://image.tmdb.org/t/p/w780" + str(value)
            image = self._public_url(value)
            if image:
                break
        image = image or self._public_url("/backdrop-default.jpg")
        url = self._public_url("/library")
        match = re.fullmatch(
            r"tmdb:(movie|tv):(\d+)", str(media.get("title_ref") or "")
        )
        tmdb_id = str(media.get("tmdb_id") or (match[2] if match else ""))
        kind = media.get("kind") or media.get("type") or (match[1] if match else None)
        if tmdb_id.isdigit() and kind in {"movie", "tv", "series", "episode"}:
            # 无公网主程序地址也可以从图文打开公开影视详情，不依赖登录令牌。
            url = (
                url
                or f"https://www.themoviedb.org/{'movie' if kind == 'movie' else 'tv'}/{tmdb_id}"
            )
        if not image or not url:
            return None
        return {
            "title": self._clip_bytes(self._safe_error(title), 128),
            "description": self._clip_bytes(self._safe_error(detail), 512),
            "url": url,
            "picurl": image,
        }

    async def _send_visual(self, title, detail, *, media=None, recipients=None):
        """单条大图文，与截图的横幅通知一致；图片缺失时明确降级，不能丢通知。"""
        article = self._article(title, detail, media or {})
        if article:
            await self._send_message(
                {"msgtype": "news", "news": {"articles": [article]}}, recipients
            )
            self._write_state(last_visual_warning="")
        else:
            self._write_state(
                last_visual_warning="缺少可用图片或详情地址，本次使用文字；请配置手机可访问的外部访问地址"
            )
            await self._send_text(title + "\n" + detail, recipients=recipients)

    def _load_delivery(self):
        if self._delivery is None:
            value = self.host.data.read_json("delivery") or {}
            self._delivery = {
                "seen": value.get("seen", {}),
                "pending": value.get("pending", []),
            }
        return self._delivery

    def _save_delivery(self):
        self.host.data.write_json("delivery", self._delivery)

    async def handle_callback(self, request: PluginCallbackRequest):
        if request.path.strip("/") != "wecom":
            return None
        if len(request.body) > 64 * 1024:
            return PluginCallbackResponse("message too large", status_code=413)
        config = self._config()
        if self._stopped or not config.get("control_enabled"):
            return PluginCallbackResponse("remote control disabled", status_code=403)
        try:
            self._validate_callback_config(config)
            encrypted = (
                request.query.get("echostr", "")
                if request.method == "GET"
                else self._parse_xml(request.body).get("Encrypt", "")
            )
            self._verify_signature(request.query, encrypted, config)
            plain = self._decrypt(encrypted, config)
            if request.method == "GET":
                self._write_state(
                    callback_status="已验证", last_callback_at=self._now()
                )
                return PluginCallbackResponse(plain)
            message = self._parse_xml(plain.encode())
            user = message.get("FromUserName", "")
            if user not in self._users(config.get("admin_users")):
                raise PermissionError("发送用户不在管理员白名单")
            if message.get("AgentID") and message["AgentID"] != str(config["agent_id"]):
                raise PermissionError("消息不属于当前应用")
            if message.get("MsgType", "text") not in {"text", "event"}:
                return PluginCallbackResponse("success")
            if message.get("MsgType") == "event":
                event = message.get("Event", "").lower()
                if event == "enter_agent":
                    message["EventKey"] = "mc:menu"
                elif event not in {"click", "template_card_event"}:
                    return PluginCallbackResponse("success")
            delivery = self._load_delivery()
            now = time.time()
            delivery["seen"] = {
                k: v for k, v in delivery["seen"].items() if now - v < 600
            }
            identity = message.get("MsgId") or "|".join(
                message.get(k, "")
                for k in (
                    "FromUserName",
                    "CreateTime",
                    "Event",
                    "EventKey",
                    "TaskId",
                    "Content",
                )
            )
            key = hashlib.sha256(
                (str(config["corp_id"]) + str(config["agent_id"]) + identity).encode()
            ).hexdigest()
            if key in delivery["seen"]:
                return PluginCallbackResponse("success")
            if len(delivery["pending"]) >= 100 or len(delivery["seen"]) >= 2000:
                return PluginCallbackResponse("busy", status_code=503)
            item = {"key": key, "message": message, "at": now}
            delivery["pending"].append(item)
            delivery["seen"][key] = now
            try:
                self._save_delivery()
            except Exception:  # 持久化失败不能应答成功或留下虚假的去重标记。
                delivery["pending"].remove(item)
                delivery["seen"].pop(key, None)
                raise
            self._start_worker()
            self._write_state(callback_status="接收正常", last_callback_at=self._now())
            return PluginCallbackResponse("success")
        except PermissionError as exc:
            self._record("接收", "未知", "回调", "拒绝", str(exc))
            return PluginCallbackResponse(
                "invalid signature or sender", status_code=403
            )
        except Exception as exc:  # noqa: BLE001 -- 回调边界只返回固定错误，不回显密钥。
            self._write_state(
                callback_status="接收失败", last_error=self._safe_error(exc)
            )
            return PluginCallbackResponse("callback failed", status_code=400)

    def _start_worker(self):
        if self._load_delivery()["pending"] and (
            self._worker is None or self._worker.done()
        ):
            self._worker = self._spawn(self._drain)

    async def _drain(self):
        while self._delivery["pending"] and not self._stopped:
            item = self._delivery["pending"][0]
            message = item["message"]
            user = message.get("FromUserName", "")
            command = message.get("Content") or message.get("EventKey") or "事件"
            try:
                if time.time() - item["at"] > 600:
                    raise ValueError("指令已过期，请重新发送")
                await self._handle_message(message)
                self._record("接收", user, command, "已处理", "")
            except Exception as exc:  # noqa: BLE001 -- 单条失败不阻断后续收件箱。
                self._record("接收", user, command, "失败", self._safe_error(exc))
                if user in self._users(self._config().get("admin_users")):
                    try:
                        await self._send_text(
                            "指令处理异常，请检查订阅或任务状态："
                            + self._safe_error(exc),
                            recipients=[user],
                        )
                    except Exception:  # noqa: BLE001 -- 发送层已记录原因，这里只记录回复未达。
                        self.host.logger.warning("企业微信指令错误提示未送达")
            self._delivery["pending"].pop(0)
            self._save_delivery()
            await asyncio.sleep(0.2)

    def _verify_signature(self, query, encrypted, config):
        timestamp, nonce = query.get("timestamp", ""), query.get("nonce", "")
        try:
            recent = abs(time.time() - int(timestamp)) <= 300
        except (TypeError, ValueError):
            recent = False
        if not recent or not nonce or not encrypted:
            raise PermissionError("回调已过期或缺少验签参数")
        expected = hashlib.sha1(
            "".join(
                sorted([str(config["callback_token"]), timestamp, nonce, encrypted])
            ).encode()
        ).hexdigest()
        if not hmac.compare_digest(str(query.get("msg_signature") or ""), expected):
            raise PermissionError("消息签名不匹配")

    async def _handle_message(self, message):
        user = message.get("FromUserName", "")
        if not self._config().get("control_enabled") or user not in self._users(
            self._config().get("admin_users")
        ):
            raise PermissionError("远程控制未开启或用户不在白名单")
        text = (message.get("Content") or message.get("EventKey") or "").strip()
        text = text.removeprefix("mc:")
        aliases = {
            "帮助": "help",
            "菜单": "menu",
            "/help": "help",
            "mc_help": "help",
            "mc_subscriptions": "subscriptions",
            "我的订阅": "subscriptions",
            "系统状态": "status",
            "任务中心": "tasks",
            "媒体库": "libraries",
            "通知状态": "notifications",
        }
        text = aliases.get(text, text)
        if text in {"menu", "help", "search"}:
            await self._send_card(
                user,
                "MediaClaw 控制中心",
                "发送“搜索 片名”或“订阅 片名”，从候选影片按钮中选择订阅。\n通过菜单查询系统、任务和媒体库；订阅操作按绑定成员权限执行。",
                [
                    ("status", "系统状态"),
                    ("tasks", "任务中心"),
                    ("subscriptions", "我的订阅"),
                ],
            )
            return
        if text == "notifications":
            state = self._state()
            await self._send_visual(
                "MediaClaw 通知状态",
                f"通知状态：{state.get('status', '待测试')}\n回调：{state.get('callback_status', '未验证')}\n菜单：{state.get('menu_status', '待同步')}\n最近错误：{state.get('last_error') or '无'}",
                recipients=[user],
            )
            return
        if text in {"status", "tasks", "libraries"}:
            control = getattr(self.host, "control", None)
            if control is None:
                raise ValueError("主程序尚不支持控制台概览，请更新配套主程序")
            view = await control.overview()
            if text == "status":
                lines = [
                    f"MediaClaw {view['version']}",
                    f"媒体库：{len(view['libraries'])} 个",
                ] + [f"{_STATUS.get(k, k)}：{v}" for k, v in view["jobs"].items()]
            elif text == "tasks":
                lines = ["最近任务"] + [
                    f"{row['title']} · {_STATUS.get(row['status'], row['status'])}"
                    for row in view["recent_jobs"]
                ]
            else:
                lines = ["内置媒体库"] + [
                    f"{row['name']} · {row['count']} 项" for row in view["libraries"]
                ]
            await self._send_visual(
                lines[0], "\n".join(lines[1:]) or "暂无记录", recipients=[user]
            )
            return
        member = await self._member_for(user)
        if member is None:
            raise ValueError(
                f"UserID {user} 尚未绑定 MediaClaw 成员，请在插件中设置成员绑定"
            )
        if text == "subscriptions":
            rows = await self.host.members.list_subscriptions(member["id"], limit=12)
            if not rows:
                await self._send_visual("我的订阅", "你还没有订阅。", recipients=[user])
            for row in rows:
                await self._send_visual(
                    self._media_title(row) + " · 我的订阅",
                    f"入库进度：{row['imported']}/{row['total']}\n"
                    + self._media_description(row),
                    media=row,
                    recipients=[user],
                )
            return
        if text.startswith("sub:"):
            token = text[4:]
            choice = self._choices.get(token)
            if not choice or choice["expires"] < time.time():
                raise ValueError("选片按钮已过期或已使用，请重新搜索")
            if choice["user"] != user or choice["member_id"] != member["id"]:
                raise PermissionError("该选片按钮不属于当前账号")
            # 在任何 await 之前消费一次性选择，双击或并发点击只执行一次。
            self._choices.pop(token)
            result = await self.host.members.create_subscription(
                member["id"], choice["ref"]
            )
            self._record("操作", user, "订阅", "成功", result["title"])
            media = await self._enrich_media({**choice["media"], **result})
            await self._send_visual(
                f"已加入订阅：{result['title']}",
                self._media_description(media)
                + "\n"
                + str(
                    media.get("overview")
                    or "已提交订阅，后续下载和入库将按通知设置推送。"
                ),
                media=media,
                recipients=[user],
            )
            return
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or parts[0] not in {"搜索", "订阅"}:
            raise ValueError("无法识别命令，发送“帮助”查看操作说明")
        query = parts[1].strip()
        if len(query) > 200:
            raise ValueError("搜索词过长，请填写影片名称")
        rows = await self.host.members.search_titles(member["id"], query, limit=5)
        if not rows:
            await self._send_visual("影视搜索", "没有找到相关影视。", recipients=[user])
            return
        self._choices = {
            k: v
            for k, v in self._choices.items()
            if v["expires"] > time.time() and v["user"] != user
        }
        if len(self._choices) + len(rows) > 500:
            raise ValueError("搜索请求较多，请稍后重试")
        buttons, lines = [], []
        for index, row in enumerate(rows, 1):
            token = secrets.token_urlsafe(12)
            self._choices[token] = {
                "user": user,
                "member_id": member["id"],
                "ref": row["title_ref"],
                "media": row,
                "expires": time.time() + 120,
            }
            title = f"{row['title'][:60]} ({row.get('year') or '年份未知'}) · {'电影' if row.get('kind') == 'movie' else '剧集'}"
            lines.append(f"{index}. {title}")
            buttons.append(("sub:" + token, f"订阅 {index}"))
            await self._send_visual(
                f"{index}. {self._media_title(row)}",
                self._media_description(row)
                + "\n📝简介："
                + str(row.get("overview") or "暂无简介")
                + f"\n请点击下方「订阅 {index}」按钮。",
                media=row,
                recipients=[user],
            )
        # 图文发送可能等待限流；按钮有效期从候选展示完毕开始，不能发送时就过期。
        for key, _label in buttons:
            choice = self._choices.get(key.removeprefix("sub:"))
            if choice:
                choice["expires"] = time.time() + 120
        await self._send_card(
            user,
            "请选择要订阅的影片",
            "\n".join(lines) + "\n按钮 2 分钟内有效，点击后按成员权限创建订阅。",
            buttons,
        )

    async def _member_for(self, user):
        name = self._bindings(self._config()).get(user)
        return await self.host.members.get_by_username(name) if name else None

    async def _sync_menu(self, force=False):
        async with self._menu_lock:
            config = self._config()
            self.validate_config(config)
            menu = _MENU if config.get("control_enabled") else None
            fingerprint = hashlib.sha256(
                json.dumps(
                    [
                        config.get(k)
                        for k in (
                            "corp_id",
                            "agent_id",
                            "corp_secret",
                            "access_token",
                            "proxy_url",
                        )
                    ]
                    + [menu],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if not force and fingerprint == self._state().get("menu_fingerprint"):
                return
            endpoint = "menu/create" if menu else "menu/delete"
            await self._api(
                "POST" if menu else "GET",
                endpoint,
                payload=menu,
                params={"agentid": str(config["agent_id"])},
            )
            self._write_state(
                menu_status="已同步" if menu else "已移除",
                menu_fingerprint=fingerprint,
                last_menu_at=self._now(),
            )
            self._record(
                "菜单", "管理员", "同步菜单", "成功", "已同步" if menu else "已移除"
            )

    @staticmethod
    def _base_url(config):
        value = (
            str(config.get("proxy_url") or "https://qyapi.weixin.qq.com")
            .strip()
            .rstrip("/")
        )
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("企业微信代理 URL 必须为不含账号、查询参数的 HTTPS 地址")
        return value + "/cgi-bin"

    async def _access_token(self):
        async with self._token_lock:
            config = self._config()
            manual = str(config.get("access_token") or "").strip()
            if manual:
                return manual
            key = hashlib.sha256(
                json.dumps(
                    [
                        self._base_url(config),
                        config.get("corp_id"),
                        config.get("corp_secret"),
                    ]
                ).encode()
            ).hexdigest()
            if (
                self._token
                and key == self._token_key
                and time.monotonic() < self._token_expires_at
            ):
                return self._token
            response = await self.host.http.get(
                self._base_url(config) + "/gettoken",
                timeout=5.0,
                params={
                    "corpid": config["corp_id"],
                    "corpsecret": config["corp_secret"],
                },
            )
            result = self._json(response)
            if (
                not response.is_success
                or int(result.get("errcode") or 0)
                or not result.get("access_token")
            ):
                raise RuntimeError(self._error(result, response.status_code))
            self._token = str(result["access_token"])
            self._token_key = key
            self._token_expires_at = time.monotonic() + max(
                1, int(result.get("expires_in") or 7200) - 60
            )
            return self._token

    @staticmethod
    def _json(response):
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise TypeError("not object")
            return result
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"企业微信返回无法解析的响应（HTTP {response.status_code}）"
            ) from exc

    async def _api(self, method, endpoint, *, payload=None, params=None):
        refreshed = False
        for attempt in range(3):
            try:
                token = await self._access_token()
                options = {
                    "params": {"access_token": token, **(params or {})},
                    "timeout": 5.0,
                }
                if payload is not None:
                    options["json"] = payload
                response = await getattr(self.host.http, method.lower())(
                    self._base_url(self._config()) + "/" + endpoint, **options
                )
                if response.status_code in {429, 502, 503, 504} and attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                result = self._json(response)
                code = int(result.get("errcode") or 0)
                if response.is_success and code == 0:
                    return result
                if (
                    code in {40014, 42001}
                    and not self._config().get("access_token")
                    and not refreshed
                    and attempt < 2
                ):
                    async with self._token_lock:
                        if self._token == token:
                            self._token = ""
                    refreshed = True
                    continue
                if (
                    code in {-1, 45009, 45011}
                    or response.status_code in {429, 502, 503, 504}
                ) and attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise RuntimeError(self._error(result, response.status_code))
            except httpx.HTTPError:
                if attempt == 2:
                    raise RuntimeError(
                        "连接企业微信超时或网络异常，请检查网络出口及代理配置"
                    ) from None
                await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError("企业微信请求重试次数已用尽")

    async def _send_text(self, text, *, recipients=None):
        parts = list(self._split_text(str(text)))
        for index, part in enumerate(parts, 1):
            # 分段加序号，避免内容相同的两段被企微防重复机制当成重发。
            if len(parts) > 1:
                part = f"[{index}/{len(parts)}]\n" + part
            await self._send_message(
                {"msgtype": "text", "text": {"content": part}}, recipients
            )

    async def _send_card(self, user, title, text, buttons):
        await self._send_message(
            {
                "msgtype": "template_card",
                "template_card": {
                    "card_type": "button_interaction",
                    "source": {"desc": "MediaClaw"},
                    "main_title": {"title": title[:32], "desc": "企业微信控制台"},
                    "sub_title_text": text[:512],
                    "task_id": "mc_" + secrets.token_hex(12),
                    "button_list": [
                        {"text": label[:30], "key": "mc:" + key}
                        for key, label in buttons[:6]
                    ],
                },
            },
            [user],
        )

    async def _send_message(self, message, recipients):
        users = recipients or self._users(self._config().get("admin_users"))
        if not users:
            raise ValueError("请配置至少一个企业微信管理员用户 ID")
        async with self._send_lock:
            try:
                result = await self._api(
                    "POST",
                    "message/send",
                    payload={
                        **message,
                        "touser": "|".join(users),
                        "agentid": int(self._config()["agent_id"]),
                        "safe": 0,
                        "enable_duplicate_check": 1,
                        "duplicate_check_interval": 180,
                    },
                )
                invalid = (
                    result.get("invaliduser")
                    or result.get("invalidparty")
                    or result.get("invalidtag")
                    or result.get("unlicenseduser")
                )
                if invalid:
                    raise RuntimeError(
                        f"部分接收对象无效或不可见：{invalid}；请检查通讯录 UserID 和应用可见范围"
                    )
                self._write_state(
                    status="连接正常", last_send_at=self._now(), last_error=""
                )
                self._record(
                    "发送",
                    "、".join(users),
                    {"template_card": "交互卡片", "news": "图文通知"}.get(
                        message["msgtype"], "文字通知"
                    ),
                    "成功",
                    str(result.get("msgid") or "已提交"),
                )
            except Exception as exc:  # noqa: BLE001 -- 通道错误统一脱敏并更新真实状态。
                error = self._safe_error(exc)
                self._write_state(status="发送失败", last_error=error)
                self._record("发送", "、".join(users), "消息", "失败", error)
                raise RuntimeError(error) from None

    @staticmethod
    def _split_text(text):
        # 企微限制以 UTF-8 字节计；保留每个字符完整，不能按 Python 字符数截断。
        current, size = [], 0
        for char in text:
            width = len(char.encode("utf-8"))
            if size + width > 1950:
                yield "".join(current)
                current, size = [], 0
            current.append(char)
            size += width
        if current:
            yield "".join(current)

    def _error(self, result, status):
        code = int(result.get("errcode") or 0)
        reasons = {
            40014: "访问令牌无效，请检查应用密钥或清空手动 Token",
            42001: "访问令牌已过期，请清空手动 Token 后使用应用密钥",
            60020: "出口 IP 不在企业微信可信 IP 列表，请配置可信 IP 或受信任的 API 转发地址",
            40013: "企业 ID 无效",
            40001: "应用密钥无效",
            81013: "用户不在应用可见范围",
            45009: "接口调用达到频率限制，请稍后再试",
            45011: "接口调用过于频繁，请稍后再试",
        }
        return f"企业微信（{code or status}）：{reasons.get(code) or self._safe_error(result.get('errmsg') or '请求失败')}"

    def _safe_error(self, value):
        if isinstance(value, TimeoutError):
            return "企业微信通知处理超时，请检查网络出口或减少高频通知"
        text = str(value)
        for key in (
            "corp_secret",
            "access_token",
            "callback_token",
            "encoding_aes_key",
        ):
            secret = self._config().get(key)
            if secret:
                text = text.replace(str(secret), "[已隐藏]")
        if self._token:
            text = text.replace(self._token, "[已隐藏]")
        text = re.sub(r"https?://[^\s<>]+", "[链接已隐藏]", text)
        return text[:500]

    @staticmethod
    def _users(value):
        return list(
            dict.fromkeys(
                item
                for item in re.split(r"[\s,，;；|]+", str(value or "").strip())
                if item
            )
        )

    @staticmethod
    def _bindings(config):
        result = {}
        for item in re.split(r"[,，;；\n]+", str(config.get("member_bindings") or "")):
            if not item.strip():
                continue
            user, separator, name = item.strip().partition("=")
            if not separator or not user.strip() or not name.strip():
                raise ValueError(
                    "成员绑定格式应为 UserID=MediaClaw成员登录名，每行一项"
                )
            result[user.strip()] = name.strip()
        return result

    def _record(self, direction, user, command, status, detail):
        if self.host is None:
            return
        rows = self.host.data.read_json("history") or []
        rows.append(
            {
                "time": self._now(),
                "direction": direction,
                "user": user[:160],
                "command": self._safe_error(command)[:120],
                "status": status,
                "detail": self._safe_error(detail),
            }
        )
        self.host.data.write_json("history", rows[-100:])
        if status in {"失败", "拒绝"}:
            self.host.logger.warning(
                "企业微信%s：%s，%s", direction, status, self._safe_error(detail)
            )

    def page(self):
        state = self._state()
        base = str(self._config().get("public_base_url") or "").rstrip("/")
        elements = [
            {
                "component": "VAlert",
                "props": {
                    "type": "success"
                    if state.get("status") == "连接正常"
                    else "warning",
                    "title": state.get("status") or "待测试",
                    "text": f"最近测试：{state.get('last_test_at') or '-'} · 耗时：{state.get('latency_ms', '-')}ms",
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "title": "回调与菜单",
                    "text": f"回调：{state.get('callback_status', '未验证')} · 菜单：{state.get('menu_status', '待同步')}\n{base + '/api/v1/plugin-callbacks/enterprise-wecom/wecom' if base else '请配置外部访问地址'}",
                },
            },
        ]
        if state.get("last_visual_warning"):
            elements.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "title": "图文显示提示",
                        "text": state["last_visual_warning"],
                    },
                }
            )
        if state.get("last_error"):
            elements.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "error",
                        "title": "最近错误",
                        "text": state["last_error"],
                    },
                }
            )
        rows = self.host.data.read_json("history") or []
        for row in reversed(rows[-30:]):
            elements.append(
                {
                    "component": "VCard",
                    "content": [
                        {
                            "component": "VCardText",
                            "text": f"{row['time']} · {row['direction']} · {row['status']}\n{row['user']} · {row['command']}\n{row['detail']}",
                        }
                    ],
                }
            )
        if not rows:
            elements.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "text": "暂无收发记录，可在配置页测试连接。",
                    },
                }
            )
        return {"elements": elements}

    def clear_page(self):
        # 审计记录可清理，去重收件箱与配置不能同时被清空。
        self.host.data.write_json("history", [])
        return True

    def _config(self):
        return self.host.config.get() if self.host is not None else {}

    def _state(self):
        value = self.host.data.read_json("state") if self.host else None
        return value if isinstance(value, dict) else {}

    def _write_state(self, **values):
        if self.host:
            state = self._state()
            state.update(values)
            self.host.data.write_json("state", state)

    @staticmethod
    def _now():
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _decrypt(self, encrypted: str, config: dict) -> str:
        try:
            key = base64.b64decode(str(config["encoding_aes_key"]) + "=")
            ciphertext = base64.b64decode(encrypted)
        except (ValueError, TypeError) as exc:
            raise ValueError("企业微信加密消息格式无效") from exc
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        padding = padded[-1]
        if (
            padding < 1
            or padding > 32
            or padded[-padding:] != bytes([padding]) * padding
        ):
            raise ValueError("企业微信消息填充无效")
        plain = padded[:-padding]
        if len(plain) < 20:
            raise ValueError("企业微信消息长度无效")
        length = struct.unpack("!I", plain[16:20])[0]
        if 20 + length > len(plain):
            raise ValueError("企业微信消息长度无效")
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
        if not re.fullmatch(r"[A-Za-z0-9+/]{43}", aes_key):
            raise ValueError("消息加密密钥必须是 43 位 Base64 密钥")


plugin = EnterpriseWeCom
