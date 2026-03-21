import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from typing import List, Optional
import os
import re

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger
import astrbot.api.message_components as Comp

from .core.url_resolver import UrlResolver
from .core.search_service import SearchService
from .core.download_service import SsbDownloadService
from .core.ssb_flow import SsbFlow
from .core.sxsy_flow import SxsyFlow
from .core.site_monitor import SiteMonitorService
from .core.cache import UserSearchCache


class SoushuBaLinkExtractorPlugin(Star):
    MONITOR_SUBSCRIBERS_KEY = "status_monitor_subscribers"
    MONITOR_FAILURE_RECHECK_DELAY_SECONDS = 10

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.target_domains: List[str] = [
            "https://soushu2022.com",
            "https://soushu2025.com",
            "https://soushu2030.com",
            "https://soushu2035.com",
        ]
        self.plugin_config = config or {}
        self.search_result_count = self.plugin_config.get("search_result_count", 10)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        raw_interval = self.plugin_config.get("monitor_check_interval", 3600)
        try:
            self.monitor_check_interval = max(int(raw_interval), 10)
        except (TypeError, ValueError):
            self.monitor_check_interval = 3600
        self.monitor_failure_recheck_delay = self.MONITOR_FAILURE_RECHECK_DELAY_SECONDS

        self.data_dir = StarTools.get_data_dir("astrbot_plugin_soushuba")
        os.makedirs(self.data_dir, exist_ok=True)
        self.ssb_cookie_file = os.path.join(self.data_dir, "ssb_cookies.json")
        self.url_resolver = UrlResolver(self.headers, self.plugin_config, self.target_domains)
        self.search_service = SearchService(
            self.headers,
            self.plugin_config,
            self.search_result_count,
            self.ssb_cookie_file,
            self.url_resolver,
        )
        self.ssb_cache = UserSearchCache()
        self.ssb_download = SsbDownloadService(
            self.headers,
            self.data_dir,
            self.url_resolver,
            self.search_service,
            self.get_kv_data,
            self.put_kv_data,
        )
        allow_users = self.plugin_config.get("download_allow_users", ["0"])
        self.search_service.set_access_control(allow_users)
        self.ssb_flow = SsbFlow(
            self.search_service,
            self.ssb_download,
            self.ssb_cache,
            self.target_domains,
        )
        self.sxsy_cache = UserSearchCache()
        self.sxsy_flow = SxsyFlow(
            self.search_service,
            self.ssb_download,
            self.sxsy_cache,
        )
        self.site_monitor = SiteMonitorService(
            url_resolver=self.url_resolver,
            context=self.context,
            monitor_check_interval=self.monitor_check_interval,
            monitor_failure_recheck_delay=self.monitor_failure_recheck_delay,
            get_kv_data=self.get_kv_data,
            put_kv_data=self.put_kv_data,
            subscribers_key=self.MONITOR_SUBSCRIBERS_KEY,
        )

    async def initialize(self):
        await self.site_monitor.start()
        logger.debug(
            f"[状态监控] 后台任务已启动，检测间隔: {self.monitor_check_interval}s, "
            f"异常复检延迟: {self.monitor_failure_recheck_delay}s, "
            f"搜书吧订阅: {self.site_monitor.get_subscriber_count('ssb')}, "
            f"SXSY订阅: {self.site_monitor.get_subscriber_count('sxsy')}"
        )

    def _normalize_base_url(self, url: str) -> str:
        return self.url_resolver.normalize_base_url(url)

    def _get_sxsy_search_base_url(self) -> Optional[str]:
        return self.url_resolver.get_sxsy_search_base_url()

    async def _extract_sxsy_nav_image_url(
        self,
        session: aiohttp.ClientSession,
        nav_url: str = "https://sxsy.org/",
    ) -> Optional[str]:
        return await self.url_resolver.extract_sxsy_nav_image_url(session, nav_url)


    async def _get_text(self, response: aiohttp.ClientResponse) -> str:
        return await self.url_resolver.get_text(response)

    async def _extract_link_from_url(self, session: aiohttp.ClientSession, url: str) -> Optional[str]:
        return await self.url_resolver.extract_link_from_url(session, url)

    @filter.command("ssb", alias={'搜书吧'})
    async def ssb_command(self, event: AstrMessageEvent):
        """获取搜书吧的网址或搜索书籍"""
        args = event.message_str.strip().split(maxsplit=1)
        arg = args[1] if len(args) > 1 else None
        async for result in self.ssb_flow.handle(event, arg):
            yield result

    @filter.command("sxsy", alias={'尚香书苑'})
    async def sxsy_command(self, event: AstrMessageEvent):
        """尚香书苑搜索"""
        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            # SXSY 导航使用图片展示，直接返回导航图。
            async with aiohttp.ClientSession() as session:
                try:
                    nav_image_url = await self.url_resolver.extract_sxsy_nav_image_url(
                        session
                    )
                    if nav_image_url:
                        chain = [
                            Comp.Plain("🌸 成功获取尚香书苑最新网址："),
                            Comp.Image.fromURL(nav_image_url),
                        ]
                        yield event.chain_result(chain)
                        return
                except Exception as e:
                    logger.error(f"[获取sxsy导航图] 错误: {e}")
            yield event.plain_result("❌ 抱歉，尚香书苑导航站目前无法访问或未找到导航图。")
            return

        arg = args[1].strip()
        try:
            async for result in self.sxsy_flow.handle(event, arg):
                yield result
        except Exception as e:
            logger.error(f"sxsy 处理出错: {e}")
            yield event.plain_result(f"❌ 处理过程中发生错误: {str(e)}，请稍后重试。")

    async def _handle_monitor_command(
        self,
        event: AstrMessageEvent,
        site_key: str,
        site_name: str,
    ):
        args = event.message_str.strip().split(maxsplit=1)
        action = args[1].strip() if len(args) > 1 else "on"
        action_lower = action.lower()
        disable_actions = {"off", "close", "disable", "关闭", "取消", "停止"}
        enable = action_lower not in disable_actions and action not in disable_actions
        session = event.unified_msg_origin

        changed = await self.site_monitor.set_site_subscription(site_key, session, enable)
        if enable:
            if changed:
                logger.debug(
                    f"[状态监控] 已订阅 {site_name}: session={session}, "
                    f"interval={self.monitor_check_interval}s"
                )
                yield event.plain_result(f"✅ 已订阅 {site_name} 状态监控通知。")
            else:
                yield event.plain_result(
                    f"ℹ️ 当前会话已在 {site_name} 状态监控订阅中。\n"
                    "可使用 `off` / `关闭` / `取消` 取消订阅。"
                )
            return

        if changed:
            yield event.plain_result(f"✅ 已取消 {site_name} 状态监控订阅。")
        else:
            yield event.plain_result(f"ℹ️ 当前会话未订阅 {site_name} 状态监控。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ssbmon", alias={"监控搜书吧"})
    async def ssb_monitor_command(self, event: AstrMessageEvent):
        """订阅或取消订阅搜书吧状态监控（管理员）"""
        async for result in self._handle_monitor_command(event, "ssb", "搜书吧"):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("sxsymon", alias={"监控尚香书苑"})
    async def sxsy_monitor_command(self, event: AstrMessageEvent):
        """订阅或取消订阅 SXSY 状态监控（管理员）"""
        async for result in self._handle_monitor_command(event, "sxsy", "SXSY"):
            yield result

    @filter.command("sis", alias={'第一会所'})
    async def sis_command(self, event: AstrMessageEvent):
        """获取第一会所的网址"""
        target_navs = ["http://sis001dz.org/", "http://www.sis001home.com/"]
        async with aiohttp.ClientSession() as session:
            for url in target_navs:
                try:
                    async with session.get(url, headers=self.headers, timeout=10) as response:
                        if response.status == 200:
                            text = await self._get_text(response)
                            soup = BeautifulSoup(text, 'lxml')
                            link_element = soup.find('a', string=re.compile(r'地址一'))
                            if link_element and link_element.has_attr('href'):
                                yield event.plain_result(f"🔞 成功找到第一会所最新网址：\n{link_element['href']}")
                                return
                except: continue
        yield event.plain_result("❌ 抱歉，第一会所导航站目前无法访问。")

    @filter.command("01bz", alias={'第一版主'})
    async def dybz_command(self, event: AstrMessageEvent):
        """获取第一版主的网址"""
        target_navs = ["https://www.龙腾小说.com/", "http://01bz.cc/"]
        async with aiohttp.ClientSession() as session:
            for url in target_navs:
                try:
                    async with session.get(url, headers=self.headers, timeout=10) as response:
                        if response.status == 200:
                            text = await self._get_text(response)
                            soup = BeautifulSoup(text, 'lxml')
                            link_element = soup.find('a', string=re.compile(r'最新线路\s*1'))
                            if link_element and link_element.has_attr('href'):
                                yield event.plain_result(f"📚 成功找到第一版主最新网址：\n{link_element['href']}")
                                return
                except: continue
        yield event.plain_result("❌ 抱歉，第一版主导航站目前无法访问。")

    @filter.command("uaa", alias={'有爱爱'})
    async def uaa_command(self, event: AstrMessageEvent):
        """获取有爱爱的网址"""
        url = "https://uaadizhi.com/"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=self.headers, timeout=10) as response:
                    if response.status == 200:
                        text = await self._get_text(response)
                        soup = BeautifulSoup(text, 'lxml')
                        for li in soup.find_all('li'):
                            span = li.find('span')
                            if span and '最新' in span.get_text():
                                a_tag = li.find('a')
                                if a_tag:
                                    yield event.plain_result(f"💕 成功找到有爱爱最新网址：\n{a_tag['href']}")
                                    return
            except: pass
        yield event.plain_result("❌ 抱歉，有爱爱导航站目前无法访问。")

    async def terminate(self):
        await self.site_monitor.stop()
        logger.debug("搜书吧链接获取插件已卸载")
