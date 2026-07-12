import asyncio
import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup
from astrbot.api import logger

from .http_session import (
    ProxyError,
    is_definitely_proxy_error,
)


class UrlResolver:
    def __init__(self, headers: dict, auth_cfg: dict, target_domains: List[str]):
        self.headers = headers
        self.auth_cfg = auth_cfg or {}
        self.target_domains = target_domains

    def normalize_base_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/"
        return url

    def get_sxsy_search_base_url(self) -> Optional[str]:
        raw_url = str(
            ((self.auth_cfg.get("sxsy", {}) or {}).get("url", "") or "")
        ).strip()
        if not raw_url:
            return None
        if not raw_url.startswith(("http://", "https://")):
            raw_url = f"https://{raw_url}"
        parsed = urlparse(raw_url)
        if not parsed.scheme or not parsed.netloc:
            return None
        return f"{parsed.scheme}://{parsed.netloc}/"

    async def get_text(self, response: aiohttp.ClientResponse) -> str:
        """获取响应内容并处理编码问题"""
        content = await response.read()

        charset = response.charset
        if charset:
            try:
                return content.decode(charset)
            except Exception:
                pass

        for encoding in ["utf-8", "gbk", "gb2312", "big5"]:
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue

        return content.decode("utf-8", errors="ignore")

    async def extract_link_from_url(
        self, session: aiohttp.ClientSession, url: str
    ) -> Optional[str]:
        """尝试访问URL并提取指定链接。成功则返回链接，失败返回 None。"""
        try:
            ssl_verify = False if url.startswith("https://") else True

            async with session.get(url, timeout=20, ssl=ssl_verify) as response:
                final_url = str(response.url)
                html_content = await self.get_text(response)

            js_redirect_match = re.search(
                r"window\.location\.href\s*=\s*['\"](.*?)['\"];",
                html_content,
            )
            if js_redirect_match:
                redirect_target_url = urljoin(final_url, js_redirect_match.group(1))
                return await self.extract_link_from_url(session, redirect_target_url)

            meta_refresh_match = re.search(
                r"<meta http-equiv=\"refresh\" content=\"[\d\.]*;\s*url=(.*?)\"",
                html_content,
                re.IGNORECASE,
            )
            if meta_refresh_match:
                redirect_target_url = urljoin(final_url, meta_refresh_match.group(1))
                return await self.extract_link_from_url(session, redirect_target_url)

            soup = BeautifulSoup(html_content, "lxml")
            link_element = soup.select_one("a.link")
            if not link_element:
                link_element = soup.find("a", string="搜书吧")
            if not link_element:
                link_element = soup.find("a")

            if link_element and link_element.has_attr("href"):
                link_url = link_element["href"]
                if not link_url.startswith(("http://", "https://")):
                    link_url = urljoin(final_url, link_url)
                return link_url

        except Exception as e:
            if is_definitely_proxy_error(e):
                raise ProxyError(str(e)) from e
            logger.error(f"访问 {url} 失败: {e}")
        return None

    async def extract_sxsy_nav_image_url(
        self,
        session: aiohttp.ClientSession,
        nav_url: str = "https://sxsy.org/",
    ) -> Optional[str]:
        try:
            async with session.get(
                nav_url,
                headers=self.headers,
                timeout=10,
                ssl=False,
                allow_redirects=True,
            ) as response:
                if response.status != 200:
                    return None
                final_url = str(response.url)
                html_content = await self.get_text(response)

            soup = BeautifulSoup(html_content, "lxml")
            og_image = soup.find("meta", attrs={"property": "og:image"})
            if og_image and og_image.get("content"):
                return urljoin(final_url, og_image["content"].strip())

            fallback_images: List[str] = []
            preferred_selectors = (
                "#content img",
                ".content img",
                ".entry img",
                ".post img",
                "article img",
                "main img",
                "img",
            )
            for selector in preferred_selectors:
                for img_tag in soup.select(selector):
                    src = (
                        img_tag.get("src")
                        or img_tag.get("data-src")
                        or img_tag.get("data-original")
                        or ""
                    ).strip()
                    if not src or src.startswith("data:"):
                        continue
                    image_url = urljoin(final_url, src)
                    if image_url not in fallback_images:
                        fallback_images.append(image_url)
                    lowered = image_url.lower()
                    if any(
                        token in lowered
                        for token in ("logo", "icon", "avatar", "favicon")
                    ):
                        continue
                    return image_url

            if fallback_images:
                return fallback_images[0]
        except Exception as e:
            if is_definitely_proxy_error(e):
                raise ProxyError(str(e)) from e
            logger.warning(f"[状态监控] 获取SXSY真实地址失败: {e}")
        return None

    async def resolve_ssb_monitor_url(
        self, session: aiohttp.ClientSession
    ) -> Optional[str]:
        for domain_url in self.target_domains:
            link_url = await self.extract_link_from_url(session, domain_url)
            if link_url:
                return self.normalize_base_url(link_url)
        return None

    async def resolve_sxsy_monitor_url(
        self, session: aiohttp.ClientSession
    ) -> Optional[str]:
        configured_base_url = self.get_sxsy_search_base_url()
        if configured_base_url:
            return configured_base_url

        logger.warning("[状态监控] 未配置 sxsy_url，无法检测SXSY状态")
        return None

    def check_ssb_page_health(self, html: str) -> Optional[str]:
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

    def check_sxsy_page_health(self, html: str) -> Optional[str]:
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

    async def probe_site_access(
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
                    html_content = await self.get_text(response)
                    content_error = None
                    if site_key == "ssb":
                        content_error = self.check_ssb_page_health(html_content)
                    elif site_key == "sxsy":
                        content_error = self.check_sxsy_page_health(html_content)
                    if content_error:
                        return False, content_error, self.normalize_base_url(final_url)
                    return (
                        True,
                        f"HTTP {response.status}",
                        self.normalize_base_url(final_url),
                    )
                return (
                    False,
                    f"HTTP {response.status}",
                    self.normalize_base_url(final_url),
                )
        except asyncio.TimeoutError:
            return False, "请求超时", self.normalize_base_url(url)
        except Exception as e:
            if is_definitely_proxy_error(e):
                raise ProxyError(str(e)) from e
            return False, str(e), self.normalize_base_url(url)
