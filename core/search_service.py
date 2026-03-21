import datetime
import json
import os
import re
import time
from typing import Optional, Iterable
from urllib.parse import urljoin, urlparse, urlencode

import aiohttp
from bs4 import BeautifulSoup
from astrbot.api import logger

from .url_resolver import UrlResolver
from .cache import SsbSearchItem


class SearchService:
    def __init__(
        self,
        headers: dict,
        plugin_config: dict,
        search_result_count: int,
        ssb_cookie_file: str,
        url_resolver: UrlResolver,
    ):
        self.headers = headers
        self.plugin_config = plugin_config or {}
        self.search_result_count = search_result_count
        self.ssb_cookie_file = ssb_cookie_file
        self.url_resolver = url_resolver
        self.last_ssb_search_time = 0
        self._allow_users: list[str] = []

    def set_access_control(
        self,
        allow_users: Iterable[str] | None,
    ) -> None:
        self._allow_users = [
            str(x).strip() for x in (allow_users or []) if str(x).strip()
        ]

    def is_download_allowed(self, user_id: str, is_admin: bool) -> bool:
        if is_admin:
            return True
        if not self._allow_users:
            return True
        if "0" in self._allow_users:
            return False
        return str(user_id) in set(self._allow_users)

    def check_ssb_rate_limit(self, interval_seconds: int = 40) -> bool:
        current_time = time.time()
        if current_time - self.last_ssb_search_time < interval_seconds:
            return False
        self.last_ssb_search_time = current_time
        return True

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
                    except Exception:
                        pass
            all_cookies[username] = {
                "cookies": cookies,
                "update_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(self.ssb_cookie_file, "w", encoding="utf-8") as f:
                json.dump(all_cookies, f, ensure_ascii=False, indent=2)
            logger.debug(f"[SSB] 账号 {username} 的 Cookie 已保存")
        except Exception as e:
            logger.error(f"保存 SSB Cookie 失败: {e}")

    async def _ssb_login(
        self, session: aiohttp.ClientSession, base_url: str, username: str, password: str
    ) -> bool:
        """参考 ssb.py 的登录逻辑"""
        try:
            logger.debug(f"[SSB 登录] 开始登录流程: {username} @ {base_url}")
            login_url = urljoin(base_url, "member.php?mod=logging&action=login")
            async with session.get(
                login_url, headers=self.headers, timeout=15, ssl=False
            ) as resp:
                html = await self.url_resolver.get_text(resp)
                soup = BeautifulSoup(html, "lxml")
                formhash_el = soup.find("input", {"name": "formhash"})
                if not formhash_el:
                    logger.error("[SSB 登录] 无法在登录页面获取 formhash")
                    return False
                formhash = formhash_el["value"]
                logger.debug(f"[SSB 登录] 获取到 formhash: {formhash}")

            login_post_url = urljoin(
                base_url,
                "member.php?mod=logging&action=login&loginsubmit=yes&infloat=yes&lssubmit=yes&inajax=1",
            )
            login_data = {
                "formhash": formhash,
                "username": username,
                "password": password,
                "quickforward": "yes",
                "handlekey": "ls",
            }
            logger.debug("[SSB 登录] 提交登录请求...")
            async with session.post(
                login_post_url, data=login_data, headers=self.headers, timeout=15, ssl=False
            ) as resp:
                await resp.read()

            check_url = urljoin(base_url, "home.php?mod=spacecp")
            async with session.get(
                check_url, headers=self.headers, timeout=15, ssl=False
            ) as resp:
                final_url = str(resp.url)
                html = await self.url_resolver.get_text(resp)
                if "登录" not in final_url and username in html:
                    logger.debug(f"[SSB 登录] 登录验证成功: {username}")
                    cookies = {c.key: c.value for c in session.cookie_jar}
                    self._save_ssb_cookies(username, cookies)
                    return True
                logger.error(
                    f"[SSB 登录] 登录验证失败。URL: {final_url}, 用户名是否存在: {username in html}"
                )
        except Exception as e:
            logger.error(f"[SSB 登录] 异常: {e}")
        return False

    async def find_ssb_latest_url(
        self, session: aiohttp.ClientSession, target_domains: list[str]
    ) -> Optional[str]:
        for domain_url in target_domains:
            link_url = await self.url_resolver.extract_link_from_url(session, domain_url)
            if link_url:
                return link_url
        return None

    async def ssb_search(
        self,
        session: aiohttp.ClientSession,
        keyword: str,
        target_domains: list[str],
    ) -> tuple[bool, str, list[SsbSearchItem]]:
        ssb_auth = self.plugin_config.get("ssb_auth", "")
        if not ssb_auth or "&" not in ssb_auth:
            return False, " 请先在插件配置中设置 ssb_auth (格式: 账号&密码)。", []

        username, password = ssb_auth.split("&", 1)

        base_url = await self.find_ssb_latest_url(session, target_domains)
        if not base_url:
            return False, " 无法获取搜书吧最新网址，请稍后再试。", []

        parsed = urlparse(base_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}/"
        logger.debug(f"[SSB 搜索] 使用 Base URL: {base_url}")

        cookies = self._load_ssb_cookies(username)
        if cookies:
            session.cookie_jar.update_cookies(cookies)
            logger.debug(f"[SSB 搜索] 已加载账号 {username} 的历史 Cookie")

        check_url = urljoin(base_url, "home.php?mod=spacecp")
        is_logged_in = False
        try:
            async with session.get(
                check_url, headers=self.headers, timeout=10, ssl=False
            ) as resp:
                final_url = str(resp.url)
                html = await self.url_resolver.get_text(resp)
                if "登录" not in final_url and username in html:
                    is_logged_in = True
                    logger.debug(f"[SSB 搜索] Cookie 验证有效: {username}")
        except Exception as e:
            logger.warning(f"[SSB 搜索] Cookie 验证异常: {e}")

        if not is_logged_in:
            logger.debug(f"[SSB 搜索] Cookie 失效或未登录，尝试登录: {username}")
            if not await self._ssb_login(session, base_url, username, password):
                return False, " 搜书吧登录失败，请检查账密配置。", []

        search_url = urljoin(base_url, "search.php?mod=forum")

        formhash = ""
        async with session.get(
            search_url, headers=self.headers, timeout=10, ssl=False
        ) as resp:
            if resp.status != 200:
                logger.error(
                    f"[SSB 搜索] 访问搜索页失败: {search_url}, Status: {resp.status}"
                )
                return False, "❌ 搜书吧访问异常，请稍后重试", []
            html = await self.url_resolver.get_text(resp)
            fh_match = re.search(r'name="formhash" value="([a-f0-9]+)"', html)
            if fh_match:
                formhash = fh_match.group(1)

        logger.debug(f"[SSB 搜索] 获取搜索页 formhash: {formhash}")

        search_params = {
            "mod": "forum",
            "searchsubmit": "yes",
            "srchtxt": keyword,
            "formhash": formhash,
        }
        encoded_data = urlencode(search_params, encoding="gbk")

        search_headers = self.headers.copy()
        search_headers["Referer"] = search_url
        search_headers["Content-Type"] = "application/x-www-form-urlencoded"

        logger.debug(f"[SSB 搜索] 发送搜索 POST 请求, 关键词: {keyword}")
        async with session.post(
            search_url, data=encoded_data, headers=search_headers, timeout=15, ssl=False
        ) as p_resp:
            if p_resp.status != 200:
                logger.error(
                    f"[SSB 搜索] POST 搜索请求失败: {search_url}, Status: {p_resp.status}"
                )
                return False, "❌ 搜书吧搜索请求失败，请稍后重试", []
            html = await self.url_resolver.get_text(p_resp)
            logger.debug(
                f"[SSB 搜索] 搜索响应 URL: {p_resp.url}, 长度: {len(html)}"
            )

        if "对不起，没有找到匹配结果。" in html:
            return False, f" 未找到与 {keyword} 相关的结果。", []

        soup = BeautifulSoup(html, "lxml")
        items = soup.select("div#threadlist ul li.pbw")
        logger.debug(f"[SSB 搜索] 解析到 {len(items)} 条结果")

        if not items:
            if "验证码" in html or "secqaa" in html:
                return False, " 搜索触发了验证码，请稍后再试。", []
            return False, " 无法获取搜索结果，可能是被拦截或解析失败。", []

        results = []
        items_out: list[SsbSearchItem] = []
        for i, item in enumerate(items[: self.search_result_count], 1):
            title_el = item.select_one("h3.xs3 a")
            if not title_el:
                continue

            title = "".join(title_el.find_all(string=True, recursive=True)).strip()
            link = urljoin(base_url, title_el["href"])

            time_text = "未知"
            time_span = item.select_one("p span")
            if time_span:
                time_text = time_span.get_text(strip=True)

            results.append(f"【{i}】{title}\n📅 时间: {time_text}\n🔗 {link}")
            items_out.append(SsbSearchItem(title=title, link=link, time_text=time_text))

        reply = f"✅ 为您找到以下关于 “{keyword}” 的结果：\n\n" + "\n\n".join(results)
        return True, reply, items_out

    async def sxsy_search(
        self, session: aiohttp.ClientSession, keyword: str
    ) -> tuple[bool, str, list[SsbSearchItem]]:
        cookie = self.plugin_config.get("sxsy_cookie", "") if self.plugin_config else ""
        if not cookie:
            return False, "❌ 请先在插件配置中设置 sxsy_cookie。", []

        base_url = self.url_resolver.get_sxsy_search_base_url()
        if not base_url:
            return False, "❌ 请先在插件配置中设置 sxsy_url（尚香书苑网址）后再搜索。", []

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            "Cookie": cookie,
            "Referer": urljoin(base_url, "search.php?mod=forum"),
        }
        post_url = urljoin(base_url, "search.php?mod=forum")

        formhash = ""
        try:
            async with session.get(
                post_url, headers=headers, timeout=10, ssl=False
            ) as f_resp:
                if f_resp.status != 200:
                    logger.error(
                        f"[sxsy] 访问搜索页失败: {post_url}, Status: {f_resp.status}"
                    )
                    return False, "❌ 尚香书苑访问异常，请稍后重试", []
                f_html = await self.url_resolver.get_text(f_resp)
                fh_match = re.search(r'name="formhash" value="([a-f0-9]+)"', f_html)
                if fh_match:
                    formhash = fh_match.group(1)
        except Exception as e:
            logger.error(f"[sxsy] 获取 formhash 异常: {e}")
            return False, "❌ 尚香书苑访问超时或网络错误", []

        post_data = {
            "mod": "forum",
            "searchsubmit": "yes",
            "srchtxt": keyword,
            "formhash": formhash,
        }

        logger.debug(f"[sxsy 搜索] 尝试 POST 搜索: {post_url}")
        async with session.post(
            post_url, data=post_data, headers=headers, timeout=15, ssl=False
        ) as p_resp:
            if p_resp.status != 200:
                logger.error(
                    f"[sxsy 搜索] POST 搜索请求失败: {post_url}, Status: {p_resp.status}"
                )
                return False, "❌ 尚香书苑搜索请求失败，请稍后重试", []
            html = await self.url_resolver.get_text(p_resp)
            logger.debug(f"[sxsy 搜索] POST 响应 URL: {p_resp.url}, 长度: {len(html)}")

        if (
            '<title>登录 -' in html
            or 'class="pg_logging"' in html
            or "member.php?mod=logging&action=login" in html
        ):
            return False, "❌ Cookie 已失效或未登录，请更新CK。", []

        if "对不起，没有找到匹配结果。" in html or "相关内容 0 个" in html:
            return False, f"📦 尚香书苑未找到与 “{keyword}” 相关的搜索结果。", []

        soup = BeautifulSoup(html, "lxml")
        items = soup.select("div#threadlist ul li.pbw") or soup.select(
            "div.slst ul li.pbw"
        )
        logger.debug(f"[sxsy 搜索] 解析到 {len(items)} 条结果")

        if not items:
            return False, "❌ 无法获取搜索结果，请检查 Cookie 是否过期。", []

        results = []
        items_out: list[SsbSearchItem] = []
        for i, item in enumerate(items[: self.search_result_count], 1):
            title_el = item.select_one("h3.xs3 a")
            if not title_el:
                continue

            title = "".join(title_el.find_all(string=True, recursive=True)).strip()
            link = urljoin(base_url, title_el["href"])

            time_text = "未知"
            time_span = item.select_one("p span")
            if time_span:
                time_text = time_span.get_text(strip=True)

            results.append(f"【{i}】{title}\n📅 时间: {time_text}\n🔗 {link}")
            items_out.append(SsbSearchItem(title=title, link=link, time_text=time_text))

        reply = f"✅ 为您找到以下关于 “{keyword}” 的结果：\n\n" + "\n\n".join(results)
        return True, reply, items_out
