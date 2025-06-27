import asyncio
import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from typing import List, Dict, Optional
import os
import re

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain
from astrbot.api import logger
from astrbot.core.config import AstrBotConfig


@register(
    "astrbot_plugin_soushuba",
    "Foolllll",
    "搜书吧链接提取器",
    "1.0.0",
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


    # 修改此处：添加一个可选参数 start_url，默认为 None
    async def _extract_link_from_url(self, session: aiohttp.ClientSession, url: str, start_url: Optional[str] = None) -> str:
        """尝试访问URL并提取指定链接，即使状态码是404也会尝试解析内容。
        start_url 是最初的请求URL，用于最终消息报告。
        """
        # 如果是首次调用，将当前URL作为start_url
        if start_url is None:
            start_url = url

        try:
            ssl_verify = False if url.startswith("https://") else True
            if not ssl_verify:
                logger.warning(f"由于证书问题，访问 {url} 将禁用 SSL 验证。")
            
            async with session.get(url, timeout=20, ssl=ssl_verify) as response:
                final_url = str(response.url)
                html_content = await response.text()
                status_code = response.status
            
            logger.info(f"成功访问 {url}。最终URL: {final_url}，状态码: {status_code}。HTML内容长度: {len(html_content)} 字节。")
            logger.debug(f"HTML Content preview (first 500 chars from {final_url}): \n{html_content[:500]}...") 

            # 步骤1: 检查并处理 JavaScript 重定向
            js_redirect_match = re.search(r"window\.location\.href\s*=\s*['\"](.*?)['\"];", html_content)
            if js_redirect_match:
                redirect_target_url = urljoin(final_url, js_redirect_match.group(1))
                logger.info(f"检测到 JavaScript 跳转到: {redirect_target_url}，再次请求。")
                # **修改此处：递归调用时传入原始的 start_url**
                return await self._extract_link_from_url(session, redirect_target_url, start_url)

            # 步骤2: 检查并处理 Meta Refresh 重定向
            meta_refresh_match = re.search(r"<meta http-equiv=\"refresh\" content=\"[\d\.]*;\s*url=(.*?)\"", html_content, re.IGNORECASE)
            if meta_refresh_match:
                redirect_target_url = urljoin(final_url, meta_refresh_match.group(1))
                logger.info(f"检测到 Meta Refresh 跳转到: {redirect_target_url}，再次请求。")
                # **修改此处：递归调用时传入原始的 start_url**
                return await self._extract_link_from_url(session, redirect_target_url, start_url)

            # 步骤3: 执行 BeautifulSoup 查找
            soup = BeautifulSoup(html_content, 'lxml') 
            link_element = None

            link_element = soup.select_one('a.link') 
            if not link_element:
                link_element = soup.find('a', string='搜书吧')
            if not link_element:
                link_element = soup.find('a')

            if link_element and link_element.has_attr('href'):
                link_url = link_element['href']
                if not link_url.startswith(('http://', 'https://')):
                    link_url = urljoin(final_url, link_url)
                
                logger.info(f"BeautifulSoup 最终找到链接: {link_url}")
                # **修改此处：在返回成功消息时使用 start_url**
                return f"✅ 成功找到链接于 {start_url}:\n{link_url}"
            else:
                logger.warning(f"所有查找策略均未能在 {final_url} 找到有效链接。")
                # **修改此处：在返回信息性消息时使用 start_url**
                return f"ℹ️ 访问 {start_url} 成功，但未找到任何有效的链接元素。"

        except aiohttp.ClientError as e: 
            logger.error(f"❌ 访问 {url} 失败: 网络连接错误 - {e}")
            # **修改此处：在返回错误消息时使用 start_url**
            return f"❌ 访问 {start_url} 失败: 网络连接错误 - {e}"
        except asyncio.TimeoutError: 
            logger.error(f"❌ 访问 {url} 超时。")
            # **修改此处：在返回超时错误时使用 start_url**
            return f"❌ 访问 {start_url} 超时，请稍后再试。"
        except Exception as e: 
            logger.error(f"❌ 访问 {url} 发生未知错误: {e}")
            # **修改此处：在返回未知错误时使用 start_url**
            return f"❌ 访问 {start_url} 发生未知错误: {e}"

    @filter.command("ssb")
    async def ssb_command(self, event: AstrMessageEvent):
        """
        依次尝试访问预设列表中的搜书网站，并返回第一个成功访问到的页面中的第一个链接。
        用法: /ssb
        """
        logger.info(f"用户 {event.get_sender_name()} 触发 /ssb 命令，开始搜书。")
        yield event.plain_result("🚀 正在尝试访问搜书网站，请稍候...")
        
        async with aiohttp.ClientSession() as session:
            for domain_url in self.target_domains:
                logger.info(f"正在尝试访问: {domain_url}")
                result_message = await self._extract_link_from_url(session, domain_url, domain_url) 
                
                if result_message.startswith("✅") or result_message.startswith("ℹ️"):
                    yield event.plain_result(result_message)
                    return
                else:
                    logger.warning(f"访问 {domain_url} 失败，正在尝试下一个...")
            
        yield event.plain_result("❌ 抱歉，所有预设网站均无法访问或未找到可用链接。")


    async def terminate(self):
        """插件销毁时的清理工作"""
        logger.info("搜书吧链接提取器插件已卸载")