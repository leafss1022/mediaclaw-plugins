"""媒体库封面生成插件。

插件保留 MoviePilot 封面插件的使用思路，但不直接访问宿主数据库或媒体服务器：
媒体库列表、最近入库海报选择和图片渲染都由 Mediaclaw 的 library gate 提供。
插件只负责配置、定时触发和历史记录展示，便于后续替换封面风格而不影响宿主。
"""

from __future__ import annotations

import datetime

from mediaclaw_plugins.sdk import PluginBase


STYLE_LABELS = {
    "static_1": "风格 1 · 经典拼贴",
    "static_2": "风格 2 · 斜切层叠",
    "static_3": "风格 3 · 多图矩阵",
    "static_4": "风格 4 · 玻璃标题",
}

ANIMATION_FORMATS = {"gif", "webp"}

STYLE_PREVIEWS = {
    "static_1": "/plugin-assets/media-cover-generator/style_1.jpeg",
    "static_2": "/plugin-assets/media-cover-generator/style_2.jpeg",
    "static_3": "/plugin-assets/media-cover-generator/style_3.jpeg",
    "static_4": "/plugin-assets/media-cover-generator/style_4.jpeg",
}


class MediaCoverGenerator(PluginBase):
    """按媒体库生成静态横向封面，并在插件页面展示最近生成结果。"""

    def on_enable(self) -> None:
        if self.host is None:
            return
        config = self.host.config.get()
        cron = str(config.get("cron") or "0 6 * * *").strip() or "0 6 * * *"
        self.host.scheduler.register(
            "media-cover-refresh",
            self.refresh,
            title="媒体库封面刷新",
            trigger_type="cron",
            cron=cron,
        )
        self.host.logger.info("媒体库封面生成已启用，执行周期：%s", cron)

    async def refresh(self) -> None:
        """按配置刷新一个或全部媒体库，并持久化生成结果。"""
        if self.host is None:
            return

        config = self.host.config.get()
        include_libraries = self._normalise_library_ids(config.get("include_libraries"))
        try:
            library_id = int(config.get("library_id") or 0)
        except (TypeError, ValueError):
            library_id = 0
        if not include_libraries and library_id:
            include_libraries = {library_id}
        style = self._configured_style(config)

        try:
            libraries = await self.host.library.list_libraries()
        except Exception as exc:
            self.host.logger.error("读取媒体库列表失败：%s", exc)
            return

        selected = [
            library
            for library in libraries
            if not include_libraries or int(library.get("id") or 0) in include_libraries
        ]
        if include_libraries and not selected:
            self.host.logger.warning("未找到已选择的媒体库：%s", sorted(include_libraries))
            return

        history = self.host.data.read_json("history") or []
        if not isinstance(history, list):
            history = []
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        generated_count = 0
        skipped_count = 0

        for library in selected:
            current_id = int(library["id"])
            try:
                result = await self.host.library.generate_library_cover(
                    current_id, style=style, apply=True
                )
            except Exception as exc:
                self.host.logger.error(
                    "生成媒体库封面失败（%s）：%s", library.get("name") or current_id, exc
                )
                skipped_count += 1
                continue
            if result is None:
                self.host.logger.info(
                    "媒体库没有可用本地海报，跳过：%s", library.get("name") or current_id
                )
                skipped_count += 1
                continue

            unique = f"{self.plugin_id}:{current_id}:{style}"
            item = {
                "unique": unique,
                "title": library.get("name") or f"媒体库 {current_id}",
                "library_id": current_id,
                "style": style,
                "style_label": self._style_label(style),
                "type": "媒体库",
                "time": now,
                "poster_url": result.get("cover_url"),
                "genres": [library.get("kind") or "library"],
                "item_count": int(library.get("item_count") or 0),
                "key": result.get("key"),
            }
            previous = next((row for row in history if row.get("unique") == unique), None)
            if previous is None:
                history.append(item)
            else:
                previous.update(item)
            generated_count += 1

        history_limit = self._positive_int(config.get("covers_page_history_limit"), 50)
        self.host.data.write_json("history", history[-history_limit:])
        self.host.data.write_json(
            "summary",
            {
                "last_time": now,
                "style": style,
                "library_id": library_id,
                "include_libraries": sorted(include_libraries),
                "selected_count": len(selected),
                "generated_count": generated_count,
                "skipped_count": skipped_count,
            },
        )
        self.host.logger.info(
            "媒体库封面刷新完成：处理 %d 个媒体库，生成 %d 个，跳过 %d 个",
            len(selected),
            generated_count,
            skipped_count,
        )

    def page(self) -> dict | None:
        """返回 MoviePilot 风格的封面控制台页面，由前端安全渲染。"""
        if self.host is None:
            return None
        config = self.host.config.get()
        history = self.host.data.read_json("history") or []
        summary = self.host.data.read_json("summary") or {}
        if not isinstance(history, list):
            history = []
        if not isinstance(summary, dict):
            summary = {}

        style = self._configured_style(config, fallback=str(summary.get("style") or "static_1"))
        cron = str(config.get("cron") or "0 6 * * *")
        try:
            library_id = int(config.get("library_id") or 0)
        except (TypeError, ValueError):
            library_id = 0

        recent = list(reversed(history[-12:]))
        return {
            "elements": [
                self._hero_alert(),
                self._summary_row(
                    style=style,
                    cron=cron,
                    library_id=library_id,
                    history_count=len(history),
                    summary=summary,
                ),
                self._section_title(
                    "封面风格",
                    "参考 MoviePilot 的静态风格卡片；实际生成使用 Mediaclaw 最近入库的本地海报。",
                ),
                {"component": "VRow", "content": self._style_cards(self._base_style(style))},
                self._section_title("最近生成", "只展示最近 12 张，避免打开插件页时加载过慢。"),
                self._history_grid(recent),
            ]
        }

    def delete_page_item(self, key: str) -> bool:
        """删除一个媒体库封面历史卡片。"""
        if self.host is None:
            return False
        history = self.host.data.read_json("history") or []
        if not isinstance(history, list):
            return False
        kept = [item for item in history if item.get("unique") != key]
        if len(kept) == len(history):
            return False
        self.host.data.write_json("history", kept)
        return True

    @staticmethod
    def _normalise_library_ids(value) -> set[int]:
        """把 MP 多选媒体库配置规范成整数集合，兼容字符串、数字和空值。"""
        if value in (None, ""):
            return set()
        raw_items = value if isinstance(value, list) else [value]
        result: set[int] = set()
        for item in raw_items:
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                result.add(parsed)
        return result

    @staticmethod
    def _positive_int(value, default: int) -> int:
        """读取正整数配置，非法值回退到默认值，避免用户输入导致任务中断。"""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default
    @classmethod
    def _configured_style(cls, config: dict, fallback: str = "static_1") -> str:
        """把“静态/动态 + 四种基础风格 + 输出格式”合成宿主可生成的样式名。"""
        base = cls._normalise_style(
            str(config.get("cover_style") or config.get("cover_style_base") or config.get("style") or fallback)
        )
        if str(config.get("cover_style_variant") or "static") != "animated":
            return base
        fmt = str(config.get("animation_format") or "gif").lower()
        if fmt not in ANIMATION_FORMATS:
            fmt = "gif"
        return f"animated_{base.rsplit('_', 1)[-1]}_{fmt}"

    @staticmethod
    def _normalise_style(style: str) -> str:
        """限制封面样式，避免页面和任务写入未知风格。"""
        return style if style in STYLE_LABELS else "static_1"

    @staticmethod
    def _base_style(style: str) -> str:
        if style.startswith("animated_"):
            parts = style.split("_")
            if len(parts) >= 2:
                return f"static_{parts[1]}"
        return style

    @staticmethod
    def _style_label(style: str) -> str:
        if style.startswith("animated_"):
            parts = style.split("_")
            if len(parts) == 3:
                base = f"static_{parts[1]}"
                return f"{STYLE_LABELS.get(base, base)} · 动态 {parts[2].upper()}"
        return STYLE_LABELS.get(style, style)

    @staticmethod
    def _hero_alert() -> dict:
        return {
            "component": "VAlert",
            "props": {
                "type": "info",
                "title": "媒体库封面生成",
                "text": "按媒体库最近入库的本地海报生成横向封面。配置保存后可点右上角“立即运行”预览效果。",
            },
        }

    @staticmethod
    def _section_title(title: str, subtitle: str) -> dict:
        return {
            "component": "VCard",
            "props": {"variant": "flat", "title": title, "subtitle": subtitle},
        }

    @classmethod
    def _summary_row(
        cls,
        *,
        style: str,
        cron: str,
        library_id: int,
        history_count: int,
        summary: dict,
    ) -> dict:
        library_label = "全部媒体库" if library_id == 0 else f"媒体库 #{library_id}"
        last_time = summary.get("last_time") or "尚未运行"
        generated = summary.get("generated_count")
        skipped = summary.get("skipped_count")
        result_text = "-" if generated is None else f"生成 {generated} / 跳过 {skipped or 0}"
        cards = [
            ("目标媒体库", library_label, f"历史记录 {history_count} 条"),
            ("当前风格", cls._style_label(style), result_text),
            ("执行周期", cron, f"上次运行：{last_time}"),
        ]
        return {
            "component": "VRow",
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 4},
                    "content": [
                        {
                            "component": "VCard",
                            "props": {"variant": "flat"},
                            "content": [
                                {
                                    "component": "VCardText",
                                    "content": [
                                        {"component": "VLabel", "text": label},
                                        {"component": "VCardTitle", "text": value},
                                        {"component": "VText", "text": hint},
                                    ],
                                }
                            ],
                        }
                    ],
                }
                for label, value, hint in cards
            ],
        }

    @staticmethod
    def _style_cards(selected_style: str) -> list[dict]:
        cards = []
        for style, label in STYLE_LABELS.items():
            selected = style == selected_style
            cards.append(
                {
                    "component": "VCol",
                    "props": {"cols": 12, "sm": 6, "md": 3},
                    "content": [
                        {
                            "component": "VCard",
                            "props": {"variant": "flat"},
                            "content": [
                                {
                                    "component": "VImg",
                                    "props": {
                                        "src": STYLE_PREVIEWS[style],
                                        "aspect-ratio": "16/9",
                                        "cover": True,
                                        "alt": label,
                                    },
                                },
                                {
                                    "component": "VCardText",
                                    "content": [
                                        {"component": "VCardTitle", "text": label},
                                        {
                                            "component": "VChip",
                                            "props": {
                                                "text": "当前使用" if selected else "可在配置中选择",
                                                "color": "success" if selected else "default",
                                            },
                                        },
                                    ],
                                },
                            ],
                        }
                    ],
                }
            )
        return cards

    @staticmethod
    def _history_grid(items: list[dict]) -> dict:
        if not items:
            return {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "title": "暂无生成记录",
                    "text": "本地还没有可展示的媒体库封面。确认媒体库已入库并有本地海报后，点击“立即运行”。",
                },
            }
        return {
            "component": "VRow",
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 6},
                    "content": [
                        {
                            "component": "VCard",
                            "props": {"variant": "flat"},
                            "content": [
                                {
                                    "component": "VImg",
                                    "props": {
                                        "src": item.get("poster_url"),
                                        "aspect-ratio": "21/10",
                                        "cover": True,
                                        "alt": item.get("title"),
                                    },
                                },
                                {
                                    "component": "VCardText",
                                    "content": [
                                        {"component": "VCardTitle", "text": item.get("title")},
                                        {
                                            "component": "VText",
                                            "text": f"{item.get('style_label') or self._style_label(str(item.get('style') or ''))} · {item.get('time') or '-'}",
                                        },
                                        {
                                            "component": "VChip",
                                            "props": {
                                                "text": f"媒体库 #{item.get('library_id')} · {item.get('item_count', 0)} 部",
                                            },
                                        },
                                    ],
                                },
                            ],
                        }
                    ],
                }
                for item in items
                if item.get("poster_url")
            ],
        }


plugin = MediaCoverGenerator()
