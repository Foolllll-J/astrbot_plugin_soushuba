import json
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlencode
from typing import List, Dict, Optional
import os
import re
import datetime

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.message_components import Plain
from astrbot.api import logger

@register(
    "astrbot_plugin_soushuba",
    "Foolllll",
    "搜书吧助手",
    "1.1.1",
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
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_soushuba")
        os.makedirs(self.data_dir, exist_ok=True)
        self.ssb_cookie_file = os.path.join(self.data_dir, "ssb_cookies.json")

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

    def _load_ssb_cookies(self, username: str) -> dict:
        if os.path.exists(self.ssb_cookie_file):
            try:
                with open(self.ssb_cookie_file, "r", encoding="utf-8") as f:
                    all_cookies = json.load(f)
                    data = all_cookies.get(username, {})
                    return data.get("cookies", {})
            except Exception as e:
                logger.error(f"加载 SSB Cookie 失败: {e}")
        return {}

    def _save_ssb_cookies(self, username: str, cookies: dict):
        try:
            all_cookies = {}
            if os.path.exists(self.ssb_cookie_file):
                with open(self.ssb_cookie_file, "r", encoding="utf-8") as f:
                    try:
                        all_cookies = json.load(f)
                    except: pass
            all_cookies[username] = {
                "cookies": cookies,
                "update_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            with open(self.ssb_cookie_file, "w", encoding="utf-8") as f:
                json.dump(all_cookies, f, ensure_ascii=False, indent=2)
            logger.info(f"[SSB] 账号 {username} 的 Cookie 已保存")
        except Exception as e:
            logger.error(f"保存 SSB Cookie 失败: {e}")

    async def _ssb_login(self, session, base_url: str, username, password):
        """参考 ssb.py 的登录逻辑"""
        try:
            logger.info(f"[SSB 登录] 开始登录流程: {username} @ {base_url}")
            # 1. 获取 formhash
            login_url = urljoin(base_url, "member.php?mod=logging&action=login")
            async with session.get(login_url, headers=self.headers, timeout=15, ssl=False) as resp:
                html = await self._get_text(resp)
                soup = BeautifulSoup(html, "lxml")
                formhash_el = soup.find("input", {"name": "formhash"})
                if not formhash_el: 
                    logger.error("[SSB 登录] 无法在登录页面获取 formhash")
                    return False
                formhash = formhash_el["value"]
                logger.info(f"[SSB 登录] 获取到 formhash: {formhash}")

            # 2. 提交登录
            login_post_url = urljoin(base_url, "member.php?mod=logging&action=login&loginsubmit=yes&infloat=yes&lssubmit=yes&inajax=1")
            login_data = {
                "formhash": formhash,
                "username": username,
                "password": password,
                "quickforward": "yes",
                "handlekey": "ls"
            }
            logger.info(f"[SSB 登录] 提交登录请求...")
            async with session.post(login_post_url, data=login_data, headers=self.headers, timeout=15, ssl=False) as resp:
                await resp.read() # 确保读取

            # 3. 校验登录状态
            check_url = urljoin(base_url, "home.php?mod=spacecp")
            async with session.get(check_url, headers=self.headers, timeout=15, ssl=False) as resp:
                final_url = str(resp.url)
                html = await self._get_text(resp)
                if "登录" not in final_url and username in html:
                    logger.info(f"[SSB 登录] 登录验证成功: {username}")
                    cookies = {c.key: c.value for c in session.cookie_jar}
                    self._save_ssb_cookies(username, cookies)
                    return True
                else:
                    logger.error(f"[SSB 登录] 登录验证失败。URL: {final_url}, 用户名是否存在: {username in html}")
        except Exception as e:
            logger.error(f"[SSB 登录] 异常: {e}")
        return False

    @filter.command("ssb", alias={'搜书吧'})
    async def ssb_command(self, event: AstrMessageEvent):
        """获取搜书吧的网址或搜索书籍"""
        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            # 获取网址逻辑
            async with aiohttp.ClientSession() as session:
                for domain_url in self.target_domains:
                    link_url = await self._extract_link_from_url(session, domain_url)
                    if link_url:
                        yield event.plain_result(f" 成功找到搜书吧最新网址：\n{link_url}")
                        return
            yield event.plain_result("❌ 抱歉，所有导航网站均无法访问或未找到可用链接。")
            return

        # 搜索逻辑
        keyword = args[1]
        ssb_auth = self.plugin_config.get("ssb_auth", "")
        if not ssb_auth or "&" not in ssb_auth:
            yield event.plain_result(" 请先在插件配置中设置 ssb_auth (格式: 账号&密码)。")
            return
        
        username, password = ssb_auth.split("&", 1)
        yield event.plain_result(f"🔍 正在搜书吧搜索: {keyword}...")

        async with aiohttp.ClientSession() as session:
            try:
                # 1. 获取最新 base_url
                base_url = None
                for domain_url in self.target_domains:
                    base_url = await self._extract_link_from_url(session, domain_url)
                    if base_url: break
                
                if not base_url:
                    yield event.plain_result(" 无法获取搜书吧最新网址，请稍后再试。")
                    return
                
                parsed = urlparse(base_url)
                base_url = f"{parsed.scheme}://{parsed.netloc}/"
                logger.info(f"[SSB 搜索] 使用 Base URL: {base_url}")

                # 2. 加载 Cookie 并校验
                cookies = self._load_ssb_cookies(username)
                if cookies:
                    session.cookie_jar.update_cookies(cookies)
                    logger.info(f"[SSB 搜索] 已加载账号 {username} 的历史 Cookie")
                
                # 校验登录状态
                check_url = urljoin(base_url, "home.php?mod=spacecp")
                is_logged_in = False
                try:
                    async with session.get(check_url, headers=self.headers, timeout=10, ssl=False) as resp:
                        final_url = str(resp.url)
                        html = await self._get_text(resp)
                        if "登录" not in final_url and username in html:
                            is_logged_in = True
                            logger.info(f"[SSB 搜索] Cookie 验证有效: {username}")
                except Exception as e: 
                    logger.warning(f"[SSB 搜索] Cookie 验证异常: {e}")

                if not is_logged_in:
                    logger.info(f"[SSB 搜索] Cookie 失效或未登录，尝试登录: {username}")
                    if not await self._ssb_login(session, base_url, username, password):
                        yield event.plain_result(" 搜书吧登录失败，请检查账密配置。")
                        return

                # 3. 搜索
                search_url = urljoin(base_url, "search.php?mod=forum")
                
                # 获取 formhash
                formhash = ""
                async with session.get(search_url, headers=self.headers, timeout=10, ssl=False) as resp:
                    html = await self._get_text(resp)
                    fh_match = re.search(r'name="formhash" value="([a-f0-9]+)"', html)
                    if fh_match: formhash = fh_match.group(1)
                
                logger.info(f"[SSB 搜索] 获取搜索页 formhash: {formhash}")

                search_params = {
                    'mod': 'forum',
                    'searchsubmit': 'yes',
                    'srchtxt': keyword,
                    'formhash': formhash
                }
                encoded_data = urlencode(search_params, encoding='gbk')
                
                search_headers = self.headers.copy()
                search_headers['Referer'] = search_url
                search_headers['Content-Type'] = 'application/x-www-form-urlencoded'
                
                logger.info(f"[SSB 搜索] 发送搜索 POST 请求, 关键词: {keyword}")
                async with session.post(search_url, data=encoded_data, headers=search_headers, timeout=15, ssl=False) as p_resp:
                    html = await self._get_text(p_resp)
                    final_search_url = str(p_resp.url)
                    logger.info(f"[SSB 搜索] 搜索响应 URL: {final_search_url}, 长度: {len(html)}")

                if "未找到符合条件的搜索结果" in html:
                    yield event.plain_result(f" 未找到与 {keyword} 相关的结果。")
                    return

                # 4. 解析结果
                soup = BeautifulSoup(html, 'lxml')
                items = soup.select('div#threadlist ul li.pbw')
                logger.info(f"[SSB 搜索] 解析到 {len(items)} 条结果")

                if not items:
                    if "验证码" in html or "secqaa" in html:
                        yield event.plain_result(" 搜索触发了验证码，请稍后再试。")
                    else:
                        yield event.plain_result(" 无法获取搜索结果，可能是被拦截或解析失败。")
                    return

                results = []
                for i, item in enumerate(items[:self.search_result_count], 1):
                    title_el = item.select_one('h3.xs3 a')
                    if not title_el: continue
                    
                    title = "".join(title_el.find_all(string=True, recursive=True)).strip()
                    link = urljoin(base_url, title_el['href'])
                    
                    time_text = "未知"
                    time_span = item.select_one('p span')
                    if time_span:
                        time_text = time_span.get_text(strip=True)
                    
                    results.append(f"【{i}】{title}\n📅 时间: {time_text}\n🔗 {link}")

                reply = f"✅ 为您找到以下关于 “{keyword}” 的结果：\n\n" + "\n\n".join(results)
                yield event.plain_result(reply)

            except Exception as e:
                logger.error(f"[SSB 搜索] 出错: {e}")
                yield event.plain_result(f" 搜索过程中发生错误: {str(e)}")

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
                for i, item in enumerate(items[:self.search_result_count], 1):
                    title_el = item.select_one('h3.xs3 a')
                    if not title_el: continue
                    
                    title = "".join(title_el.find_all(string=True, recursive=True)).strip()
                    link = urljoin(f"https://{host}/", title_el['href'])
                    
                    # 提取时间
                    time_text = "未知"
                    time_span = item.select_one('p span') # Discuz 搜索页通常第一个 span 是时间
                    if time_span:
                        time_text = time_span.get_text(strip=True)
                    
                    results.append(f"【{i}】{title}\n📅 时间: {time_text}\n🔗 {link}")

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