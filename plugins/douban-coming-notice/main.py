"""豆瓣将映魔改版（移植自 luanyi143/MoviePilot-Plugins）。

流程：RSSHub「豆瓣即将上映」RSS → 想看人数达阈值 → 豆瓣收敛到 TMDB →
距开播 ≤ advance_days 且库中无、未订阅 → 自动建订阅；开播前 notify_hours
内发送一次提醒；历史记录经 host.data 持久化（跨更新保留）。
"""

from __future__ import annotations

import asyncio
import datetime
import re
import xml.etree.ElementTree as ET

from mediaclaw_plugins.sdk import PluginBase

_DOUBAN_ID_RE = re.compile(r"/subject/(\d+)")
_WISH_RE = re.compile(r"想看人数[：:\s]*([\d,]+)")
_YEAR_RE = re.compile(r"\((\d{4})\)")
_SEASON_RE = re.compile(r"(?:第\s*([一二三四五六七八九十百\d]+)\s*季|[Ss](\d{1,2}))")


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(value: str) -> int:
    """中文数字 -> int（支持一到十及十位数）。"""
    value = value.strip()
    if value.isdigit():
        return int(value)
    if value in _CN_NUM:
        return _CN_NUM[value]
    if value.endswith("十"):
        head = value[:-1]
        return 10 * _CN_NUM.get(head, 1) if head else 10
    if "十" in value:
        head, tail = value.split("十", 1)
        return 10 * _CN_NUM.get(head, 1) + _CN_NUM.get(tail, 0)
    return 1


class DoubanComingNotice(PluginBase):
    def on_enable(self) -> None:
        if self.host is None:
            return
        cfg = self.host.config.get()
        cron = str(cfg.get("cron") or "0 8 * * *").strip() or "0 8 * * *"
        self.host.scheduler.register(
            "douban-refresh",
            self.refresh,
            title="豆瓣将映刷新",
            trigger_type="cron",
            cron=cron,
        )
        if cfg.get("only_once"):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.refresh())
                self.host.logger.info("立即运行一次：豆瓣将映刷新已触发")
            except RuntimeError:
                pass

    def page(self) -> dict | None:
        """历史记录卡片墙（原版 get_page 的数据结构）。"""
        if self.host is None:
            return None
        history = self.host.data.read_json("history") or []
        cards = []
        for h in history:
            douban_id = h.get("douban_id")
            cards.append(
                {
                    "unique": h.get("unique"),
                    "title": h.get("title"),
                    "douban_id": douban_id,
                    "douban_url": (
                        f"https://movie.douban.com/subject/{douban_id}/"
                        if douban_id
                        else None
                    ),
                    "type": "电视剧",
                    "time": h.get("time"),
                    "wish_count": h.get("wish_count"),
                    "air_date": h.get("air_date"),
                    "genres": h.get("genres") or [],
                    "poster_url": h.get("poster_url"),
                    "subscribed": bool(h.get("subscribed")),
                    "notified": bool(h.get("air_notify_sent")),
                }
            )
        return {"cards": cards}

    def delete_page_item(self, key: str) -> bool:
        """删除一条历史记录。"""
        if self.host is None:
            return False
        history = self.host.data.read_json("history") or []
        kept = [h for h in history if h.get("unique") != key]
        if len(kept) == len(history):
            return False
        self.host.data.write_json("history", kept)
        return True

    async def refresh(self) -> None:
        if self.host is None:
            return
        cfg = self.host.config.get()
        rsshub = str(cfg.get("rsshub") or "https://rsshub.ddsrem.com").rstrip("/")
        sort_by = cfg.get("sort_by") or "hot"
        count = int(cfg.get("count") or 10)
        threshold = int(cfg.get("wish_count_threshold") or 5000)
        advance_days = int(cfg.get("advance_days") or 7)
        notify_before_air = bool(cfg.get("notify_before_air", True))
        notify_hours = int(cfg.get("notify_hours") or 24)

        try:
            resp = await self.host.http.get(
                f"{rsshub}/douban/tv/coming?sort={sort_by}"
            )
            items = self._parse_rss(resp.text)
        except Exception as exc:
            self.host.logger.error("抓取/解析豆瓣将映 RSS 失败：%s", exc)
            return

        history = self.host.data.read_json("history") or []
        notify_history = self.host.data.read_json("notify_history") or []
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for raw in items[:count]:
            try:
                await self._process(
                    raw,
                    threshold,
                    advance_days,
                    notify_before_air,
                    notify_hours,
                    history,
                    notify_history,
                    now,
                )
            except Exception as exc:
                self.host.logger.warning("处理条目 %s 失败：%s", raw.get("title"), exc)

        self.host.data.write_json("history", history)
        self.host.data.write_json("notify_history", notify_history)
        self.host.logger.info("豆瓣将映刷新完成，历史 %d 条", len(history))

    async def _process(
        self,
        raw: dict,
        threshold: int,
        advance_days: int,
        notify_before_air: bool,
        notify_hours: int,
        history: list,
        notify_history: list,
        now: str,
    ) -> None:
        title = raw.get("title")
        if not title:
            return
        wish_count = int(raw.get("wish_count") or 0)
        if wish_count > 0 and wish_count < threshold:
            self.host.logger.info(
                "%s 想看人数 %s 低于阈值 %s，跳过", title, wish_count, threshold
            )
            return
        media = await self.host.media.resolve_douban(
            title, year=raw.get("year"), douban_id=raw.get("douban_id")
        )
        if not media:
            self.host.logger.info("%s 未收敛到 TMDB，跳过", title)
            return
        poster_url = None
        if media.get("poster_path"):
            poster_url = f"https://image.tmdb.org/t/p/w500{media['poster_path']}"
        season = int(raw.get("season") or 1)
        air_date = await self.host.media.tv_air_date(media["tmdb_id"], season=season)
        days = self._days_until(air_date)

        unique = f"doubancomingnotice: {title} (DB:{raw.get('douban_id')})"
        history_item = next((h for h in history if h.get("unique") == unique), None)
        subscribed = bool(history_item.get("subscribed")) if history_item else False
        air_notified = False

        if (
            not subscribed
            and days is not None
            and 0 <= days <= advance_days
        ):
            in_library = await self.host.library.library_exists(media["tmdb_id"])
            already = await self.host.subscription.subscription_exists(media["tmdb_id"])
            if not in_library and not already:
                try:
                    sub = await self.host.subscription.subscription_create(
                        media["tmdb_id"],
                        season=season,
                        douban_id=raw.get("douban_id"),
                    )
                    subscribed = sub is not None
                    self.host.logger.info("已自动订阅：%s（S%d）", media["title"], season)
                except Exception as exc:
                    self.host.logger.warning("自动订阅失败 %s：%s", title, exc)

        if notify_before_air and days is not None and 0 <= days * 24 <= notify_hours:
            notify_key = f"air_notify:{raw.get('douban_id') or title}:{air_date or 'unknown'}"
            if notify_key not in {n.get("unique") for n in notify_history}:
                text = (
                    f"类型：电视剧\n开播时间：{air_date or '-'}\n"
                    f"想看人数：{wish_count}\n"
                    f"订阅状态：{'已订阅' if subscribed else '未订阅'}\n"
                    f"TMDB ID：{media['tmdb_id']}"
                )
                try:
                    await self.host.notify.notify(f"📺 豆瓣开播提醒：{media['title']}", text)
                    notify_history.append(
                        {"unique": notify_key, "title": media["title"], "notified_at": now}
                    )
                    air_notified = True
                except Exception as exc:
                    self.host.logger.warning("开播提醒发送失败 %s：%s", title, exc)

        entry = {
            "unique": unique,
            "title": media["title"],
            "douban_id": raw.get("douban_id"),
            "wish_count": wish_count,
            "air_date": air_date,
            "genres": media.get("genres") or [],
            "poster_url": poster_url,
            "subscribed": subscribed,
            "air_notify_sent": air_notified,
            "time": now,
        }
        if history_item is None:
            history.append(entry)
        else:
            history_item.update(entry)

    def _parse_rss(self, xml_text: str) -> list[dict]:
        """解析 RSSHub 的豆瓣即将播出 RSS，抽取标题/豆瓣ID/想看人数/年份/季号。

        RSSHub 字段格式不稳定：想看人数可能是「想看人数：」或「想看：」；
        剧集标题多为「剧名 第X季」（中文季），识别时需去掉季后缀。
        """
        items: list[dict] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return items
        for node in root.iter("item"):
            title = node.findtext("title") or ""
            link = node.findtext("link") or node.findtext("guid") or ""
            description = node.findtext("description") or ""
            douban_match = _DOUBAN_ID_RE.search(link)
            wish_match = _WISH_RE.search(description)
            year_match = _YEAR_RE.search(title)
            season_match = _SEASON_RE.search(title)
            wish_count = 0
            if wish_match:
                try:
                    wish_count = int(wish_match.group(1).replace(",", ""))
                except ValueError:
                    wish_count = 0
            season = 1
            if season_match:
                if season_match.group(1):
                    season = _cn_to_int(season_match.group(1))
                elif season_match.group(2):
                    try:
                        season = int(season_match.group(2))
                    except ValueError:
                        season = 1
            # 识别用基础标题：去掉「第X季」/「Sxx」后缀，避免 TMDB 搜索带季名
            base_title = _SEASON_RE.sub("", title).strip()
            items.append(
                {
                    "title": base_title or title.strip(),
                    "original_title": title.strip(),
                    "douban_id": douban_match.group(1) if douban_match else None,
                    "description": description,
                    "wish_count": wish_count,
                    "year": int(year_match.group(1)) if year_match else None,
                    "season": season,
                }
            )
        return items
        for node in root.iter("item"):
            title = node.findtext("title") or ""
            link = node.findtext("link") or node.findtext("guid") or ""
            description = node.findtext("description") or ""
            douban_match = _DOUBAN_ID_RE.search(link)
            wish_match = _WISH_RE.search(description)
            year_match = _YEAR_RE.search(title)
            season_match = _SEASON_RE.search(title)
            wish_count = 0
            if wish_match:
                try:
                    wish_count = int(wish_match.group(1).replace(",", ""))
                except ValueError:
                    wish_count = 0
            items.append(
                {
                    "title": title.strip(),
                    "douban_id": douban_match.group(1) if douban_match else None,
                    "description": description,
                    "wish_count": wish_count,
                    "year": int(year_match.group(1)) if year_match else None,
                    "season": int(season_match.group(1)) if season_match else 1,
                }
            )
        return items

    @staticmethod
    def _days_until(air_date: str | None) -> int | None:
        if not air_date:
            return None
        try:
            target = datetime.date.fromisoformat(air_date)
        except ValueError:
            return None
        return (target - datetime.date.today()).days


plugin = DoubanComingNotice()
