import datetime
import json
import os
import re
import time
import asyncio
from typing import Optional, Iterable
from urllib.parse import urljoin, urlparse, urlencode

import aiohttp
from bs4 import BeautifulSoup
from astrbot.api import logger

from .url_resolver import UrlResolver
from .cache import SsbSearchItem
from .http_session import ProxyError


class SearchService:
    SSB_LOGIN_RETRY_ATTEMPTS = 2
    SSB_LOGIN_RETRY_DELAY = 1.0
    SXSY_SEARCH_RETRY_ATTEMPTS = 2
    SXSY_SEARCH_RETRY_DELAY = 1.0

    def __init__(
        self,
        headers: dict,
        auth_cfg: dict,
        search_result_count: int,
        ssb_cookie_file: str,
        url_resolver: UrlResolver,
    ):
        self.headers = headers
        self.auth_cfg = auth_cfg or {}
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

    def _extract_formhash(self, html: str) -> str:
        match = re.search(r'name=["\']formhash["\']\s+value=["\']([a-f0-9]+)', html)
        return match.group(1) if match else ""

    def _extract_page_title(self, html: str) -> str:
        match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        return re.sub(r"\s+", " ", match.group(1)).strip()

    def _looks_like_ssb_login_page(self, html: str, final_url: str = "") -> bool:
        lowered = html.lower()
        final_lower = final_url.lower()
        return (
            "member.php?mod=logging&action=login" in final_lower
            or "<title>登录 -" in html
            or 'class="pg_logging"' in lowered
            or 'name="loginfield"' in lowered
            or 'id="lsform"' in lowered
        )

    def _get_ssb_login_failure_message(
        self,
        reason: str,
        *,
        title: str = "",
        status: int | None = None,
        final_url: str = "",
    ) -> str:
        if reason == "login_page_status":
            return f"搜书吧登录页访问异常（HTTP {status or '未知'}），请稍后重试。"
        if reason == "login_page_missing_formhash":
            title_hint = f"当前页面标题：{title}。" if title else ""
            return (
                "搜书吧登录页异常，未获取到 formhash，可能是站点临时异常、风控页或页面结构变化，请稍后重试。"
                + title_hint
            )
        if reason == "login_failed_bad_credentials":
            return "搜书吧登录失败，账号或密码可能不正确，请检查 ssb_auth 配置。"
        if reason == "login_failed_captcha":
            return "搜书吧登录失败，站点可能触发了验证码或风控校验，请稍后重试。"
        if reason == "login_failed_still_on_login":
            title_hint = f"当前页面标题：{title}。" if title else ""
            return (
                "搜书吧登录未通过，站点仍停留在登录页，可能是临时风控、Cookie 状态异常或账密错误。"
                + title_hint
            )
        if reason == "login_failed_unexpected_page":
            where = f" 返回地址：{final_url}。" if final_url else ""
            return (
                "搜书吧登录后返回了异常页面，暂时无法确认是否为账密问题，请稍后重试。"
                + where
            )
        if reason == "login_connection_error":
            return "搜书吧网站当前无法访问，无法执行登录操作。"
        return "搜书吧登录失败，原因未明，请稍后重试。"

    def _classify_ssb_login_verification_failure(
        self,
        final_url: str,
        html: str,
        username: str,
    ) -> tuple[str, str]:
        lowered = html.lower()
        title = self._extract_page_title(html)
        if any(
            token in html
            for token in ("密码错误", "登录失败", "帐号或密码错误", "账号或密码错误")
        ):
            return (
                "login_failed_bad_credentials",
                self._get_ssb_login_failure_message(
                    "login_failed_bad_credentials", title=title, final_url=final_url
                ),
            )
        if "secqaa" in lowered or "验证码" in html:
            return (
                "login_failed_captcha",
                self._get_ssb_login_failure_message(
                    "login_failed_captcha", title=title, final_url=final_url
                ),
            )
        if self._looks_like_ssb_login_page(html, final_url):
            return (
                "login_failed_still_on_login",
                self._get_ssb_login_failure_message(
                    "login_failed_still_on_login", title=title, final_url=final_url
                ),
            )
        return (
            "login_failed_unexpected_page",
            self._get_ssb_login_failure_message(
                "login_failed_unexpected_page", title=title, final_url=final_url
            ),
        )

    def _should_retry_ssb_login_reason(self, reason: str) -> bool:
        return reason in {
            "login_page_status",
            "login_page_missing_formhash",
            "login_failed_unexpected_page",
        }

    def _get_search_result_download_tip(self, command: str) -> str:
        allow_users = self._allow_users
        is_admin_only = "0" in allow_users
        tip = f"⬇️ 可继续发送 /{command} 序号 下载对应附件。"
        if is_admin_only:
            tip += " 当前下载功能仅管理员可用。"
        return tip

    def _append_search_result_tip(self, reply: str, command: str) -> str:
        tip = self._get_search_result_download_tip(command)
        if not tip:
            return reply
        header_end = reply.find("\n\u200b\n")
        if header_end != -1:
            header = reply[:header_end]
            results = reply[header_end:]
            return f"{header}\n\u200b\n{tip}{results}"
        return f"{reply}\n\u200b\n{tip}"

    def _looks_like_sxsy_login_page(self, html: str, final_url: str = "") -> bool:
        lowered = html.lower()
        final_lower = final_url.lower()
        return (
            "member.php?mod=logging&action=login" in final_lower
            or "<title>登录 -" in html
            or 'class="pg_logging"' in lowered
            or 'name="loginfield"' in lowered
            or 'id="lsform"' in lowered
        )

    def _get_sxsy_failure_message(
        self,
        reason: str,
        *,
        title: str = "",
        status: int | None = None,
        final_url: str = "",
    ) -> str:
        if reason == "search_page_status":
            return f"❌ 尚香书苑搜索页访问异常（HTTP {status or '未知'}），请稍后重试"
        if reason == "search_page_missing_formhash":
            title_hint = f" 当前页面标题：{title}。" if title else ""
            return (
                "❌ 尚香书苑搜索页异常，未获取到 formhash，可能是站点临时异常、风控页或页面结构变化，请稍后重试。"
                + title_hint
            )
        if reason == "search_post_status":
            return f"❌ 尚香书苑搜索请求异常（HTTP {status or '未知'}），请稍后重试"
        if reason == "cookie_expired":
            return "❌ 尚香书苑 Cookie 已失效或未登录，请更新 CK。"
        if reason == "search_captcha":
            return "❌ 尚香书苑搜索可能触发了验证码或风控校验，请稍后重试。"
        if reason == "search_unexpected_page":
            where = f" 返回地址：{final_url}。" if final_url else ""
            return "❌ 尚香书苑返回了异常页面，暂时无法完成搜索，请稍后重试。" + where
        if reason == "search_network_error":
            return "❌ 尚香书苑访问超时或网络错误"
        if reason == "search_post_error":
            return "❌ 尚香书苑搜索请求失败，请稍后重试"
        if reason == "sxsy_connection_error":
            return "❌ 尚香书苑网站当前无法访问，可能是网址已更换或网络不可达。"
        return "❌ 尚香书苑搜索失败，请稍后重试"

    def _classify_sxsy_search_page(self, html: str, final_url: str) -> tuple[str, str]:
        lowered = html.lower()
        title = self._extract_page_title(html)
        if self._looks_like_sxsy_login_page(html, final_url):
            return (
                "cookie_expired",
                self._get_sxsy_failure_message(
                    "cookie_expired", title=title, final_url=final_url
                ),
            )
        if "secqaa" in lowered or "验证码" in html:
            return (
                "search_captcha",
                self._get_sxsy_failure_message(
                    "search_captcha", title=title, final_url=final_url
                ),
            )
        if (
            "对不起，没有找到匹配结果。" in html
            or "相关内容 0 个" in html
            or "searchid=" in final_url.lower()
            or 'id="threadlist"' in lowered
            or 'class="slst"' in lowered
        ):
            return ("ok", "")
        return (
            "search_unexpected_page",
            self._get_sxsy_failure_message(
                "search_unexpected_page", title=title, final_url=final_url
            ),
        )

    def _should_retry_sxsy_reason(self, reason: str) -> bool:
        return reason in {
            "search_page_status",
            "search_page_missing_formhash",
            "search_post_status",
            "search_unexpected_page",
            "search_network_error",
            "search_post_error",
        }

    async def _ssb_login(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        username: str,
        password: str,
    ) -> tuple[bool, str]:
        """参考 ssb.py 的登录逻辑"""
        try:
            login_url = urljoin(base_url, "member.php?mod=logging&action=login")
            login_post_url = urljoin(
                base_url,
                "member.php?mod=logging&action=login&loginsubmit=yes&infloat=yes&lssubmit=yes&inajax=1",
            )
            check_url = urljoin(base_url, "home.php?mod=spacecp")

            for attempt in range(1, self.SSB_LOGIN_RETRY_ATTEMPTS + 1):
                logger.debug(
                    f"[SSB 登录] 开始登录流程: {username} @ {base_url} (attempt {attempt}/{self.SSB_LOGIN_RETRY_ATTEMPTS})"
                )
                async with session.get(
                    login_url, headers=self.headers, timeout=15, ssl=False
                ) as resp:
                    final_url = str(resp.url)
                    html = await self.url_resolver.get_text(resp)
                    if resp.status != 200:
                        message = self._get_ssb_login_failure_message(
                            "login_page_status", status=resp.status, final_url=final_url
                        )
                        logger.warning(
                            f"[SSB 登录] 登录页状态异常: status={resp.status}, url={final_url}"
                        )
                        if (
                            attempt < self.SSB_LOGIN_RETRY_ATTEMPTS
                            and self._should_retry_ssb_login_reason("login_page_status")
                        ):
                            await asyncio.sleep(self.SSB_LOGIN_RETRY_DELAY)
                            continue
                        return False, message

                    formhash = self._extract_formhash(html)
                    if not formhash:
                        title = self._extract_page_title(html)
                        message = self._get_ssb_login_failure_message(
                            "login_page_missing_formhash",
                            title=title,
                            final_url=final_url,
                        )
                        logger.warning(
                            f"[SSB 登录] 登录页未获取到 formhash: title={title or 'N/A'}, url={final_url}, len={len(html)}"
                        )
                        if (
                            attempt < self.SSB_LOGIN_RETRY_ATTEMPTS
                            and self._should_retry_ssb_login_reason(
                                "login_page_missing_formhash"
                            )
                        ):
                            await asyncio.sleep(self.SSB_LOGIN_RETRY_DELAY)
                            continue
                        return False, message

                    logger.debug(f"[SSB 登录] 获取到 formhash: {formhash}")

                login_data = {
                    "formhash": formhash,
                    "username": username,
                    "password": password,
                    "quickforward": "yes",
                    "handlekey": "ls",
                }
                logger.debug("[SSB 登录] 提交登录请求...")
                async with session.post(
                    login_post_url,
                    data=login_data,
                    headers=self.headers,
                    timeout=15,
                    ssl=False,
                ) as resp:
                    await resp.read()

                async with session.get(
                    check_url, headers=self.headers, timeout=15, ssl=False
                ) as resp:
                    final_url = str(resp.url)
                    html = await self.url_resolver.get_text(resp)
                    if "登录" not in final_url and username in html:
                        logger.debug(f"[SSB 登录] 登录验证成功: {username}")
                        cookies = {c.key: c.value for c in session.cookie_jar}
                        self._save_ssb_cookies(username, cookies)
                        return True, ""

                    reason, message = self._classify_ssb_login_verification_failure(
                        final_url, html, username
                    )
                    logger.warning(
                        f"[SSB 登录] 登录验证失败: reason={reason}, url={final_url}, username_found={username in html}"
                    )
                    if (
                        attempt < self.SSB_LOGIN_RETRY_ATTEMPTS
                        and self._should_retry_ssb_login_reason(reason)
                    ):
                        await asyncio.sleep(self.SSB_LOGIN_RETRY_DELAY)
                        continue
                    return False, message
        except ProxyError:
            raise
        except aiohttp.ClientConnectorError as e:
            logger.error(f"[SSB 登录] 连接异常: {e}")
            return False, self._get_ssb_login_failure_message("login_connection_error")
        except Exception as e:
            logger.error(f"[SSB 登录] 异常: {e}")
        return False, "搜书吧登录时发生异常，请稍后重试。"

    async def find_ssb_latest_url(
        self, session: aiohttp.ClientSession, target_domains: list[str]
    ) -> Optional[str]:
        for domain_url in target_domains:
            link_url = await self.url_resolver.extract_link_from_url(
                session, domain_url
            )
            if link_url:
                return link_url
        return None

    async def ssb_search(
        self,
        session: aiohttp.ClientSession,
        keyword: str,
        target_domains: list[str],
    ) -> tuple[bool, str, list[SsbSearchItem]]:
        ssb_auth = self.auth_cfg.get("ssb", "")
        if not ssb_auth or "&" not in ssb_auth:
            return False, " 请先在插件配置中设置 ssb_auth (格式: 账号&密码)。", []

        username, password = ssb_auth.split("&", 1)

        try:
            base_url = await self.find_ssb_latest_url(session, target_domains)
        except ProxyError:
            raise
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
            login_ok, login_message = await self._ssb_login(
                session, base_url, username, password
            )
            if not login_ok:
                return False, f" {login_message}", []

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
            logger.debug(f"[SSB 搜索] 搜索响应 URL: {p_resp.url}, 长度: {len(html)}")

        if "对不起，没有找到匹配结果。" in html:
            return False, f"📦 搜书吧未找到与 “{keyword}” 相关的搜索结果。", []

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

        reply = (
            f"✅ 为您找到以下关于 “{keyword}” 的结果：\n\u200b\n"
            + "\n\u200b\n".join(results)
        )
        reply = self._append_search_result_tip(reply, "ssb")
        return True, reply, items_out

    async def sxsy_search(
        self, session: aiohttp.ClientSession, keyword: str
    ) -> tuple[bool, str, list[SsbSearchItem]]:
        cookie = (
            ((self.auth_cfg.get("sxsy", {}) or {}).get("cookie", "") or "")
            if self.auth_cfg
            else ""
        )
        if not cookie:
            return False, "❌ 请先在插件配置中设置尚香书苑 Cookie。", []

        base_url = self.url_resolver.get_sxsy_search_base_url()
        if not base_url:
            return False, "❌ 请先在插件配置中设置尚香书苑网址后再搜索。", []

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            "Cookie": cookie,
            "Referer": urljoin(base_url, "search.php?mod=forum"),
        }
        post_url = urljoin(base_url, "search.php?mod=forum")

        html = ""
        last_error_message = ""
        for attempt in range(1, self.SXSY_SEARCH_RETRY_ATTEMPTS + 1):
            formhash = ""
            try:
                async with session.get(
                    post_url, headers=headers, timeout=10, ssl=False
                ) as f_resp:
                    final_url = str(f_resp.url)
                    if f_resp.status != 200:
                        last_error_message = self._get_sxsy_failure_message(
                            "search_page_status",
                            status=f_resp.status,
                            final_url=final_url,
                        )
                        logger.warning(
                            f"[sxsy 搜索] 搜索页状态异常: status={f_resp.status}, url={final_url}, attempt={attempt}/{self.SXSY_SEARCH_RETRY_ATTEMPTS}"
                        )
                        if (
                            attempt < self.SXSY_SEARCH_RETRY_ATTEMPTS
                            and self._should_retry_sxsy_reason("search_page_status")
                        ):
                            await asyncio.sleep(self.SXSY_SEARCH_RETRY_DELAY)
                            continue
                        return False, last_error_message, []
                    f_html = await self.url_resolver.get_text(f_resp)
                    formhash = self._extract_formhash(f_html)
                    if not formhash:
                        title = self._extract_page_title(f_html)
                        last_error_message = self._get_sxsy_failure_message(
                            "search_page_missing_formhash",
                            title=title,
                            final_url=final_url,
                        )
                        logger.warning(
                            f"[sxsy 搜索] 搜索页未获取到 formhash: title={title or 'N/A'}, url={final_url}, len={len(f_html)}, attempt={attempt}/{self.SXSY_SEARCH_RETRY_ATTEMPTS}"
                        )
                        if (
                            attempt < self.SXSY_SEARCH_RETRY_ATTEMPTS
                            and self._should_retry_sxsy_reason(
                                "search_page_missing_formhash"
                            )
                        ):
                            await asyncio.sleep(self.SXSY_SEARCH_RETRY_DELAY)
                            continue
                        return False, last_error_message, []
            except ProxyError:
                raise
            except aiohttp.ClientConnectorError as e:
                logger.error(f"[sxsy] 连接异常（获取 formhash）: {e}")
                return (
                    False,
                    self._get_sxsy_failure_message("sxsy_connection_error"),
                    [],
                )
            except Exception as e:
                logger.error(f"[sxsy] 获取 formhash 异常: {e}")
                last_error_message = self._get_sxsy_failure_message(
                    "search_network_error"
                )
                if (
                    attempt < self.SXSY_SEARCH_RETRY_ATTEMPTS
                    and self._should_retry_sxsy_reason("search_network_error")
                ):
                    await asyncio.sleep(self.SXSY_SEARCH_RETRY_DELAY)
                    continue
                return False, last_error_message, []

            post_data = {
                "mod": "forum",
                "searchsubmit": "yes",
                "srchtxt": keyword,
                "formhash": formhash,
            }

            logger.debug(
                f"[sxsy 搜索] 尝试 POST 搜索: {post_url} (attempt {attempt}/{self.SXSY_SEARCH_RETRY_ATTEMPTS})"
            )
            try:
                async with session.post(
                    post_url, data=post_data, headers=headers, timeout=15, ssl=False
                ) as p_resp:
                    final_url = str(p_resp.url)
                    if p_resp.status != 200:
                        last_error_message = self._get_sxsy_failure_message(
                            "search_post_status",
                            status=p_resp.status,
                            final_url=final_url,
                        )
                        logger.warning(
                            f"[sxsy 搜索] POST 搜索请求状态异常: status={p_resp.status}, url={final_url}, attempt={attempt}/{self.SXSY_SEARCH_RETRY_ATTEMPTS}"
                        )
                        if (
                            attempt < self.SXSY_SEARCH_RETRY_ATTEMPTS
                            and self._should_retry_sxsy_reason("search_post_status")
                        ):
                            await asyncio.sleep(self.SXSY_SEARCH_RETRY_DELAY)
                            continue
                        return False, last_error_message, []
                    html = await self.url_resolver.get_text(p_resp)
                    logger.debug(
                        f"[sxsy 搜索] POST 响应 URL: {p_resp.url}, 长度: {len(html)}"
                    )
            except ProxyError:
                raise
            except aiohttp.ClientConnectorError as e:
                logger.error(f"[sxsy 搜索] POST 连接异常: {e}")
                return (
                    False,
                    self._get_sxsy_failure_message("sxsy_connection_error"),
                    [],
                )
            except Exception as e:
                logger.error(f"[sxsy 搜索] POST 请求异常: {e}")
                last_error_message = self._get_sxsy_failure_message("search_post_error")
                if (
                    attempt < self.SXSY_SEARCH_RETRY_ATTEMPTS
                    and self._should_retry_sxsy_reason("search_post_error")
                ):
                    await asyncio.sleep(self.SXSY_SEARCH_RETRY_DELAY)
                    continue
                return False, last_error_message, []

            reason, message = self._classify_sxsy_search_page(html, final_url)
            if reason == "cookie_expired":
                return False, message, []
            if reason == "search_captcha":
                return False, message, []
            if reason == "search_unexpected_page":
                logger.warning(
                    f"[sxsy 搜索] 返回异常页面: url={final_url}, attempt={attempt}/{self.SXSY_SEARCH_RETRY_ATTEMPTS}"
                )
                if (
                    attempt < self.SXSY_SEARCH_RETRY_ATTEMPTS
                    and self._should_retry_sxsy_reason("search_unexpected_page")
                ):
                    await asyncio.sleep(self.SXSY_SEARCH_RETRY_DELAY)
                    continue
                return False, message, []
            break

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

        reply = (
            f"✅ 为您找到以下关于 “{keyword}” 的结果：\n\u200b\n"
            + "\n\u200b\n".join(results)
        )
        reply = self._append_search_result_tip(reply, "sxsy")
        return True, reply, items_out
