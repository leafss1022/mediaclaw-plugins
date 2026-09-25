"""MediaClaw 115 资源接收插件。

借鉴 DaquanClaw 的链接识别、统一目录和消息幂等策略，但所有 115 请求、
凭据读取、离线 Job 与 STRM 同步都通过 MediaClaw 插件 SDK 完成。
"""

from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import parse_qs, unquote, urlparse

from mediaclaw_plugins.sdk import PluginBase

_TRAILING_PUNCTUATION = re.compile(
    r"[\s\u3000。，、；：！？）》】」』\]}>.,;:!?]+$", re.UNICODE
)
_LINK_RE = re.compile(
    r'(?:ed2k://\|file\|[^\r\n<>"\']*?\|/|115://[^\s<>"\']+'
    r'|https?://[^\s<>"\']+|115(?:cdn)?\.com/s/[A-Za-z0-9_-]+(?:\?[^\s<>"\']*)?)',
    re.IGNORECASE,
)
_PASSWORD_KEYS = (
    "password",
    "receive_code",
    "receiveCode",
    "receive",
    "pwd",
    "passwd",
    "secret",
)


def normalize_115_cid(value: object, *, allow_root: bool = True) -> str:
    """接受纯 CID 或完整 115 目录地址，统一返回数字 CID。"""
    source = str(value or "").strip()
    if not source:
        return ""
    parsed = urlparse(source if "://" in source else f"https://{source}")
    query = parse_qs(parsed.query)
    source = str((query.get("cid") or query.get("parent_id") or [source])[0])
    matched = re.search(
        r"(?:^|[?&#\s])(?:cid|parent_id)=([0-9]+)", source, re.IGNORECASE
    )
    if matched:
        source = matched.group(1)
    normalized = re.sub(r"^cid\s*[:=]\s*", "", source, flags=re.IGNORECASE).strip()
    if not re.fullmatch(r"\d{1,30}", normalized):
        raise ValueError("115 目录 CID 必须是数字，或粘贴包含 cid=数字 的地址")
    if not allow_root and normalized == "0":
        raise ValueError("该目录不能使用 115 根目录 CID 0")
    return normalized


def extract_resource_links(text: str) -> list[str]:
    """从一段消息中提取 ED2K 与 115 分享链接并保持原顺序去重。"""
    source = (
        str(text or "")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
    )
    links: list[str] = []
    seen: set[str] = set()
    for match in _LINK_RE.finditer(source):
        link = _TRAILING_PUNCTUATION.sub("", match.group(0).strip())
        key = link.lower()
        if link and key not in seen and (_is_ed2k(link) or _is_115_link(link)):
            seen.add(key)
            links.append(link)
    return links


def parse_115_share_link(value: str) -> dict | None:
    """解析 115 分享码和提取码；支持上游插件使用的参数别名。"""
    original = _TRAILING_PUNCTUATION.sub("", str(value or "").strip())
    if not _is_115_link(original):
        return None
    normalized = (
        f"https://{original[6:]}" if original.lower().startswith("115://") else original
    )
    if "://" not in normalized:
        normalized = f"https://{normalized}"
    parsed = urlparse(normalized)
    matched = re.search(r"/s/([A-Za-z0-9_-]+)", parsed.path, re.IGNORECASE)
    query = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment)
    share_code = (
        matched.group(1)
        if matched
        else _first_query(
            query, fragment, ("share_code", "sharecode", "shareCode", "code", "share")
        )
    )
    if not share_code:
        return None
    return {
        "share_code": share_code,
        "receive_code": _first_query(query, fragment, _PASSWORD_KEYS),
    }


def _first_query(query: dict, fragment: dict, names: tuple[str, ...]) -> str:
    for name in names:
        values = query.get(name) or fragment.get(name)
        if values and str(values[0]).strip():
            return unquote(str(values[0])).strip()
    return ""


def _is_ed2k(value: str) -> bool:
    return str(value or "").strip().lower().startswith("ed2k://")


def _is_115_link(value: str) -> bool:
    source = str(value or "").strip()
    if source.lower().startswith("115://"):
        return True
    parsed = urlparse(source if "://" in source else f"https://{source}")
    host = (parsed.hostname or "").lower()
    return host in {"115.com", "115cdn.com", "anxia.com"} or host.endswith(
        (".115.com", ".115cdn.com", ".anxia.com")
    )


class One15ResourceReceiver(PluginBase):
    """统一接收资源文本，并分发到现有 115 离线与分享转存服务。"""

    def on_enable(self) -> None:
        if self.host is None:
            return
        self._validated_config()
        self.host.netdisk.register_resource_receiver(self.process_resource)
        self.host.logger.info("115资源接收已启用，统一入口已注册")

    async def run(self) -> None:
        """检查账号、目录和可选同步根配置，不产生真实离线或转存任务。"""
        if self.host is None:
            raise RuntimeError("插件宿主尚未初始化")
        config = self._validated_config()
        accounts = await self.host.netdisk.list_accounts()
        account = next(
            (row for row in accounts if row["id"] == config["account_id"]), None
        )
        if account is None:
            raise ValueError("配置的 115 账号不存在")
        if account.get("status") != "active":
            raise ValueError(
                f"115 账号尚未认证可用：{account.get('last_error') or account.get('status')}"
            )
        if config["sync_root_id"] is not None:
            roots = await self.host.netdisk.list_sync_roots(config["account_id"])
            root = next(
                (row for row in roots if row["id"] == config["sync_root_id"]), None
            )
            if root is None or not root.get("enabled"):
                raise ValueError("配置的 STRM 同步根不存在、已停用或不属于当前账号")
            if root["remote_root_id"] != config["transfer_target_remote_id"]:
                raise ValueError("STRM 同步根远程目录必须与分享转存目录一致")
        self._update_state(
            status="配置正常",
            last_error="",
            last_message=f"账号：{account['name']}；离线目录：{config['offline_target_remote_id']}；转存目录：{config['transfer_target_remote_id']}",
            last_run_at=self._now(),
        )
        self.host.logger.info(
            "115资源接收配置检查通过：account_id=%s", config["account_id"]
        )

    async def process_resource(
        self, text: str, source: str = "plugin", source_ref: str | None = None
    ) -> dict:
        """识别资源并执行；写入幂等标记后才调用 115，避免入口重试造成重复转存。"""
        if self.host is None:
            raise RuntimeError("插件宿主尚未初始化")
        links = extract_resource_links(text)
        if not links:
            return {"accepted": False, "items": []}
        config = self._validated_config()
        items = []
        for link in links:
            kind = "offline" if _is_ed2k(link) else "transfer"
            receive_code = ""
            fingerprint = hashlib.sha256(link.encode("utf-8")).hexdigest()[:16]
            receipt_key = f"{source}:{source_ref}:{fingerprint}" if source_ref else ""
            duplicate = self._receipt(receipt_key) if receipt_key else None
            if duplicate:
                items.append(
                    {
                        "type": kind,
                        "status": "duplicate",
                        "message": "该入口消息已处理，已跳过重复提交",
                    }
                )
                continue
            if receipt_key:
                self._save_receipt(
                    receipt_key, {"status": "processing", "time": self._now()}
                )
            try:
                if kind == "offline":
                    result = await self.host.netdisk.enqueue_offline(
                        config["account_id"], [link], config["offline_target_remote_id"]
                    )
                    message = "离线任务已加入 MediaClaw 任务中心"
                    reference = result["job_id"]
                else:
                    share = parse_115_share_link(link)
                    if not share:
                        raise ValueError("无法识别 115 分享链接")
                    receive_code = share["receive_code"]
                    result = await self.host.netdisk.receive_share(
                        config["account_id"],
                        share["share_code"],
                        share["receive_code"],
                        config["transfer_target_remote_id"],
                        sync_root_id=config["sync_root_id"],
                    )
                    message = result["message"]
                    reference = result.get("sync_job_id") or result.get("remote_id")
                item = {
                    "type": kind,
                    "status": "success",
                    "message": message,
                    "reference": reference,
                }
            except Exception as exc:  # noqa: BLE001 -- 单个链接失败不能阻断同一消息中的其它资源
                error = str(exc)
                if receive_code:
                    error = error.replace(receive_code, "****")
                item = {"type": kind, "status": "failed", "message": error[:300]}
                self.host.logger.error(
                    "115资源处理失败：type=%s source=%s error=%s", kind, source, error
                )
            if receipt_key:
                self._save_receipt(
                    receipt_key,
                    {"status": item["status"], "time": self._now(), "type": kind},
                )
            self._append_history(source, source_ref, item)
            items.append(item)
        failed = [item for item in items if item["status"] == "failed"]
        self._update_state(
            status="处理失败" if failed else "处理完成",
            last_error=failed[0]["message"] if failed else "",
            last_message=f"本次识别 {len(items)} 个资源，失败 {len(failed)} 个",
            last_run_at=self._now(),
        )
        return {"accepted": True, "items": items}

    def page(self) -> dict:
        state = self._state()
        history = state.get("history") if isinstance(state.get("history"), list) else []
        return {
            "elements": [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "error" if state.get("last_error") else "success",
                        "title": state.get("status") or "等待配置",
                        "text": state.get("last_error")
                        or state.get("last_message")
                        or "保存配置并启用后，可接收 ED2K 与 115 分享链接。",
                    },
                },
                {
                    "component": "VDataTable",
                    "props": {
                        "headers": [
                            {"title": "时间", "key": "time"},
                            {"title": "入口", "key": "source"},
                            {"title": "类型", "key": "type"},
                            {"title": "状态", "key": "status"},
                            {"title": "结果", "key": "message"},
                        ],
                        "items": history[:20],
                    },
                },
            ]
        }

    def clear_page(self) -> bool:
        if self.host is None:
            return False
        state = self._state()
        changed = bool(state.get("history") or state.get("receipts"))
        state["history"] = []
        state["receipts"] = {}
        self.host.data.write_json("state", state)
        return changed

    def _validated_config(self) -> dict:
        config = self.host.config.get() if self.host is not None else {}
        try:
            account_id = int(str(config.get("account_id") or "").strip())
        except ValueError as exc:
            raise ValueError("115账号 ID 必须是正整数") from exc
        if account_id <= 0:
            raise ValueError("115账号 ID 必须是正整数")
        offline = normalize_115_cid(config.get("offline_target_remote_id"))
        transfer = normalize_115_cid(config.get("transfer_target_remote_id"))
        if not offline or not transfer:
            raise ValueError("请同时配置离线下载目录和分享转存目录")
        sync_root_id = None
        if bool(config.get("auto_sync_after_transfer")):
            try:
                sync_root_id = int(str(config.get("sync_root_id") or "").strip())
            except ValueError as exc:
                raise ValueError(
                    "开启转存后同步时，STRM 同步根 ID 必须是正整数"
                ) from exc
            if sync_root_id <= 0:
                raise ValueError("开启转存后同步时，STRM 同步根 ID 必须是正整数")
        return {
            "account_id": account_id,
            "offline_target_remote_id": offline,
            "transfer_target_remote_id": transfer,
            "sync_root_id": sync_root_id,
        }

    def _state(self) -> dict:
        if self.host is None:
            return {}
        value = self.host.data.read_json("state")
        return value if isinstance(value, dict) else {}

    def _update_state(self, **values) -> None:
        if self.host is None:
            return
        state = self._state()
        state.update(values)
        self.host.data.write_json("state", state)

    def _receipt(self, key: str) -> dict | None:
        receipts = self._state().get("receipts")
        value = receipts.get(key) if isinstance(receipts, dict) else None
        return value if isinstance(value, dict) else None

    def _save_receipt(self, key: str, value: dict) -> None:
        state = self._state()
        receipts = (
            state.get("receipts") if isinstance(state.get("receipts"), dict) else {}
        )
        receipts[key] = value
        state["receipts"] = dict(list(receipts.items())[-200:])
        self.host.data.write_json("state", state)

    def _append_history(self, source: str, source_ref: str | None, item: dict) -> None:
        state = self._state()
        history = state.get("history") if isinstance(state.get("history"), list) else []
        history.insert(
            0,
            {
                "time": self._now(),
                "source": source,
                "source_ref": source_ref or "-",
                "type": "离线下载" if item["type"] == "offline" else "分享转存",
                "status": item["status"],
                "message": item["message"],
            },
        )
        state["history"] = history[:50]
        self.host.data.write_json("state", state)

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")


plugin = One15ResourceReceiver
