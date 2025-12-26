import asyncio
import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Optional
import os
import re

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain
from astrbot.api import logger
from astrbot.core.config import AstrBotConfig

DEFAULT_SXSY_HOST = "sxsy19.com" # 默认域名

@register(
    "astrbot_plugin_soushuba",
    "Foolllll",
    "搜书吧链接获取",
    "1.1.0",
    "https://github.com/Foolllll-J/astrbot_plugin_soushuba",
)
class SoushuBaLinkExtractorPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.target_domains: List[str] = [
            "https://soushu2022.com",
            "https://soushu2025.com",
            "https://soushu2030.com",
            "https://soushu2035.com",
        ]
        self.plugin_config = config


    async def _extract_link_from_url(self, session: aiohttp.ClientSession, url: str) -> Optional[str]:
        """尝试访问URL并提取指定链接。成功则返回链接，失败返回 None。"""
        try:
            ssl_verify = False if url.startswith("https://") else True
            
            async with session.get(url, timeout=20, ssl=ssl_verify) as response:
                final_url = str(response.url)
                html_content = await response.text()
            
            # 步骤1: 检查并处理 JavaScript 重定向
            js_redirect_match = re.search(r"window\.location\.href\s*=\s*['\"](.*?)['\"];", html_content)
            if js_redirect_match:
                redirect_target_url = urljoin(final_url, js_redirect_match.group(1))
                return await self._extract_link_from_url(session, redirect_target_url)

            # 步骤2: 检查并处理 Meta Refresh 重定向
            meta_refresh_match = re.search(r"<meta http-equiv=\"refresh\" content=\"[\d\.]*;\s*url=(.*?)\"", html_content, re.IGNORECASE)
            if meta_refresh_match:
                redirect_target_url = urljoin(final_url, meta_refresh_match.group(1))
                return await self._extract_link_from_url(session, redirect_target_url)

            # 步骤3: 执行 BeautifulSoup 查找
            soup = BeautifulSoup(html_content, 'lxml') 
            link_element = soup.select_one('a.link') 
            if not link_element:
                link_element = soup.find('a', string='搜书吧')
            if not link_element:
                link_element = soup.find('a')

            if link_element and link_element.has_attr('href'):
                link_url = link_element['href']
                if not link_url.startswith(('http://', 'https://')):
                    link_url = urljoin(final_url, link_url)
                return link_url

        except Exception as e: 
            logger.error(f"访问 {url} 失败: {e}")
        return None

    @filter.command("ssb", alias={'搜书吧'})
    async def ssb_command(self, event: AstrMessageEvent):
        """
        获取搜书吧的网址。
        用法: /ssb
        """
        logger.info(f"用户 {event.get_sender_name()} 触发 /ssb 命令，开始查找搜书吧网址。")
        
        async with aiohttp.ClientSession() as session:
            for domain_url in self.target_domains:
                link_url = await self._extract_link_from_url(session, domain_url)
                if link_url:
                    yield event.plain_result(f"📖 成功找到搜书吧最新网址：\n{link_url}")
                    return
            
        yield event.plain_result("❌ 抱歉，所有导航网站均无法访问或未找到可用链接。")

    @filter.command("sxsy", alias={'尚香书苑'})
    async def sxsy_command(self, event: AstrMessageEvent):
        """
        获取尚香书苑的网址。
        用法: /sxsy
        """
        logger.info(f"用户 {event.get_sender_name()} 触发 /sxsy 命令，开始查找尚香书苑网址。")

        async with aiohttp.ClientSession() as session:
            try:
                url = "https://sxsy.org/"
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0',
                }
                async with session.get(url, headers=headers, timeout=10, ssl=False) as response:
                    if response.status == 200:
                        text = await response.text()
                        match = re.search(r'href="https://([^"]+)"', text)
                        if match:
                            host = match.group(1)
                            yield event.plain_result(f"🌸 成功找到尚香书苑最新网址：\nhttps://{host}")
                            return
                    
                    yield event.plain_result(f"🌸 尚香书苑最新网址：\nhttps://{DEFAULT_SXSY_HOST}")

            except Exception as e:
                logger.error(f"[获取sxsy host] 发生错误: {e}")
                yield event.plain_result(f"🌸 尚香书苑目前网址：\nhttps://{DEFAULT_SXSY_HOST}")

    @filter.command("sis", alias={'第一会所'})
    async def sis_command(self, event: AstrMessageEvent):
        """
        获取第一会所的网址。
        用法: /sis
        """
        logger.info(f"用户 {event.get_sender_name()} 触发 /sis 命令，开始查找第一会所网址。")
        
        target_navs = ["http://sis001dz.org/", "http://www.sis001home.com/"]
        
        async with aiohttp.ClientSession() as session:
            for url in target_navs:
                try:
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0',
                    }
                    async with session.get(url, headers=headers, timeout=10) as response:
                        if response.status == 200:
                            text = await response.text()
                            soup = BeautifulSoup(text, 'lxml')
                            # 查找包含“地址一”文本的 <a> 标签
                            link_element = soup.find('a', string=re.compile(r'地址一'))
                            if link_element and link_element.has_attr('href'):
                                link_url = link_element['href']
                                yield event.plain_result(f"🔞 成功找到第一会所最新网址：\n{link_url}")
                                return
                except Exception as e:
                    logger.error(f"访问 {url} 失败: {e}")
                    continue
            
        yield event.plain_result("❌ 抱歉，第一会所导航站目前无法访问。")

    async def terminate(self):
        """插件销毁时的清理工作"""
        logger.info("搜书吧链接获取插件已卸载")