import json
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlencode
from typing import List, Dict, Optional, Callable, Awaitable
import os
import re
import datetime

import time
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger

@register(
    "astrbot_plugin_soushuba",
    "Foolllll",
    "搜书吧助手",
    "1.2",
    "https://github.com/Foolllll-J/astrbot_plugin_soushuba",
)
class SoushuBaLinkExtractorPlugin(Star):
    MONITOR_SUBSCRIBERS_KEY = "status_monitor_subscribers"

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

        self.data_dir = StarTools.get_data_dir("astrbot_plugin_soushuba")
        os.makedirs(self.data_dir, exist_ok=True)
        self.ssb_cookie_file = os.path.join(self.data_dir, "ssb_cookies.json")
        self.last_ssb_search_time = 0
        self._monitor_stop_event = asyncio.Event()
        self._monitor_task: Optional[asyncio.Task] = None
        self._monitor_subscribers: Dict[str, List[str]] = {"ssb": [], "sxsy": []}
        self._monitor_states: Dict[str, Dict[str, object]] = {
            "ssb": {"failed": False, "real_url": None},
            "sxsy": {"failed": False, "real_url": None},
        }

    async def initialize(self):
        await self._load_monitor_subscribers()
        self._monitor_stop_event.clear()
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(
            f"[状态监控] 后台任务已启动，检测间隔: {self.monitor_check_interval}s, "
            f"搜书吧订阅: {len(self._monitor_subscribers['ssb'])}, "
            f"尚香书苑订阅: {len(self._monitor_subscribers['sxsy'])}"
        )

    async def _load_monitor_subscribers(self):
        data = await self.get_kv_data(self.MONITOR_SUBSCRIBERS_KEY, {})
        if not isinstance(data, dict):
            return
        for site_key in ("ssb", "sxsy"):
            sessions = data.get(site_key, [])
            if isinstance(sessions, list):
                normalized = [str(s).strip() for s in sessions if str(s).strip()]
                # 去重并保留顺序
                self._monitor_subscribers[site_key] = list(dict.fromkeys(normalized))

    async def _save_monitor_subscribers(self):
        await self.put_kv_data(self.MONITOR_SUBSCRIBERS_KEY, self._monitor_subscribers)

    async def _set_site_monitor_subscription(self, site_key: str, session: str, enable: bool) -> bool:
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

    def _normalize_base_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/"
        return url

    async def _resolve_ssb_monitor_url(self, session: aiohttp.ClientSession) -> Optional[str]:
        for domain_url in self.target_domains:
            link_url = await self._extract_link_from_url(session, domain_url)
            if link_url:
                return self._normalize_base_url(link_url)
        return None

    async def _resolve_sxsy_monitor_url(self, session: aiohttp.ClientSession) -> Optional[str]:
        host = "sxsy87.com"
        try:
            async with session.get("https://sxsy.org/", headers=self.headers, timeout=10, ssl=False) as response:
                if response.status == 200:
                    text = await self._get_text(response)
                    match = re.search(r'href="https://([^"]+)"', text)
                    if match:
                        host = match.group(1).strip()
        except Exception as e:
            logger.warning(f"[状态监控] 获取尚香书苑真实地址失败: {e}")
        if not host:
            return None
        return self._normalize_base_url(f"https://{host}/")

    def _check_ssb_page_health(self, html: str) -> Optional[str]:
        content = html.lower()
        ssb_error_signs = [
            "database error",
            "discuz! database error",
            "mysql error",
            "sql syntax",
            "table './",
            "db_driver_mysqli",
        ]
        for sign in ssb_error_signs:
            if sign in content:
                return "检测到数据库错误页面"
        return None

    def _check_sxsy_page_health(self, html: str) -> Optional[str]:
        content = html.lower()
        ngx_default_signs = [
            "welcome to nginx",
            "test page for the nginx",
            "if you see this page, the nginx web server is successfully installed",
        ]
        for sign in ngx_default_signs:
            if sign in content:
                return "检测到 nginx 默认页面"

        sxsy_healthy_signs = [
            "尚香书苑",
            "powered by discuz",
            "forum.php",
            "search.php",
            "member.php",
            "discuz!",
        ]
        has_healthy_sign = any(sign in content for sign in sxsy_healthy_signs)
        if "nginx" in content and not has_healthy_sign:
            return "检测到代理异常页面"
        if not has_healthy_sign and len(content.strip()) < 1200:
            return "页面内容异常"
        return None

    async def _probe_site_access(
        self,
        session: aiohttp.ClientSession,
        url: str,
        site_key: str,
    ) -> tuple[bool, str, str]:
        try:
            async with session.get(
                url,
                headers=self.headers,
                timeout=15,
                ssl=False,
                allow_redirects=True,
            ) as response:
                final_url = str(response.url) if response.url else url
                if 200 <= response.status < 400:
                    html_content = await self._get_text(response)
                    content_error = None
                    if site_key == "ssb":
                        content_error = self._check_ssb_page_health(html_content)
                    elif site_key == "sxsy":
                        content_error = self._check_sxsy_page_health(html_content)
                    if content_error:
                        return False, content_error, self._normalize_base_url(final_url)
                    return True, f"HTTP {response.status}", self._normalize_base_url(final_url)
                return False, f"HTTP {response.status}", self._normalize_base_url(final_url)
        except asyncio.TimeoutError:
            return False, "请求超时", self._normalize_base_url(url)
        except Exception as e:
            return False, str(e), self._normalize_base_url(url)

    async def _check_site_status(
        self,
        site_key: str,
        session: aiohttp.ClientSession,
        resolver: Callable[[aiohttp.ClientSession], Awaitable[Optional[str]]],
    ) -> tuple[bool, str, Optional[str]]:
        state = self._monitor_states[site_key]
        current_url = state.get("real_url")
        last_error = ""

        if isinstance(current_url, str) and current_url:
            ok, detail, final_url = await self._probe_site_access(session, current_url, site_key)
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
            ok, detail, final_url = await self._probe_site_access(session, resolved_url, site_key)
            if ok:
                state["real_url"] = final_url
                return True, detail, final_url
            last_error = f"{final_url} -> {detail}"

        return False, last_error or "访问失败", self._normalize_base_url(str(resolved_url))

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

        async with aiohttp.ClientSession() as session:
            if has_ssb_subscribers:
                ok, detail, real_url = await self._check_site_status(
                    "ssb",
                    session,
                    self._resolve_ssb_monitor_url,
                )
                await self._handle_site_transition("ssb", "搜书吧", ok, detail, real_url)

            if has_sxsy_subscribers:
                ok, detail, real_url = await self._check_site_status(
                    "sxsy",
                    session,
                    self._resolve_sxsy_monitor_url,
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
            logger.debug(f"[SSB] 账号 {username} 的 Cookie 已保存")
        except Exception as e:
            logger.error(f"保存 SSB Cookie 失败: {e}")

    async def _ssb_login(self, session, base_url: str, username, password):
        """参考 ssb.py 的登录逻辑"""
        try:
            logger.debug(f"[SSB 登录] 开始登录流程: {username} @ {base_url}")
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
                logger.debug(f"[SSB 登录] 获取到 formhash: {formhash}")

            # 2. 提交登录
            login_post_url = urljoin(base_url, "member.php?mod=logging&action=login&loginsubmit=yes&infloat=yes&lssubmit=yes&inajax=1")
            login_data = {
                "formhash": formhash,
                "username": username,
                "password": password,
                "quickforward": "yes",
                "handlekey": "ls"
            }
            logger.debug(f"[SSB 登录] 提交登录请求...")
            async with session.post(login_post_url, data=login_data, headers=self.headers, timeout=15, ssl=False) as resp:
                await resp.read() # 确保读取

            # 3. 校验登录状态
            check_url = urljoin(base_url, "home.php?mod=spacecp")
            async with session.get(check_url, headers=self.headers, timeout=15, ssl=False) as resp:
                final_url = str(resp.url)
                html = await self._get_text(resp)
                if "登录" not in final_url and username in html:
                    logger.debug(f"[SSB 登录] 登录验证成功: {username}")
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
                        yield event.plain_result(f"📖 成功找到搜书吧最新网址：\n{link_url}")
                        return
            yield event.plain_result("❌ 抱歉，所有导航网站均无法访问或未找到可用链接。")
            return

        # 搜索逻辑
        keyword = args[1]
        
        # 40秒搜索限制
        current_time = time.time()
        if current_time - self.last_ssb_search_time < 40:
            yield event.plain_result("搜书吧在 40 秒内只能进行一次搜索")
            return
        
        ssb_auth = self.plugin_config.get("ssb_auth", "")
        if not ssb_auth or "&" not in ssb_auth:
            yield event.plain_result(" 请先在插件配置中设置 ssb_auth (格式: 账号&密码)。")
            return
        
        self.last_ssb_search_time = current_time # 更新上次搜索时间
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
                logger.debug(f"[SSB 搜索] 使用 Base URL: {base_url}")

                # 2. 加载 Cookie 并校验
                cookies = self._load_ssb_cookies(username)
                if cookies:
                    session.cookie_jar.update_cookies(cookies)
                    logger.debug(f"[SSB 搜索] 已加载账号 {username} 的历史 Cookie")
                
                # 校验登录状态
                check_url = urljoin(base_url, "home.php?mod=spacecp")
                is_logged_in = False
                try:
                    async with session.get(check_url, headers=self.headers, timeout=10, ssl=False) as resp:
                        final_url = str(resp.url)
                        html = await self._get_text(resp)
                        if "登录" not in final_url and username in html:
                            is_logged_in = True
                            logger.debug(f"[SSB 搜索] Cookie 验证有效: {username}")
                except Exception as e: 
                    logger.warning(f"[SSB 搜索] Cookie 验证异常: {e}")

                if not is_logged_in:
                    logger.debug(f"[SSB 搜索] Cookie 失效或未登录，尝试登录: {username}")
                    if not await self._ssb_login(session, base_url, username, password):
                        yield event.plain_result(" 搜书吧登录失败，请检查账密配置。")
                        return

                # 3. 搜索
                search_url = urljoin(base_url, "search.php?mod=forum")
                
                # 获取 formhash
                formhash = ""
                async with session.get(search_url, headers=self.headers, timeout=10, ssl=False) as resp:
                    if resp.status != 200:
                        logger.error(f"[SSB 搜索] 访问搜索页失败: {search_url}, Status: {resp.status}")
                        yield event.plain_result(f"❌ 搜书吧访问异常，请稍后重试")
                        return
                    html = await self._get_text(resp)
                    fh_match = re.search(r'name="formhash" value="([a-f0-9]+)"', html)
                    if fh_match: formhash = fh_match.group(1)
                
                logger.debug(f"[SSB 搜索] 获取搜索页 formhash: {formhash}")

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
                
                logger.debug(f"[SSB 搜索] 发送搜索 POST 请求, 关键词: {keyword}")
                async with session.post(search_url, data=encoded_data, headers=search_headers, timeout=15, ssl=False) as p_resp:
                    if p_resp.status != 200:
                        logger.error(f"[SSB 搜索] POST 搜索请求失败: {search_url}, Status: {p_resp.status}")
                        yield event.plain_result(f"❌ 搜书吧搜索请求失败，请稍后重试")
                        return
                    html = await self._get_text(p_resp)
                    final_search_url = str(p_resp.url)
                    logger.debug(f"[SSB 搜索] 搜索响应 URL: {final_search_url}, 长度: {len(html)}")

                if "对不起，没有找到匹配结果。" in html:
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
                        if f_resp.status != 200:
                            logger.error(f"[sxsy] 访问搜索页失败: {post_url}, Status: {f_resp.status}")
                            yield event.plain_result(f"❌ 尚香书苑访问异常，请稍后重试")
                            return
                        f_html = await self._get_text(f_resp)
                        fh_match = re.search(r'name="formhash" value="([a-f0-9]+)"', f_html)
                        if fh_match: formhash = fh_match.group(1)
                except Exception as e:
                    logger.error(f"[sxsy] 获取 formhash 异常: {e}")
                    yield event.plain_result(f"❌ 尚香书苑访问超时或网络错误")
                    return

                post_data = {
                    'mod': 'forum',
                    'searchsubmit': 'yes',
                    'srchtxt': keyword,
                    'formhash': formhash
                }

                # 3. 发送 POST 搜索
                logger.debug(f"[sxsy 搜索] 尝试 POST 搜索: {post_url}")
                async with session.post(post_url, data=post_data, headers=headers, timeout=15, ssl=False) as p_resp:
                    if p_resp.status != 200:
                        logger.error(f"[sxsy 搜索] POST 搜索请求失败: {post_url}, Status: {p_resp.status}")
                        yield event.plain_result(f"❌ 尚香书苑搜索请求失败，请稍后重试")
                        return
                    html = await self._get_text(p_resp)
                    logger.debug(f"[sxsy 搜索] POST 响应 URL: {p_resp.url}, 长度: {len(html)}")

                # 4. 检查异常状态
                # CK 失效特征：页面标题包含“登录”，或者 body 带有 pg_logging 类，或者包含特定的登录 action 链接
                if '<title>登录 -  尚香书苑  </title>' in html or 'class="pg_logging"' in html or 'member.php?mod=logging&action=login' in html:
                    yield event.plain_result("❌ Cookie 已失效或未登录，请更新CK。")
                    return
                
                # 搜索无结果特征：包含“对不起，没有找到匹配结果。”或者结果数为 0
                if "对不起，没有找到匹配结果。" in html or "相关内容 0 个" in html:
                    yield event.plain_result(f"📦 尚香书苑未找到与 “{keyword}” 相关的搜索结果。")
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

        changed = await self._set_site_monitor_subscription(site_key, session, enable)
        if enable:
            if changed:
                logger.info(
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
        """订阅或取消订阅尚香书苑状态监控（管理员）"""
        async for result in self._handle_monitor_command(event, "sxsy", "尚香书苑"):
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
        self._monitor_stop_event.set()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("搜书吧链接获取插件已卸载")
