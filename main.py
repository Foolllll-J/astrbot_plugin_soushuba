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

@register(
    "astrbot_plugin_soushuba",
    "Foolllll",
    "搜书吧助手",
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
        self.search_result_count = config.get("search_result_count", 10)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

    async def _get_text(self, response: aiohttp.ClientResponse) -> str:
        """获取响应内容并处理编码问题"""
        content = await response.read()
        
        charset = response.charset
        if charset:
            try:
                return content.decode(charset)
            except:
                pass
        
        for encoding in ['utf-8', 'gbk', 'gb2312', 'big5']:
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        
        return content.decode('utf-8', errors='ignore')

    async def _extract_link_from_url(self, session: aiohttp.ClientSession, url: str) -> Optional[str]:
        """尝试访问URL并提取指定链接。成功则返回链接，失败返回 None。"""
        try:
            ssl_verify = False if url.startswith("https://") else True
            
            async with session.get(url, timeout=20, ssl=ssl_verify) as response:
                final_url = str(response.url)
                html_content = await self._get_text(response)
            
            js_redirect_match = re.search(r"window\.location\.href\s*=\s*['\"](.*?)['\"];", html_content)
            if js_redirect_match:
                redirect_target_url = urljoin(final_url, js_redirect_match.group(1))
                return await self._extract_link_from_url(session, redirect_target_url)

            meta_refresh_match = re.search(r"<meta http-equiv=\"refresh\" content=\"[\d\.]*;\s*url=(.*?)\"", html_content, re.IGNORECASE)
            if meta_refresh_match:
                redirect_target_url = urljoin(final_url, meta_refresh_match.group(1))
                return await self._extract_link_from_url(session, redirect_target_url)

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
        """获取搜书吧的网址"""
        async with aiohttp.ClientSession() as session:
            for domain_url in self.target_domains:
                link_url = await self._extract_link_from_url(session, domain_url)
                if link_url:
                    yield event.plain_result(f"📖 成功找到搜书吧最新网址：\n{link_url}")
                    return
        yield event.plain_result("❌ 抱歉，所有导航网站均无法访问或未找到可用链接。")

    @filter.command("sxsy", alias={'尚香书苑'})
    async def sxsy_command(self, event: AstrMessageEvent):
        """尚香书苑搜索"""
        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            # 基础网址获取逻辑
            async with aiohttp.ClientSession() as session:
                try:
                    url = "https://sxsy.org/"
                    async with session.get(url, headers=self.headers, timeout=10, ssl=False) as response:
                        if response.status == 200:
                            text = await self._get_text(response)
                            match = re.search(r'href="https://([^"]+)"', text)
                            if match:
                                yield event.plain_result(f"🌸 成功找到尚香书苑最新网址：\nhttps://{match.group(1)}")
                                return
                except Exception as e:
                    logger.error(f"[获取sxsy host] 错误: {e}")
            yield event.plain_result("❌ 抱歉，尚香书苑导航站目前无法访问。")
            return

        keyword = args[1]
        cookie = self.plugin_config.get("sxsy_cookie", "") if self.plugin_config else ""
        if not cookie:
            yield event.plain_result("❌ 请先在插件配置中设置 sxsy_cookie。")
            return

        yield event.plain_result(f"🔍 正在尚香书苑搜索: {keyword}...")

        async with aiohttp.ClientSession() as session:
            try:
                # 1. 获取最新 host
                host = "sxsy87.com"
                try:
                    async with session.get("https://sxsy.org/", timeout=10, ssl=False) as resp:
                        if resp.status == 200:
                            t = await self._get_text(resp)
                            m = re.search(r'href="https://([^"]+)"', t)
                            if m: host = m.group(1)
                except: pass

                # 2. 准备 POST 请求
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
                    'Cookie': cookie,
                    'Referer': f"https://{host}/search.php?mod=forum"
                }
                post_url = f"https://{host}/search.php?mod=forum"

                # 提取 formhash
                formhash = ""
                try:
                    async with session.get(post_url, headers=headers, timeout=10, ssl=False) as f_resp:
                        f_html = await self._get_text(f_resp)
                        fh_match = re.search(r'name="formhash" value="([a-f0-9]+)"', f_html)
                        if fh_match: formhash = fh_match.group(1)
                except: pass

                post_data = {
                    'mod': 'forum',
                    'searchsubmit': 'yes',
                    'srchtxt': keyword,
                    'formhash': formhash
                }

                # 3. 发送 POST 搜索
                logger.info(f"[sxsy 搜索] 尝试 POST 搜索: {post_url}")
                async with session.post(post_url, data=post_data, headers=headers, timeout=15, ssl=False) as p_resp:
                    html = await self._get_text(p_resp)
                    logger.info(f"[sxsy 搜索] POST 响应 URL: {p_resp.url}, 长度: {len(html)}")

                # 4. 检查异常状态
                if "请先登录" in html or "访问限制" in html:
                    yield event.plain_result("❌ Cookie 可能已失效，请重新配置。")
                    return
                if "未找到符合条件的搜索结果" in html:
                    yield event.plain_result(f"📦 未找到与 “{keyword}” 相关的搜索结果。")
                    return

                # 5. 解析结果
                soup = BeautifulSoup(html, 'lxml')
                items = soup.select('div#threadlist ul li.pbw') or soup.select('div.slst ul li.pbw')
                logger.info(f"[sxsy 搜索] 解析到 {len(items)} 条结果")

                if not items:
                    yield event.plain_result("❌ 无法获取搜索结果，请检查 Cookie 是否过期。")
                    return

                results = []
                for item in items[:self.search_result_count]:
                    title_el = item.select_one('h3.xs3 a')
                    if not title_el: continue
                    
                    title = "".join(title_el.find_all(string=True, recursive=True)).strip()
                    link = urljoin(f"https://{host}/", title_el['href'])
                    
                    # 提取时间
                    time_text = "未知"
                    time_span = item.select_one('p span') # Discuz 搜索页通常第一个 span 是时间
                    if time_span:
                        time_text = time_span.get_text(strip=True)
                    
                    results.append(f"📌 {title}\n🔗 {link}\n📅 时间: {time_text}")

                reply = f"✅ 为您找到以下关于 “{keyword}” 的结果：\n\n" + "\n\n".join(results)
                yield event.plain_result(reply)

            except Exception as e:
                logger.error(f"sxsy 搜索出错: {e}")
                yield event.plain_result(f"❌ 搜索过程中发生错误: {str(e)}，请稍后重试。")

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
        logger.info("搜书吧链接获取插件已卸载")