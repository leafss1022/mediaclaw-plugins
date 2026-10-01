"""内置媒体库封面插件：通过宿主门面生成并应用封面，不连接外部媒体服务器。"""

from __future__ import annotations

import datetime

from mediaclaw_plugins.sdk import PluginBase

STYLE_LABELS = {
    "static_1": "标题与单图",
    "static_2": "斜切背景",
    "static_3": "多图海报墙",
    "static_4": "氛围标题",
}


class MediaCoverGenerator(PluginBase):
    """配置和记录留在插件，素材选择、渲染、封面持久化复用内置媒体库。"""

    def __init__(self) -> None:
        super().__init__()
        self._running = False

    def on_enable(self) -> None:
        if self.host is None:
            return
        # 调度配置由宿主「计划任务」统一管理；兼容旧版本第一次注册时的 cron。
        cron = str(self.host.config.get().get("cron") or "0 6 * * *").strip()
        self.host.scheduler.register(
            "media-cover-refresh",
            self.refresh,
            title="内置媒体库封面刷新",
            trigger_type="cron",
            cron=cron,
        )
        self.host.logger.info("内置媒体库封面生成已启用，可在计划任务中调整周期")

    @staticmethod
    def _library_ids(config: dict) -> set[int]:
        """兼容旧 ID 字段和多选列表；非法值不能被误当作「全部媒体库」。"""
        value = config.get("include_libraries")
        if value is None and config.get("library_id"):
            value = [config["library_id"]]
        if isinstance(value, str):
            value = value.replace("，", ",").split(",")
        ids = set()
        for item in value or []:
            if not str(item).strip():
                continue
            number = int(str(item).strip())
            if number <= 0:
                raise ValueError("请选择有效的内置媒体库")
            ids.add(number)
        return ids

    async def refresh(self) -> None:
        """串行处理所选库；逐库记录跳过/失败，失败不再伪装成运行成功。"""
        if self.host is None:
            return
        if self._running:
            raise RuntimeError("封面生成正在进行，请稍后再试")
        self._running = True
        try:
            await self._refresh()
        finally:
            self._running = False

    async def _refresh(self) -> None:
        config = self.host.config.get()
        ids = self._library_ids(config)
        style = (
            config.get("cover_style")
            or config.get("cover_style_base")
            or config.get("style")
            or "static_1"
        )
        if style not in STYLE_LABELS:
            raise ValueError("请选择支持的静态封面风格")
        generate = getattr(self.host.library, "generate_library_cover", None)
        if generate is None:
            raise RuntimeError(
                "当前主程序缺少内置媒体库封面生成接口，请先更新主程序后再运行插件"
            )
        libraries = await self.host.library.list_libraries()
        missing = ids - {int(row["id"]) for row in libraries}
        if missing:
            raise ValueError(f"所选内置媒体库已不存在，请重新选择：{sorted(missing)}")
        selected = [row for row in libraries if not ids or int(row["id"]) in ids]
        if not selected:
            raise ValueError("尚未创建内置媒体库，请先添加媒体库并刮削海报")
        history = self.host.data.read_json("history") or []
        history = (
            [row for row in history if isinstance(row, dict)]
            if isinstance(history, list)
            else []
        )
        now = datetime.datetime.now(datetime.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        summary = {
            "last_time": now,
            "selected_count": len(selected),
            "generated_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
        }
        for library in selected:
            library_id = int(library["id"])
            record = {
                "unique": f"{self.plugin_id}:{library_id}",
                "title": library["name"],
                "library_id": library_id,
                "style_label": STYLE_LABELS[style],
                "time": now,
            }
            try:
                result = await generate(
                    library_id,
                    style=style,
                    apply=True,
                    replace_custom=bool(config.get("replace_custom", False)),
                )
                if result and result.get("applied"):
                    record.update(
                        status="已应用",
                        poster_url=result["cover_url"],
                        message="已更新内置媒体库封面",
                    )
                    summary["generated_count"] += 1
                else:
                    reason = (result or {}).get(
                        "reason"
                    ) or "没有可用的本地海报，请先完成媒体库刮削"
                    record.update(status="已跳过", message=reason)
                    summary["skipped_count"] += 1
                self.host.logger.info("%s：%s", library["name"], record["message"])
            except Exception as exc:  # noqa: BLE001 — 单库失败需记录并继续其余库。
                record.update(status="失败", message=str(exc))
                summary["failed_count"] += 1
                self.host.logger.error(
                    "媒体库「%s」封面生成失败：%s", library["name"], exc
                )
            history = [row for row in history if row.get("unique") != record["unique"]]
            history.append(record)
        limit = max(1, min(200, int(config.get("covers_page_history_limit") or 50)))
        self.host.data.write_json("history", history[-limit:])
        self.host.data.write_json("summary", summary)
        self.host.logger.info(
            "封面生成结束：成功 %d，跳过 %d，失败 %d",
            summary["generated_count"],
            summary["skipped_count"],
            summary["failed_count"],
        )
        if summary["failed_count"]:
            raise RuntimeError(
                f"{summary['failed_count']} 个媒体库生成失败，请查看插件生成记录或日志"
            )

    def page(self) -> dict | None:
        """横向封面与逐库状态一起展示，跳过或失败也有明确原因。"""
        if self.host is None:
            return None
        history = self.host.data.read_json("history") or []
        summary = self.host.data.read_json("summary") or {}
        elements = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "title": "内置媒体库封面生成",
                    "text": "从库内已刮削的本地海报生成封面并直接应用。无需外部服务器地址或 API Key；在计划任务中调整刷新周期。",
                },
            }
        ]
        config = self.host.config.get()
        style = config.get("cover_style") or config.get("cover_style_base") or config.get("style") or "static_1"
        # 与主程序配置页共用本地示意图，避免旧插件资源路径失效或依赖外网。
        elements.append({
            "component": "VRow",
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 12, "sm": 6, "md": 3},
                    "content": [{
                        "component": "VCard",
                        "content": [
                            {
                                "component": "VImg",
                                "props": {
                                    "src": f"/images/library-covers/{key}.webp",
                                    "aspect-ratio": "21/10",
                                    "cover": True,
                                    "alt": f"{label}排版示意",
                                },
                            },
                            {"component": "VCardTitle", "text": label},
                            {
                                "component": "VCardText",
                                "text": "当前使用" if key == style else "可在配置中选择",
                            },
                        ],
                    }],
                }
                for key, label in STYLE_LABELS.items()
            ],
        })
        elements.append({
            "component": "VAlert",
            "props": {"type": "info", "text": "以上为排版示意，实际封面使用所选媒体库的名称和本地海报。"},
        })
        if isinstance(summary, dict) and summary:
            elements.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning" if summary.get("failed_count") else "info",
                        "title": f"最近运行：{summary.get('last_time', '-')}",
                        "text": f"已应用 {summary.get('generated_count', 0)} · 跳过 {summary.get('skipped_count', 0)} · 失败 {summary.get('failed_count', 0)}",
                    },
                }
            )
        cards = []
        for item in reversed(history[-12:] if isinstance(history, list) else []):
            if not isinstance(item, dict):
                continue
            content = []
            if item.get("poster_url"):
                content.append(
                    {
                        "component": "VImg",
                        "props": {
                            "src": item["poster_url"],
                            "aspect-ratio": "21/10",
                            "cover": True,
                            "alt": item.get("title", "媒体库封面"),
                        },
                    }
                )
            content.append(
                {
                    "component": "VCardText",
                    "text": f"{item.get('title', '')} · {item.get('status', '已应用')}\n{item.get('style_label', '')} · {item.get('time', '')}\n{item.get('message', '')}",
                }
            )
            cards.append(
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 6},
                    "content": [{"component": "VCard", "content": content}],
                }
            )
        elements.append(
            {"component": "VRow", "content": cards}
            if cards
            else {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "text": "暂无生成记录。保存配置后点击「立即运行」，生成结果将直接应用到内置媒体库。",
                },
            }
        )
        return {"elements": elements}

    def delete_page_item(self, key: str) -> bool:
        if self.host is None:
            return False
        history = self.host.data.read_json("history") or []
        if not isinstance(history, list):
            return False
        kept = [row for row in history if row.get("unique") != key]
        self.host.data.write_json("history", kept)
        return len(kept) != len(history)

    def clear_page(self) -> bool:
        """清理插件记录不删除已应用的媒体库封面。"""
        if self.host is None:
            return False
        self.host.data.write_json("history", [])
        self.host.data.write_json("summary", {})
        return True


plugin = MediaCoverGenerator()
