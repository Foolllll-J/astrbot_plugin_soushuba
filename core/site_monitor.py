import asyncio
import datetime
from typing import Dict, List, Optional, Callable, Awaitable

import aiohttp
from astrbot.api import logger
from astrbot.api.event import MessageEventResult

from .http_session import ProxyClientSession
from .url_resolver import UrlResolver


class SiteMonitorService:
    def __init__(
        self,
        url_resolver: UrlResolver,
        context,
        monitor_check_interval: int,
        monitor_failure_recheck_delay: int,
        get_kv_data: Callable[[str, dict], Awaitable[dict]],
        put_kv_data: Callable[[str, dict], Awaitable[None]],
        session_factory,
        subscribers_key: str = "status_monitor_subscribers",
    ):
        self.url_resolver = url_resolver
        self.context = context
        self.monitor_check_interval = monitor_check_interval
        self.monitor_failure_recheck_delay = monitor_failure_recheck_delay
        self.get_kv_data = get_kv_data
        self.put_kv_data = put_kv_data
        self.session_factory = session_factory
        self.subscribers_key = subscribers_key

        self._monitor_stop_event = asyncio.Event()
        self._monitor_task: Optional[asyncio.Task] = None
        self._monitor_subscribers: Dict[str, List[str]] = {"ssb": [], "sxsy": []}
        self._monitor_states: Dict[str, Dict[str, object]] = {
            "ssb": {"failed": False, "real_url": None},
            "sxsy": {"failed": False, "real_url": None},
        }

    def get_subscriber_count(self, site_key: str) -> int:
        return len(self._monitor_subscribers.get(site_key, []))

    async def start(self):
        await self._load_monitor_subscribers()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_stop_event.set()
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            logger.debug("[状态监控] 已清理旧的监控任务，准备启动新任务。")
        self._monitor_stop_event.clear()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        self._monitor_stop_event.set()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def _load_monitor_subscribers(self):
        data = await self.get_kv_data(self.subscribers_key, {})
        if not isinstance(data, dict):
            return
        for site_key in ("ssb", "sxsy"):
            sessions = data.get(site_key, [])
            if isinstance(sessions, list):
                normalized = [str(s).strip() for s in sessions if str(s).strip()]
                self._monitor_subscribers[site_key] = list(dict.fromkeys(normalized))

    async def _save_monitor_subscribers(self):
        await self.put_kv_data(self.subscribers_key, self._monitor_subscribers)

    async def set_site_subscription(self, site_key: str, session: str, enable: bool) -> bool:
        subscribers = self._monitor_subscribers.setdefault(site_key, [])
        if enable:
            if session in subscribers:
                return False
            subscribers.append(session)
        else:
            if session not in subscribers:
                return False
            subscribers.remove(session)
        await self._save_monitor_subscribers()
        return True

    async def _resolve_site_monitor_url(
        self, site_key: str, session: ProxyClientSession
    ) -> Optional[str]:
        if site_key == "ssb":
            return await self.url_resolver.resolve_ssb_monitor_url(session)
        if site_key == "sxsy":
            return await self.url_resolver.resolve_sxsy_monitor_url(session)
        return None

    async def _check_site_status(
        self,
        site_key: str,
        session: ProxyClientSession,
        resolver: Callable[[ProxyClientSession], Awaitable[Optional[str]]],
    ) -> tuple[bool, str, Optional[str]]:
        state = self._monitor_states[site_key]
        current_url = state.get("real_url")
        last_error = ""

        if isinstance(current_url, str) and current_url:
            ok, detail, final_url = await self.url_resolver.probe_site_access(
                session, current_url, site_key
            )
            if ok:
                state["real_url"] = final_url
                return True, detail, final_url
            last_error = f"{final_url} -> {detail}"

        resolved_url = await resolver(session)
        if not resolved_url:
            if not current_url:
                return False, "无法获取真实站点地址", None
            return False, last_error or "访问失败", str(current_url)

        if resolved_url != current_url:
            ok, detail, final_url = await self.url_resolver.probe_site_access(
                session, resolved_url, site_key
            )
            if ok:
                state["real_url"] = final_url
                return True, detail, final_url
            last_error = f"{final_url} -> {detail}"

        return (
            False,
            last_error or "访问失败",
            self.url_resolver.normalize_base_url(str(resolved_url)),
        )

    async def _send_monitor_notification(self, site_key: str, text: str):
        subscribers = list(self._monitor_subscribers.get(site_key, []))
        if not subscribers:
            return
        for session in subscribers:
            try:
                sent = await self.context.send_message(session, MessageEventResult().message(text))
                if not sent:
                    logger.warning(f"[状态监控] 发送失败，未找到会话平台: {session}")
            except Exception as e:
                logger.error(f"[状态监控] 向会话 {session} 发送通知失败: {e}")

    async def _confirm_failure_with_recheck(
        self,
        site_key: str,
        site_name: str,
        is_ok: bool,
        detail: str,
        real_url: Optional[str],
        session: ProxyClientSession,
        resolver: Callable[[ProxyClientSession], Awaitable[Optional[str]]],
    ) -> tuple[bool, str, Optional[str]]:
        if is_ok:
            return is_ok, detail, real_url
        state = self._monitor_states.get(site_key, {})
        if bool(state.get("failed", False)):
            return is_ok, detail, real_url
        if self.monitor_failure_recheck_delay <= 0:
            return is_ok, detail, real_url
        if self.monitor_failure_recheck_delay >= self.monitor_check_interval:
            logger.debug(
                f"[状态监控] {site_name} 跳过异常复检: "
                f"复检延迟({self.monitor_failure_recheck_delay}s) >= 检测间隔({self.monitor_check_interval}s)"
            )
            return is_ok, detail, real_url

        logger.warning(
            f"[状态监控] {site_name} 首次异常，{self.monitor_failure_recheck_delay}s 后复检一次: {detail}"
        )
        await asyncio.sleep(self.monitor_failure_recheck_delay)

        retry_ok, retry_detail, retry_real_url = await self._check_site_status(
            site_key, session, resolver
        )
        if retry_ok:
            logger.debug(f"[状态监控] {site_name} 首次异常后复检恢复，忽略本次异常告警。")
            return True, retry_detail, retry_real_url

        concise_detail = str(retry_detail or detail or "").strip()
        if "->" in concise_detail:
            concise_detail = concise_detail.split("->", 1)[1].strip()
        if not concise_detail:
            concise_detail = "访问异常"
        return False, concise_detail, retry_real_url or real_url

    async def _handle_site_transition(
        self,
        site_key: str,
        site_name: str,
        is_ok: bool,
        detail: str,
        real_url: Optional[str],
    ):
        state = self._monitor_states[site_key]
        if real_url:
            state["real_url"] = real_url
        was_failed = bool(state.get("failed", False))
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        site_url = str(state.get("real_url") or real_url or "未知")
        brief_detail = str(detail or "").strip()
        if "->" in brief_detail:
            brief_detail = brief_detail.split("->", 1)[1].strip()
        if not brief_detail:
            brief_detail = "访问异常"

        if is_ok:
            if was_failed:
                state["failed"] = False
                text = (
                    f"✅【网站状态恢复】{site_name}\n"
                    f"时间: {now_str}\n"
                    f"站点: {site_url}\n"
                    "状态: 已恢复正常"
                )
                await self._send_monitor_notification(site_key, text)
            return

        if not was_failed:
            state["failed"] = True
            text = (
                f"❌【网站状态异常】{site_name}\n"
                f"时间: {now_str}\n"
                f"站点: {site_url}\n"
                f"原因: {brief_detail}"
            )
            await self._send_monitor_notification(site_key, text)

    async def _monitor_once(self):
        has_ssb_subscribers = bool(self._monitor_subscribers.get("ssb"))
        has_sxsy_subscribers = bool(self._monitor_subscribers.get("sxsy"))
        if not has_ssb_subscribers and not has_sxsy_subscribers:
            return

        async with self.session_factory() as session:
            if has_ssb_subscribers:
                ok, detail, real_url = await self._check_site_status(
                    "ssb",
                    session,
                    lambda sess: self._resolve_site_monitor_url("ssb", sess),
                )
                ok, detail, real_url = await self._confirm_failure_with_recheck(
                    "ssb",
                    "搜书吧",
                    ok,
                    detail,
                    real_url,
                    session,
                    lambda sess: self._resolve_site_monitor_url("ssb", sess),
                )
                await self._handle_site_transition("ssb", "搜书吧", ok, detail, real_url)

            if has_sxsy_subscribers:
                ok, detail, real_url = await self._check_site_status(
                    "sxsy",
                    session,
                    lambda sess: self._resolve_site_monitor_url("sxsy", sess),
                )
                ok, detail, real_url = await self._confirm_failure_with_recheck(
                    "sxsy",
                    "尚香书苑",
                    ok,
                    detail,
                    real_url,
                    session,
                    lambda sess: self._resolve_site_monitor_url("sxsy", sess),
                )
                await self._handle_site_transition("sxsy", "尚香书苑", ok, detail, real_url)

    async def _monitor_loop(self):
        while not self._monitor_stop_event.is_set():
            try:
                await self._monitor_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[状态监控] 轮询异常: {e}")

            try:
                await asyncio.wait_for(
                    self._monitor_stop_event.wait(),
                    timeout=self.monitor_check_interval,
                )
            except asyncio.TimeoutError:
                continue
