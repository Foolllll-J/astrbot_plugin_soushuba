import os
import re
import time
import asyncio
import random
import json
import subprocess
import datetime
from typing import List, Optional
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, unquote

import aiohttp
from yarl import URL
from bs4 import BeautifulSoup
from astrbot.api import logger

from .cache import SsbAttachment
from .url_resolver import UrlResolver
from .search_service import SearchService


class SsbDownloadService:
    def __init__(
        self,
        headers: dict,
        data_dir: str,
        url_resolver: UrlResolver,
        search_service: SearchService,
        get_kv_data=None,
        put_kv_data=None,
    ):
        self.headers = headers
        self.data_dir = data_dir
        self.url_resolver = url_resolver
        self.search_service = search_service
        self.download_dir = os.path.join(self.data_dir, "downloads", "ssb")
        os.makedirs(self.download_dir, exist_ok=True)
        self.cleanup_delay = 300
        self.get_kv_data = get_kv_data
        self.put_kv_data = put_kv_data
        self.coin_usage_key = "download_coin_usage"

    def _sanitize_filename(self, name: str) -> str:
        name = name.strip().replace("\u3000", " ")
        name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
        return name or "attachment"

    def _get_user_coin_limit(self) -> int:
        raw = self.search_service.plugin_config.get("daily_user_coin_limit", 0)
        try:
            return max(int(raw), 0)
        except (TypeError, ValueError):
            return 0

    def _get_total_coin_limit(self) -> int:
        raw = self.search_service.plugin_config.get("daily_total_coin_limit", 0)
        try:
            return max(int(raw), 0)
        except (TypeError, ValueError):
            return 0

    def _today_str(self) -> str:
        return datetime.date.today().isoformat()

    def _parse_coin_value(self, value: Optional[str]) -> int:
        if not value:
            return 0
        m = re.search(r"\d+", str(value))
        return int(m.group(0)) if m else 0

    async def _load_coin_usage(self) -> dict:
        today = self._today_str()
        data = {}
        if self.get_kv_data:
            try:
                data = await self.get_kv_data(self.coin_usage_key, {}) or {}
            except Exception:
                data = {}
        if data.get("day") != today:
            data = {"day": today, "users": {}, "total": 0}
        if "users" not in data or not isinstance(data["users"], dict):
            data["users"] = {}
        if "total" not in data or not isinstance(data["total"], int):
            data["total"] = 0
        return data

    async def _save_coin_usage(self, data: dict) -> None:
        if not self.put_kv_data:
            return
        try:
            await self.put_kv_data(self.coin_usage_key, data)
        except Exception:
            pass

    async def _consume_coin_budget(
        self,
        user_id: str,
        is_admin: bool,
        cost: int,
    ) -> tuple[bool, str, Optional[int]]:
        user_limit = self._get_user_coin_limit()
        total_limit = self._get_total_coin_limit()
        if is_admin or cost <= 0 or (user_limit <= 0 and total_limit <= 0):
            if user_limit > 0 and not is_admin:
                usage = await self._load_coin_usage()
                spent = int(usage["users"].get(str(user_id), 0))
                return True, "", max(user_limit - spent, 0)
            return True, "", None

        usage = await self._load_coin_usage()
        user_spent = int(usage["users"].get(str(user_id), 0))
        total_spent = int(usage.get("total", 0))

        if user_limit > 0 and user_spent + cost > user_limit:
            remain = max(user_limit - user_spent, 0)
            return False, f"今日个人金币额度不足（本次需 {cost}，剩余 {remain}）", remain
        if total_limit > 0 and total_spent + cost > total_limit:
            total_remain = max(total_limit - total_spent, 0)
            return False, f"今日全局金币额度不足（本次需 {cost}，剩余 {total_remain}）", None

        usage["users"][str(user_id)] = user_spent + cost
        usage["total"] = total_spent + cost
        await self._save_coin_usage(usage)

        user_remain = None
        if user_limit > 0:
            user_remain = max(user_limit - int(usage["users"].get(str(user_id), 0)), 0)
        return True, "", user_remain

    def _get_sxsy_headers(
        self,
        referer: Optional[str] = None,
        ajax: bool = False,
        include_content_type: bool = False,
        include_origin: bool = False,
    ) -> dict:
        # SXSY 已验证核心问题在 _dsign，尽量复用通用请求头，避免过度模拟浏览器细节。
        headers = dict(self.headers or {})
        headers.setdefault("User-Agent", "Mozilla/5.0")
        headers.setdefault("Accept", "*/*")
        if referer:
            headers["Referer"] = referer
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        if include_content_type:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if include_origin and referer:
            parsed = urlparse(referer)
            if parsed.scheme and parsed.netloc:
                headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        return headers

    def _parse_cookie_string(self, cookie_text: str) -> dict:
        cookies: dict[str, str] = {}
        for pair in cookie_text.split(";"):
            part = pair.strip()
            if not part or "=" not in part:
                continue
            key, val = part.split("=", 1)
            key = key.strip()
            val = val.strip()
            if key:
                cookies[key] = val
        return cookies

    def _ensure_sxsy_session_cookies(self, session: aiohttp.ClientSession, base_url: str) -> None:
        cookie = str(self.search_service.plugin_config.get("sxsy_cookie", "") or "").strip()
        if not cookie:
            return
        jar_data = self._parse_cookie_string(cookie)
        if not jar_data:
            return
        session.cookie_jar.update_cookies(jar_data, response_url=URL(base_url))

    def _decode_sxsy_obfuscated_html(self, raw_html: str) -> str:
        if not raw_html:
            return raw_html
        lowered = raw_html.lower()
        if "<html" in lowered:
            return raw_html
        if "%e" not in lowered and "%3c" not in lowered:
            return raw_html
        tokens = re.findall(r"%[0-9a-fA-F]{2}", raw_html)
        if len(tokens) < 200:
            return raw_html
        decoded = unquote("".join(tokens))
        d_lower = decoded.lower()
        if "<html" in d_lower or "forum.php?mod=attachment" in d_lower or "action=attachpay" in d_lower:
            return decoded
        return raw_html

    def _extract_tid_from_url(self, post_url: str) -> Optional[str]:
        parsed = urlparse(post_url)
        query = parse_qs(parsed.query)
        if query.get("tid"):
            return str(query["tid"][0]).strip()
        m = re.search(r"/thread-(\d+)-", parsed.path)
        if m:
            return m.group(1)
        return None

    def _extract_sxsy_dsign(self, raw_html: str) -> Optional[str]:
        if not raw_html:
            return None
        # 1) 直接命中
        m = re.search(r"_dsign=([a-fA-F0-9]{8,32})", raw_html)
        if m:
            return m.group(1).lower()
        # 2) 对百分号编码内容解码后再找
        decoded = unquote(raw_html)
        m = re.search(r"_dsign=([a-fA-F0-9]{8,32})", decoded)
        if m:
            return m.group(1).lower()
        # 3) 从被拆分的字符串字面量里拼接再找
        literals = re.findall(r"['\"]([^'\"]{1,80})['\"]", raw_html)
        if literals:
            joined = "".join(literals)
            m = re.search(r"_dsign=([a-fA-F0-9]{8,32})", joined)
            if m:
                return m.group(1).lower()
            m = re.search(r"_dsign=([a-fA-F0-9]{8,32})", unquote(joined))
            if m:
                return m.group(1).lower()
        # 4) 壳页脚本 location.assign(...) 表达式求值
        dsign_from_shell = self._extract_sxsy_dsign_from_shell_script(raw_html)
        if dsign_from_shell:
            return dsign_from_shell
        return None

    def _extract_sxsy_shell_jump_url_by_node(self, raw_html: str) -> Optional[str]:
        """
        用 Node 执行壳页脚本，捕获 location/window 的跳转目标。
        优先返回包含 _dsign 的最长 URL。
        """
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", raw_html, re.S | re.I)
        target = ""
        for script in scripts:
            lowered = script.lower()
            if "location" in lowered and ("assign" in lowered or "href" in lowered):
                target = script
                break
        if not target and scripts:
            target = scripts[0]
        if not target:
            return None

        js = f"""
const captured = [];
const locObj = {{href:''}};
const loc = new Proxy(locObj, {{
  set(t,p,v) {{
    const sv = String(v || '');
    if (String(p)==='href' || sv.startsWith('/') || sv.includes('forum.php')) captured.push(sv);
    t[p]=v;
    return true;
  }},
  get(t,p) {{
    if(String(p)==='assign') return (u)=>{{captured.push(String(u||'')); t.href=String(u||'');}};
    if (Object.prototype.hasOwnProperty.call(t, p)) return t[p];
    return (u)=>{{captured.push(String(u||'')); t.href=String(u||'');}};
  }}
}});
Object.defineProperty(globalThis, 'location', {{
  configurable:true,
  get() {{ return loc; }},
  set(v) {{ captured.push(String(v||'')); loc.href=String(v||''); }}
}});
Object.defineProperty(globalThis, 'window', {{
  configurable:true,
  get() {{ return loc; }},
  set(v) {{ }}
}});
try {{ {target} }} catch(e) {{ }}
console.log(JSON.stringify(captured));
"""
        try:
            proc = subprocess.run(
                ["node", "-e", js],
                capture_output=True,
                text=True,
                timeout=8,
            )
        except Exception as e:
            logger.debug(f"[SXSY 下载] Node 壳执行失败: {type(e).__name__}")
            return None

        out = (proc.stdout or "").strip()
        if not out:
            return None
        try:
            values = json.loads(out)
            if not isinstance(values, list):
                return None
            candidates = [str(v or "").strip() for v in values if str(v or "").strip()]
            if not candidates:
                return None
            candidates.sort(key=lambda x: (("_dsign=" in x), len(x)), reverse=True)
            return candidates[0]
        except Exception:
            return None

    def _extract_sxsy_dsign_from_shell_script(self, raw_html: str) -> Optional[str]:
        def extract_balanced_call_arg(text: str, open_paren_index: int) -> Optional[str]:
            if open_paren_index < 0 or open_paren_index >= len(text):
                return None
            if text[open_paren_index] != "(":
                return None
            depth = 0
            in_single = False
            in_double = False
            escaped = False
            buf: list[str] = []
            for i in range(open_paren_index, len(text)):
                ch = text[i]
                if escaped:
                    escaped = False
                    if depth >= 1 and i != open_paren_index:
                        buf.append(ch)
                    continue
                if ch == "\\":
                    escaped = True
                    if depth >= 1 and i != open_paren_index:
                        buf.append(ch)
                    continue
                if ch == "'" and not in_double:
                    in_single = not in_single
                    if depth >= 1 and i != open_paren_index:
                        buf.append(ch)
                    continue
                if ch == '"' and not in_single:
                    in_double = not in_double
                    if depth >= 1 and i != open_paren_index:
                        buf.append(ch)
                    continue
                if in_single or in_double:
                    if depth >= 1 and i != open_paren_index:
                        buf.append(ch)
                    continue
                if ch == "(":
                    depth += 1
                    if depth > 1:
                        buf.append(ch)
                    continue
                if ch == ")":
                    depth -= 1
                    if depth == 0:
                        return "".join(buf).strip()
                    if depth > 0:
                        buf.append(ch)
                    continue
                if depth >= 1 and i != open_paren_index:
                    buf.append(ch)
            return None

        def extract_assignment_expr(text: str, eq_index: int) -> Optional[str]:
            if eq_index < 0 or eq_index >= len(text):
                return None
            i = eq_index + 1
            while i < len(text) and text[i].isspace():
                i += 1
            start = i

            in_single = False
            in_double = False
            escaped = False
            p_depth = 0
            b_depth = 0
            c_depth = 0

            while i < len(text):
                ch = text[i]
                if escaped:
                    escaped = False
                    i += 1
                    continue
                if ch == "\\":
                    escaped = True
                    i += 1
                    continue
                if ch == "'" and not in_double:
                    in_single = not in_single
                    i += 1
                    continue
                if ch == '"' and not in_single:
                    in_double = not in_double
                    i += 1
                    continue
                if in_single or in_double:
                    i += 1
                    continue

                if ch == "(":
                    p_depth += 1
                    i += 1
                    continue
                if ch == ")":
                    p_depth = max(0, p_depth - 1)
                    i += 1
                    continue
                if ch == "[":
                    b_depth += 1
                    i += 1
                    continue
                if ch == "]":
                    b_depth = max(0, b_depth - 1)
                    i += 1
                    continue
                if ch == "{":
                    c_depth += 1
                    i += 1
                    continue
                if ch == "}":
                    c_depth = max(0, c_depth - 1)
                    i += 1
                    continue
                if ch == ";" and p_depth == 0 and b_depth == 0 and c_depth == 0:
                    return text[start:i].strip()
                i += 1
            return text[start:i].strip() if i > start else None

        def split_top_level_plus(expr_text: str) -> list[str]:
            parts: list[str] = []
            cur = ""
            depth = 0
            in_single = False
            in_double = False
            escaped = False
            for ch in expr_text:
                if escaped:
                    escaped = False
                    cur += ch
                    continue
                if ch == "\\":
                    escaped = True
                    cur += ch
                    continue
                if ch == "'" and not in_double:
                    in_single = not in_single
                    cur += ch
                    continue
                if ch == '"' and not in_single:
                    in_double = not in_double
                    cur += ch
                    continue
                if in_single or in_double:
                    cur += ch
                    continue
                if ch == "(":
                    depth += 1
                    cur += ch
                    continue
                if ch == ")":
                    depth = max(0, depth - 1)
                    cur += ch
                    continue
                if ch == "+" and depth == 0:
                    parts.append(cur)
                    cur = ""
                    continue
                cur += ch
            if cur:
                parts.append(cur)
            return parts

        expr = ""
        expr_source = ""
        # 只匹配 location 直接调用，避免误命中普通的 .replace/.match 调用
        direct_location_call_patterns = (
            r"location\[[^\]]+\]\s*\(",
            r"location\.assign\s*\(",
        )
        for pattern in direct_location_call_patterns:
            for match in re.finditer(pattern, raw_html):
                open_paren_index = match.end() - 1
                arg = extract_balanced_call_arg(raw_html, open_paren_index)
                if arg:
                    expr = arg
                    expr_source = f"call:{pattern}"
                    break
            if expr:
                break

        # location 别名调用（如 _i5Yo9[_RQRfp](...) 或 alias.assign(...)）
        if not expr:
            location_aliases = set(
                m.group(1)
                for m in re.finditer(
                    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*location\s*;",
                    raw_html,
                )
            )
            for alias in location_aliases:
                alias_patterns = (
                    rf"\b{re.escape(alias)}\s*\[\s*[A-Za-z_][A-Za-z0-9_]*\s*\]\s*\(",
                    rf"\b{re.escape(alias)}\s*\.\s*[A-Za-z_][A-Za-z0-9_]*\s*\(",
                )
                for pattern in alias_patterns:
                    for match in re.finditer(pattern, raw_html):
                        open_paren_index = match.end() - 1
                        arg = extract_balanced_call_arg(raw_html, open_paren_index)
                        if arg:
                            expr = arg
                            expr_source = f"alias_call:{alias}"
                            break
                    if expr:
                        break
                if expr:
                    break

        if not expr:
            # fallback: 直接赋值写法
            assign_patterns = (
                r"location\.href\s*=",
                r"[A-Za-z_][A-Za-z0-9_]*\.href\s*=",
                r"_jUTq8\[['\"]href['\"]\]\s*=",
                r"window\[['\"]href['\"]\]\s*=",
                r"location\s*=",
            )
            for pattern in assign_patterns:
                for match in re.finditer(pattern, raw_html, re.S):
                    eq_index = raw_html.find("=", match.start())
                    if eq_index < 0:
                        continue
                    value = extract_assignment_expr(raw_html, eq_index)
                    if value:
                        expr = value
                        expr_source = f"assign:{pattern}"
                        break
                if expr:
                    break
        if not expr:
            return None

        var_map: dict[str, str] = {}
        for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'([^']*)';", raw_html):
            var_map[m.group(1)] = m.group(2)

        func_const: dict[str, str] = {}
        for pattern in (
            r"function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*\{([^}]*)\}",
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*function\s*\([^)]*\)\s*\{([^}]*)\}",
        ):
            for m in re.finditer(pattern, raw_html):
                body = m.group(2)
                returns = re.findall(r"return\s+'([^']*)'", body)
                if returns:
                    func_const[m.group(1)] = returns[-1]

        for pattern in (
            r"function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*\{.*?return\s+'([^']*)'.*?\}",
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*function\s*\([^)]*\)\s*\{.*?return\s+'([^']*)'.*?\}",
        ):
            for m in re.finditer(pattern, raw_html, re.S):
                func_const.setdefault(m.group(1), m.group(2))

        identity_funcs = set(
            m.group(1)
            for m in re.finditer(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*function\s*\((\w+)\)\s*\{.*?return\s+\2;",
                raw_html,
                re.S,
            )
        )

        name_funcs: dict[str, str] = {}
        for pattern in (
            r"function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*\{function\s+_[^(]+\([^)]*\)\{function\s+([A-Za-z_][A-Za-z0-9_]*)\(\)\{return\s+getName\(\);\}",
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*function\s*\([^)]*\)\s*\{function\s+_[^(]+\([^)]*\)\{function\s+([A-Za-z_][A-Za-z0-9_]*)\(\)\{return\s+getName\(\);\}",
        ):
            for m in re.finditer(pattern, raw_html):
                name_funcs[m.group(1)] = m.group(2)

        parts = split_top_level_plus(expr)

        def eval_part(part: str) -> str:
            p = part.strip()
            if not p:
                return ""
            if p.startswith("'") and p.endswith("'"):
                return p[1:-1]
            if p.startswith("(function"):
                returns = re.findall(r"return\s+'([^']*)'", p)
                if returns:
                    return returns[-1]
                m = re.match(
                    r"^\(function\((\w+)\)\{.*?return\s+\1;.*?\}\)\('([^']*)'\)$",
                    p,
                    re.S,
                )
                if m:
                    return m.group(2)

            m = re.match(r"^(\w+)\(\)$", p)
            if m:
                fn = m.group(1)
                return var_map.get(fn) or func_const.get(fn) or name_funcs.get(fn) or ""

            m = re.match(r"^(\w+)\('([^']*)'\)$", p)
            if m:
                fn, arg = m.group(1), m.group(2)
                if fn in identity_funcs:
                    return arg
                if fn in name_funcs:
                    return name_funcs[fn]
                if fn in func_const:
                    return func_const[fn]
                return arg

            if p in var_map:
                return var_map[p]
            return ""

        resolved = "".join(eval_part(p) for p in parts)
        match = re.search(r"_dsign=([a-fA-F0-9]{8,32})", resolved)
        if not match:
            match = re.search(r"_dsign=([a-fA-F0-9]{8,32})", unquote(resolved))
        if match:
            logger.debug(
                f"[SXSY 下载] 壳脚本表达式求值命中 _dsign={match.group(1).lower()} "
                f"(source={expr_source})"
            )
            return match.group(1).lower()
        return None

    def _detect_gated_content(self, html: str) -> List[str]:
        hints = []
        if self._needs_reply_unlock(html):
            hints.append("可能需要回复后可见")
        if "权限不足" in html:
            hints.append("可能权限不足")
        return hints

    def _needs_reply_unlock(self, html: str) -> bool:
        return "如果您要查看本帖隐藏内容请" in html

    async def fetch_post_attachments(
        self, session: aiohttp.ClientSession, post_url: str
    ) -> List[SsbAttachment]:
        logger.debug(f"[SSB 下载] 抓取帖子附件: {post_url}")
        html, final_url = await self._fetch_post_html(session, post_url)
        return await self._parse_attachments_with_retry(
            session, html, final_url, post_url
        )

    async def _parse_attachments_with_retry(
        self,
        session: aiohttp.ClientSession,
        html: str,
        final_url: str,
        post_url: str,
    ) -> List[SsbAttachment]:
        gated_hints = self._detect_gated_content(html)
        if gated_hints:
            logger.debug(f"[SSB 下载] 帖子可能受限: {'; '.join(gated_hints)}")

        attachments = self._parse_attachments(html, final_url)
        if attachments:
            logger.debug(f"[SSB 下载] 解析到附件数: {len(attachments)}")
            return attachments

        # 若提示需要回复且未解析到附件，先自动回帖，再刷新重试
        if self._needs_reply_unlock(html):
            logger.debug("[SSB 下载] 检测到隐藏内容，尝试回帖解锁")
            ok, msg = await self.reply_to_unlock(session, post_url, html)
            if not ok:
                logger.warning(f"[SSB 下载] 回帖失败: {msg}")
                return []
            html, final_url = await self._fetch_post_html(session, post_url)
            attachments = self._parse_attachments(html, final_url)
            logger.debug(f"[SSB 下载] 回帖后解析到附件数: {len(attachments)}")
            if attachments:
                return attachments

        logger.debug("[SSB 下载] 解析到附件数: 0")
        return []

    def _parse_attachments(
        self, html: str, final_url: str
    ) -> List[SsbAttachment]:
        soup = BeautifulSoup(html, "lxml")
        attachments: List[SsbAttachment] = []

        def _pick_better_name(current: str, candidate: str) -> str:
            def _score(value: str) -> tuple[int, int]:
                has_extension = int(bool(re.search(r"\.[A-Za-z0-9]{1,8}$", value)))
                return (has_extension, len(value))

            return candidate if _score(candidate) > _score(current) else current
        ignore_texts = {"购买", "[购买]", "记录", "[记录]"}

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "attachment" not in href and "aid=" not in href:
                continue
            if "mod=attachment" not in href and "attachment.php" not in href:
                if "mod=misc" not in href or "action=attachpay" not in href:
                    continue

            name = (
                a.get_text(strip=True)
                or a.get("title")
                or a.get("download")
                or "attachment"
            )
            if name in ignore_texts:
                continue
            full_url = urljoin(final_url, href)
            parsed = urlparse(full_url)
            query = parse_qs(parsed.query)
            aid = None
            if "aid" in query and query["aid"]:
                aid = str(query["aid"][0])

            att = SsbAttachment(
                name=name,
                url=full_url,
                aid=aid,
                post_url=final_url,
            )
            if "mod=attachment" in href or "attachment.php" in href:
                att.download_url = full_url
            if "mod=misc" in href and "action=attachpay" in href:
                att.pay_url = full_url
            attachments.append(att)

        deduped: List[SsbAttachment] = []
        merged: dict[tuple[str, str], SsbAttachment] = {}
        for item in attachments:
            key = ("target", item.download_url or item.pay_url or item.url)
            existing = merged.get(key)
            if existing:
                existing.name = _pick_better_name(existing.name, item.name)
                existing.aid = existing.aid or item.aid
                existing.pay_url = existing.pay_url or item.pay_url
                existing.download_url = existing.download_url or item.download_url
                existing.post_url = existing.post_url or item.post_url
                continue
            merged[key] = item
            deduped.append(item)

        return deduped

    def _parse_formhash(self, html: str) -> Optional[str]:
        match = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', html)
        if match:
            return match.group(1)
        return None

    def _parse_succeedhandle_register(self, text: str) -> tuple[Optional[str], Optional[str]]:
        """
        解析 inajax=1 的 XML/CDATA 响应，提取跳转 URL 与提示文案。
        返回 (url, message)；无法解析则返回 (None, None)
        """
        # 常见格式：succeedhandle_register('url', 'msg', {...});
        # SXSY 样式：succeedhandle_attachpay('url', 'msg', {...});
        match = re.search(
            r"succeedhandle_[a-zA-Z0-9_]+\(\s*'([^']+)'\s*,\s*'([^']+)'",
            text,
        )
        if match:
            return match.group(1), match.group(2)
        return None, None

    def _extract_inajax_message(self, text: str) -> Optional[str]:
        """
        从 inajax 响应中提取可读提示文本（去除标签）。
        """
        # 取 CDATA 中的内容
        cdata = None
        match = re.search(r"<!\[CDATA\[(.*)\]\]>", text, re.S)
        if match:
            cdata = match.group(1)
        raw = cdata or text
        # 移除 script/style
        raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S)
        raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.S)
        # 移除所有标签
        raw = re.sub(r"<[^>]+>", " ", raw)
        # 压缩空白
        msg = " ".join(raw.split())
        return msg.strip() or None

    def _extract_cdata_html(self, text: str) -> str:
        match = re.search(r"<!\[CDATA\[(.*)\]\]>", text, re.S)
        return match.group(1) if match else text

    def _parse_attachpay_info(self, html: str) -> dict[str, str]:
        info: dict[str, str] = {}
        soup = BeautifulSoup(html, "lxml")
        for row in soup.select("table.list tr"):
            cols = row.find_all("td")
            if len(cols) != 2:
                continue
            key = cols[0].get_text(strip=True)
            val = cols[1].get_text(strip=True)
            if key and val:
                info[key] = val
        return info

    def _parse_tid_fid(self, html: str) -> tuple[Optional[str], Optional[str]]:
        m = re.search(r"var\s+fid\s*=\s*parseInt\('(\d+)'\)", html)
        fid = m.group(1) if m else None
        m = re.search(r"var\s+tid\s*=\s*parseInt\('(\d+)'\)", html)
        tid = m.group(1) if m else None
        if tid and fid:
            return tid, fid
        m = re.search(r"forum\.php\?mod=viewthread&tid=(\d+)", html)
        if m:
            tid = tid or m.group(1)
        return tid, fid

    async def _fetch_post_html(
        self,
        session: aiohttp.ClientSession,
        post_url: str,
        extra_headers: Optional[dict] = None,
    ) -> tuple[str, str]:
        req_headers = dict(self.headers)
        if extra_headers:
            req_headers.update(extra_headers)
        async with session.get(
            post_url, headers=req_headers, timeout=20, ssl=False
        ) as resp:
            html = await self.url_resolver.get_text(resp)
            final_url = str(resp.url)
        return html, final_url

    def _parse_sxsy_attachments(
        self, html: str, final_url: str
    ) -> List[SsbAttachment]:
        soup = BeautifulSoup(html, "lxml")
        attachments: List[SsbAttachment] = []
        ignore_texts = {"购买", "[购买]", "记录", "[记录]"}

        def _pick_better_name(current: str, candidate: str) -> str:
            def _score(value: str) -> tuple[int, int]:
                has_extension = int(bool(re.search(r"\.[A-Za-z0-9]{1,8}$", value)))
                return (has_extension, len(value))

            return candidate if _score(candidate) > _score(current) else current

        for a in soup.find_all("a"):
            href = str(a.get("href") or "").strip()
            onclick = str(a.get("onclick") or "").strip()
            onclick_url = None
            onclick_match = re.search(r"showWindow\([^,]+,\s*'([^']+)'", onclick)
            if onclick_match:
                onclick_url = onclick_match.group(1).strip()

            link_ref = href
            if (not link_ref or link_ref.startswith(("javascript:", "#"))) and onclick_url:
                link_ref = onclick_url

            is_pay_link = "mod=misc" in link_ref and "action=attachpay" in link_ref
            is_download_link = "mod=attachment" in link_ref or "attachment.php" in link_ref
            if not is_pay_link and not is_download_link:
                continue

            name = (
                a.get_text(strip=True)
                or a.get("title")
                or a.get("download")
                or "attachment"
            )
            if name in ignore_texts:
                continue

            full_url = urljoin(final_url, link_ref)
            query = parse_qs(urlparse(full_url).query)
            aid = None
            if query.get("aid"):
                aid = str(query["aid"][0])
            anchor_id = str(a.get("id") or "")
            m = re.search(r"aid(\d+)", anchor_id)
            if m:
                aid = m.group(1)

            att = SsbAttachment(
                name=name,
                url=full_url,
                aid=aid,
                post_url=final_url,
            )
            if is_pay_link:
                att.pay_url = full_url
            if is_download_link:
                att.download_url = full_url
            attachments.append(att)

        deduped: List[SsbAttachment] = []
        merged: dict[tuple[str, str], SsbAttachment] = {}
        for item in attachments:
            key = ("target", item.download_url or item.pay_url or item.url)
            existing = merged.get(key)
            if existing:
                existing.name = _pick_better_name(existing.name, item.name)
                existing.aid = existing.aid or item.aid
                existing.pay_url = existing.pay_url or item.pay_url
                existing.download_url = existing.download_url or item.download_url
                existing.post_url = existing.post_url or item.post_url
                continue
            merged[key] = item
            deduped.append(item)

        if not deduped and "action=attachpay" in html:
            for match in re.finditer(
                r'href=["\']([^"\']*action=attachpay[^"\']*)["\'][^>]*>([^<]+)</a>',
                html,
                re.I,
            ):
                href = match.group(1).strip()
                name = match.group(2).strip() or "attachment"
                if name in ignore_texts:
                    continue
                full_url = urljoin(final_url, href)
                query = parse_qs(urlparse(full_url).query)
                aid = str(query.get("aid", [None])[0]) if query.get("aid") else None
                deduped.append(
                    SsbAttachment(
                        name=name,
                        url=full_url,
                        aid=aid,
                        post_url=final_url,
                        pay_url=full_url,
                    )
                )
        pay_count = sum(1 for x in deduped if x.pay_url)
        direct_count = sum(1 for x in deduped if x.download_url)
        aid_count = sum(1 for x in deduped if x.aid)
        logger.debug(
            "[SXSY 下载] 附件样式统计: "
            f"total={len(deduped)}, pay_links={pay_count}, direct_links={direct_count}, "
            f"aid_present={aid_count}, html_contains_attachpay={'action=attachpay' in html}, "
            f"html_contains_attachment={'mod=attachment' in html or 'attachment.php' in html}"
        )
        return deduped

    async def fetch_sxsy_post_attachments(
        self, session: aiohttp.ClientSession, post_url: str
    ) -> List[SsbAttachment]:
        logger.debug(f"[SXSY 下载] 抓取帖子附件: {post_url}")
        base_url = self.url_resolver.normalize_base_url(post_url)
        self._ensure_sxsy_session_cookies(session, base_url)
        patch_url = urljoin(base_url, "misc.php?mod=patch")
        try:
            async with session.get(
                patch_url,
                headers=self._get_sxsy_headers(referer=base_url),
                timeout=12,
                ssl=False,
                allow_redirects=True,
            ) as resp:
                _ = await self.url_resolver.get_text(resp)
        except Exception:
            pass

        html, final_url = await self._fetch_post_html(
            session,
            post_url,
            extra_headers=self._get_sxsy_headers(referer=post_url),
        )
        dsign = None
        dsign_method = ""
        raw_lower = html.lower()
        shell_like = "<html" not in raw_lower and (
            "location" in raw_lower or "assign" in raw_lower or "href" in raw_lower
        )
        logger.debug(
            "[SXSY 下载] 帖子页特征: "
            f"shell_like={shell_like}, has_html_tag={'<html' in raw_lower}, body_len={len(html)}, "
            f"has_location={'location' in raw_lower}, has_assign={'assign' in raw_lower}, "
            f"has_href={'href' in raw_lower}, has_dsign_literal={'_dsign' in raw_lower}"
        )

        if shell_like:
            dsign = self._extract_sxsy_dsign(html)
            if dsign:
                dsign_method = "shell_eval"
            else:
                jump_url = self._extract_sxsy_shell_jump_url_by_node(html)
                if jump_url:
                    logger.debug(
                        "[SXSY 下载] node_eval 壳跳转候选: "
                        f"len={len(jump_url)}, has_dsign={'_dsign=' in jump_url}, "
                        f"prefix={jump_url[:120]}"
                    )
                    dsign = self._extract_sxsy_dsign(jump_url) or self._extract_sxsy_dsign(unquote(jump_url))
                    if dsign:
                        dsign_method = "node_eval"

        tid = self._extract_tid_from_url(post_url)
        if dsign and tid:
            parsed = urlparse(post_url)
            dsign_url = (
                f"{parsed.scheme}://{parsed.netloc}/forum.php"
                f"?mod=viewthread&tid={tid}&_dsign={dsign}"
            )
            logger.debug(
                f"[SXSY 下载] 从壳页面提取 _dsign={dsign} "
                f"(method={dsign_method or '-'})，二次请求: {dsign_url}"
            )
            html, final_url = await self._fetch_post_html(
                session,
                dsign_url,
                extra_headers=self._get_sxsy_headers(referer=post_url),
            )
        elif shell_like:
            raw_lower = (html or "").lower()
            logger.debug(
                "[SXSY 下载] _dsign 提取失败: "
                f"has_location_call={'location' in raw_lower}, "
                f"has_href_assign={'href' in raw_lower or 'assign' in raw_lower}, "
                f"has_dsign_literal={'_dsign' in raw_lower}, raw_len={len(html or '')}"
            )
        html = self._decode_sxsy_obfuscated_html(html)
        attachments = self._parse_sxsy_attachments(html, final_url)
        if not attachments:
            lowered = html.lower()
            login_like = (
                "member.php?mod=logging&action=login" in lowered
                or "class=\"pg_logging\"" in lowered
                or "登录 -" in html
            )
            challenge_like = (
                "cf-chl-" in lowered
                or "just a moment..." in lowered
                or "attention required" in lowered
            )
            title = ""
            soup = BeautifulSoup(html, "lxml")
            if soup.title and soup.title.string:
                title = soup.title.string.strip()
            snippet = " ".join(html.strip().split())[:160] if html else ""
            logger.debug(
                "[SXSY 下载] 未解析到附件，页面特征: "
                f"title={title or '-'}, login_like={login_like}, challenge_like={challenge_like}, "
                f"contains_attachpay={'action=attachpay' in html}, final_url={final_url}, "
                f"body_len={len(html)}, has_html_tag={'<html' in lowered}, snippet={snippet or '-'}"
            )
        logger.debug(f"[SXSY 下载] 解析到附件数: {len(attachments)}")
        return attachments

    async def purchase_sxsy_attachment(
        self,
        session: aiohttp.ClientSession,
        post_url: str,
        aid: str,
        html: str,
        user_id: str,
        is_admin: bool,
    ) -> tuple[bool, str, Optional[str], int, Optional[int]]:
        tid = self._extract_tid_from_url(post_url)
        tid_source = "url"
        if not tid:
            tid, _fid = self._parse_tid_fid(html)
            tid_source = "html"
        if not tid:
            return False, "未解析到 tid", None, 0, None
        logger.debug(
            "[SXSY 下载] 购买请求参数: "
            f"tid={tid}, tid_source={tid_source}, aid={aid}, "
            f"referer_has_dsign={'_dsign=' in (post_url or '')}"
        )

        pre_url = (
            f"https://{urlparse(post_url).netloc}/forum.php"
            f"?mod=misc&action=attachpay&aid={aid}&tid={tid}"
            f"&infloat=yes&handlekey=attachpay&inajax=1&ajaxtarget=fwin_content_attachpay"
        )
        async with session.get(
            pre_url,
            headers=self._get_sxsy_headers(referer=post_url, ajax=True),
            timeout=20,
            ssl=False,
        ) as resp:
            pre_text = await self.url_resolver.get_text(resp)
            logger.debug(
                "[SXSY 下载] 购买预检响应: "
                f"status={resp.status}, len={len(pre_text)}, has_formhash={'formhash' in pre_text}, "
                f"has_attachpay_fn={'attachpay' in pre_text.lower()}"
            )
        pre_html = self._extract_cdata_html(pre_text)
        formhash = self._parse_formhash(pre_html)
        if not formhash:
            msg_text = self._extract_inajax_message(pre_text)
            return False, msg_text or "预取购买信息失败", None, 0, None

        info = self._parse_attachpay_info(pre_html)
        price = (
            info.get("售价(金钱)")
            or info.get("售价(银币)")
            or info.get("售价")
        )
        balance = (
            info.get("购买后余额(金钱)")
            or info.get("购买后余额(银币)")
            or info.get("购买后余额")
        )
        if price or balance:
            logger.debug(f"[SXSY 下载] 购买信息: 售价={price or '-'} 余额={balance or '-'}")
        coin_cost = self._parse_coin_value(price)
        ok_budget, budget_msg, remain_after = await self._consume_coin_budget(
            user_id=user_id,
            is_admin=is_admin,
            cost=coin_cost,
        )
        if not ok_budget:
            budget_msg = budget_msg.replace("金币", "金钱")
            return False, budget_msg, None, 0, remain_after

        payload = {
            "formhash": formhash,
            "referer": post_url,
            "aid": aid,
            "handlekey": "attachpay",
        }
        pay_url = (
            f"https://{urlparse(post_url).netloc}/forum.php"
            f"?mod=misc&action=attachpay&tid={tid}"
            f"&paysubmit=yes&infloat=yes&inajax=1"
        )
        async with session.post(
            pay_url,
            data=payload,
            headers=self._get_sxsy_headers(
                referer=post_url,
                include_origin=True,
                include_content_type=True,
            ),
            timeout=20,
            ssl=False,
        ) as resp:
            text = await self.url_resolver.get_text(resp)
            url, msg = self._parse_succeedhandle_register(text)
            logger.debug(
                "[SXSY 下载] 购买提交响应: "
                f"status={resp.status}, len={len(text)}, has_succeedhandle={bool(url or msg)}, "
                f"return_url={url or '-'}"
            )
            if url or msg:
                download_url = urljoin(post_url, url) if url else None
                if download_url and "action=attachpay" in download_url:
                    # 购买成功但返回的仍是购买弹窗链接，后续需回帖页重新解析真实附件链接
                    download_url = None
                return True, msg or "购买成功", download_url, coin_cost, remain_after
            msg_text = self._extract_inajax_message(text)
            return False, msg_text or "购买失败", None, 0, remain_after

    async def download_sxsy_attachment(
        self,
        session: aiohttp.ClientSession,
        attachment: SsbAttachment,
        user_id: str,
        is_admin: bool,
    ) -> tuple[bool, str, Optional[str], int, Optional[int]]:
        def _is_real_download_url(url: Optional[str]) -> bool:
            if not url:
                return False
            lowered = url.lower()
            if "action=attachpay" in lowered:
                return False
            return ("mod=attachment" in lowered) or ("attachment.php" in lowered)

        post_url = attachment.post_url or attachment.url
        base_url = self.url_resolver.normalize_base_url(post_url)
        self._ensure_sxsy_session_cookies(session, base_url)
        download_url = attachment.download_url if _is_real_download_url(attachment.download_url) else None
        sxsy_headers = self._get_sxsy_headers(referer=post_url)
        spent_coin = 0
        remain_after = None
        logger.debug(
            "[SXSY 下载] 下载入口: "
            f"aid={attachment.aid or '-'}, has_pay_url={bool(attachment.pay_url)}, "
            f"input_download_url={attachment.download_url or '-'}, normalized_download_url={download_url or '-'}"
        )

        html = ""
        if post_url:
            html, final_url = await self._fetch_post_html(
                session,
                post_url,
                extra_headers=sxsy_headers,
            )
            # 购买请求中的 referer 尽量使用实际落地页（优先带 _dsign）
            post_url = final_url or post_url
            sxsy_headers = self._get_sxsy_headers(referer=post_url)
            if attachment.aid:
                resolved = self._extract_download_url(html, final_url, attachment.aid)
                if resolved:
                    download_url = resolved if _is_real_download_url(resolved) else None
                    logger.debug(
                        "[SXSY 下载] 帖子页解析下载链接(aid): "
                        f"resolved={resolved}, accepted={bool(download_url)}"
                    )
            if not download_url:
                parsed_attachments = self._parse_sxsy_attachments(html, final_url)
                for item in parsed_attachments:
                    if attachment.aid and item.aid == attachment.aid and item.download_url:
                        download_url = item.download_url if _is_real_download_url(item.download_url) else None
                        logger.debug(
                            "[SXSY 下载] 附件列表匹配(aid): "
                            f"candidate={item.download_url}, accepted={bool(download_url)}"
                        )
                        break
                    if item.name == attachment.name and item.download_url:
                        download_url = item.download_url if _is_real_download_url(item.download_url) else None
                        logger.debug(
                            "[SXSY 下载] 附件列表匹配(name): "
                            f"candidate={item.download_url}, accepted={bool(download_url)}"
                        )
                        break

        if attachment.pay_url and attachment.aid and not download_url:
            logger.debug(f"[SXSY 下载] 检测到需购买附件: aid={attachment.aid}, tid_from_url={self._extract_tid_from_url(post_url) or '-'}")
            ok, msg, direct_url, spent_coin, remain_after = await self.purchase_sxsy_attachment(
                session, post_url, attachment.aid, html, user_id, is_admin
            )
            if not ok:
                return False, f"购买失败：{msg}", None, 0, remain_after
            if _is_real_download_url(direct_url):
                download_url = direct_url
                logger.debug(f"[SXSY 下载] 购买返回直链可用: {download_url}")
            else:
                html, final_url = await self._fetch_post_html(
                    session,
                    post_url,
                    extra_headers=sxsy_headers,
                )
                resolved = self._extract_download_url(html, final_url, attachment.aid)
                if _is_real_download_url(resolved):
                    download_url = resolved
                    logger.debug(f"[SXSY 下载] 购买后回帖解析直链可用: {download_url}")
                else:
                    logger.debug(
                        "[SXSY 下载] 购买后回帖解析仍未命中直链: "
                        f"resolved={resolved or '-'}"
                    )

        if not _is_real_download_url(download_url):
            return False, "未解析到可下载的附件链接", None, spent_coin, remain_after

        filename = self._sanitize_filename(attachment.name)
        ts = int(time.time())
        file_path = os.path.join(self.download_dir, f"{ts}_{filename}")
        logger.debug(f"[SXSY 下载] 开始下载: {download_url} -> {file_path}")

        async with session.get(
            download_url,
            headers=sxsy_headers,
            timeout=30,
            ssl=False,
            allow_redirects=True,
        ) as resp:
            if resp.status >= 400:
                return False, f"下载失败 HTTP {resp.status}", None, spent_coin, remain_after
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type or "text/xml" in content_type:
                text = await self.url_resolver.get_text(resp)
                msg_text = self._extract_inajax_message(text)
                return False, msg_text or "返回文本页面，可能购买失败或 Cookie 失效", None, spent_coin, remain_after

            with open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    f.write(chunk)

        if os.path.exists(file_path):
            logger.debug(f"[SXSY 下载] 下载完成: {file_path}")
            if remain_after is None and (not is_admin) and self._get_user_coin_limit() > 0:
                usage = await self._load_coin_usage()
                user_spent = int(usage["users"].get(str(user_id), 0))
                remain_after = max(self._get_user_coin_limit() - user_spent, 0)
            return True, "下载成功", file_path, spent_coin, remain_after
        return False, "下载失败，文件未落盘", None, spent_coin, remain_after

    async def reply_to_unlock(
        self, session: aiohttp.ClientSession, post_url: str, html: str
    ) -> tuple[bool, str]:
        formhash = self._parse_formhash(html)
        tid, fid = self._parse_tid_fid(html)
        if not formhash or not tid or not fid:
            return False, "未解析到 formhash / tid / fid"

        # 先执行 checkpostrule
        check_url = (
            f"https://{urlparse(post_url).netloc}/forum.php"
            f"?mod=ajax&action=checkpostrule&inajax=yes&ac=reply"
        )
        try:
            async with session.get(
                check_url,
                headers={**self.headers, "Referer": post_url, "X-Requested-With": "XMLHttpRequest"},
                timeout=15,
                ssl=False,
            ) as resp:
                _ = await self.url_resolver.get_text(resp)
        except Exception:
            pass

        reply_candidates = [
            "看了LZ的帖子，我只想说一句很好很强大！",
            "啥也不说了，楼主就是给力！",
            "谢谢楼主分享，祝搜书吧越办越好！",
        ]
        reply_text = random.choice(reply_candidates)
        payload = {
            "file": "",
            "message": reply_text,
            "posttime": str(int(time.time())),
            "formhash": formhash,
            "usesig": "1",
            "subject": "++",
        }
        data = urlencode(payload, encoding="gbk").encode("gbk")
        reply_url = (
            f"https://{urlparse(post_url).netloc}/forum.php"
            f"?mod=post&action=reply&fid={fid}&tid={tid}&extra="
            f"&replysubmit=yes&infloat=yes&handlekey=fastpost&inajax=1"
        )
        logger.debug(f"[SSB 下载] 尝试回帖解锁: tid={tid}, fid={fid}")
        async with session.post(
            reply_url,
            data=data,
            headers={
                **self.headers,
                "Referer": post_url,
                "Origin": f"https://{urlparse(post_url).netloc}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=20,
            ssl=False,
        ) as resp:
            text = await self.url_resolver.get_text(resp)
            logger.debug(f"[SSB 下载] 回帖响应长度: {len(text)}")
            url, msg = self._parse_succeedhandle_register(text)
            if url or msg:
                logger.debug(f"[SSB 下载] 回帖响应: {msg or '无文案'}")
                return True, msg or "回帖成功"
            msg_text = self._extract_inajax_message(text)
            preview = (msg_text or text).strip().replace("\n", " ")[:200]
            logger.warning(f"[SSB 下载] 回帖响应未识别: {preview}")
            return False, msg_text or "回帖失败"

    async def purchase_attachment(
        self,
        session: aiohttp.ClientSession,
        post_url: str,
        aid: str,
        html: str,
        user_id: str,
        is_admin: bool,
    ) -> tuple[bool, str, Optional[str], int, Optional[int]]:
        tid, _fid = self._parse_tid_fid(html)
        if not tid:
            return False, "未解析到 tid", None, 0, None

        # 先 GET 弹窗获取 formhash/余额
        pre_url = (
            f"https://{urlparse(post_url).netloc}/forum.php"
            f"?mod=misc&action=attachpay&aid={aid}&tid={tid}"
            f"&infloat=yes&handlekey=attachpay&inajax=1&ajaxtarget=fwin_content_attachpay"
        )
        logger.debug(f"[SSB 下载] 预取购买弹窗: aid={aid}, tid={tid}")
        async with session.get(
            pre_url,
            headers={**self.headers, "Referer": post_url, "X-Requested-With": "XMLHttpRequest"},
            timeout=20,
            ssl=False,
        ) as resp:
            pre_text = await self.url_resolver.get_text(resp)
        pre_html = self._extract_cdata_html(pre_text)
        formhash = self._parse_formhash(pre_html)
        if not formhash:
            msg_text = self._extract_inajax_message(pre_text)
            return False, msg_text or "预取购买信息失败", None, 0, None

        info = self._parse_attachpay_info(pre_html)
        price = (
            info.get("售价(银币)")
            or info.get("售价(金钱)")
            or info.get("售价")
        )
        balance = (
            info.get("购买后余额(银币)")
            or info.get("购买后余额(金钱)")
            or info.get("购买后余额")
        )
        if price or balance:
            logger.debug(f"[SSB 下载] 购买信息: 售价={price or '-'} 余额={balance or '-'}")
        coin_cost = self._parse_coin_value(price)
        ok_budget, budget_msg, remain_after = await self._consume_coin_budget(
            user_id=user_id,
            is_admin=is_admin,
            cost=coin_cost,
        )
        if not ok_budget:
            budget_msg = budget_msg.replace("金币", "银币")
            return False, budget_msg, None, 0, remain_after

        payload = {
            "formhash": formhash,
            "referer": post_url,
            "aid": aid,
            "handlekey": "register",
        }
        data = urlencode(payload, encoding="gbk").encode("gbk")
        pay_url = (
            f"https://{urlparse(post_url).netloc}/forum.php"
            f"?mod=misc&action=attachpay&tid={tid}"
            f"&paysubmit=yes&infloat=yes&inajax=1"
        )
        logger.debug(f"[SSB 下载] 尝试购买附件: aid={aid}, tid={tid}")
        async with session.post(
            pay_url,
            data=data,
            headers={
                **self.headers,
                "Referer": post_url,
                "Origin": f"https://{urlparse(post_url).netloc}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=20,
            ssl=False,
        ) as resp:
            text = await self.url_resolver.get_text(resp)
            logger.debug(f"[SSB 下载] 购买响应长度: {len(text)}")
            url, msg = self._parse_succeedhandle_register(text)
            if url or msg:
                logger.debug(f"[SSB 下载] 购买响应: {msg or '无文案'}")
                download_url = urljoin(post_url, url) if url else None
                return True, msg or "购买成功", download_url, coin_cost, remain_after
            msg_text = self._extract_inajax_message(text)
            preview = (msg_text or text).strip().replace("\n", " ")[:200]
            logger.warning(f"[SSB 下载] 购买响应未识别: {preview}")
            return False, msg_text or "购买失败", None, 0, remain_after

    def _extract_download_url(self, html: str, post_url: str, aid: Optional[str]) -> Optional[str]:
        if not aid:
            return None
        soup = BeautifulSoup(html, "lxml")
        anchor = None
        attach_span = soup.find("span", id=f"attach_{aid}")
        if attach_span:
            anchor = attach_span.find("a", href=True)
        if not anchor:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "mod=attachment" in href and "aid=" in href:
                    parsed = parse_qs(urlparse(href).query)
                    if parsed.get("aid", [None])[0] == aid:
                        anchor = a
                        break
        if anchor and anchor.get("href"):
            return urljoin(post_url, anchor["href"])
        return None

    async def ensure_login(
        self, session: aiohttp.ClientSession, base_url: str
    ) -> tuple[bool, str]:
        ssb_auth = self.search_service.plugin_config.get("ssb_auth", "")
        if not ssb_auth or "&" not in ssb_auth:
            return False, "请先在插件配置中设置 ssb_auth (格式: 账号&密码)。"

        username, password = ssb_auth.split("&", 1)

        cookies = self.search_service._load_ssb_cookies(username)
        if cookies:
            session.cookie_jar.update_cookies(cookies)
            logger.debug(f"[SSB 下载] 已加载账号 {username} 的历史 Cookie")

        check_url = urljoin(base_url, "home.php?mod=spacecp")
        try:
            async with session.get(
                check_url, headers=self.headers, timeout=10, ssl=False
            ) as resp:
                final_url = str(resp.url)
                html = await self.url_resolver.get_text(resp)
                if "登录" not in final_url and username in html:
                    logger.debug(f"[SSB 下载] Cookie 验证有效: {username}")
                    return True, "已登录"
        except Exception as e:
            logger.warning(f"[SSB 下载] Cookie 验证异常: {e}")

        logger.debug(f"[SSB 下载] Cookie 失效或未登录，尝试登录: {username}")
        if await self.search_service._ssb_login(session, base_url, username, password):
            return True, "登录成功"
        return False, "登录失败，请检查账密配置。"

    async def download_attachment(
        self,
        session: aiohttp.ClientSession,
        attachment: SsbAttachment,
        user_id: str,
        is_admin: bool,
    ) -> tuple[bool, str, Optional[str], int, Optional[int]]:
        post_url = attachment.post_url or attachment.url
        download_url = attachment.download_url or attachment.url
        spent_coin = 0
        remain_after = None

        html = ""
        if post_url:
            html, final_url = await self._fetch_post_html(session, post_url)
            gated_hints = self._detect_gated_content(html)
            if gated_hints:
                logger.debug(f"[SSB 下载] 帖子可能受限: {'; '.join(gated_hints)}")

        if attachment.aid and html:
            resolved = self._extract_download_url(html, post_url, attachment.aid)
            if resolved:
                download_url = resolved

        if html and self._needs_reply_unlock(html) and not download_url:
            logger.debug("[SSB 下载] 检测到隐藏内容，尝试回帖解锁")
            await self.reply_to_unlock(session, post_url, html)
            html, _ = await self._fetch_post_html(session, post_url)
            if attachment.aid:
                resolved = self._extract_download_url(html, post_url, attachment.aid)
                if resolved:
                    download_url = resolved

        if attachment.pay_url and attachment.aid and html:
            logger.debug(f"[SSB 下载] 附件可能需要购买: aid={attachment.aid}")
            ok, msg, direct_url, spent_coin, remain_after = await self.purchase_attachment(
                session, post_url, attachment.aid, html, user_id, is_admin
            )
            if not ok:
                return False, f"购买失败：{msg}", None, spent_coin, remain_after
            if direct_url:
                download_url = direct_url
            else:
                html, _ = await self._fetch_post_html(session, post_url)
                resolved = self._extract_download_url(html, post_url, attachment.aid)
                if resolved:
                    download_url = resolved

        filename = self._sanitize_filename(attachment.name)
        ts = int(time.time())
        file_path = os.path.join(self.download_dir, f"{ts}_{filename}")
        logger.debug(f"[SSB 下载] 开始下载: {download_url} -> {file_path}")

        async with session.get(
            download_url, headers={**self.headers, "Referer": post_url}, timeout=30, ssl=False
        ) as resp:
            if resp.status >= 400:
                return False, f"下载失败 HTTP {resp.status}", None, spent_coin, remain_after
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type:
                html = await self.url_resolver.get_text(resp)
                hints = self._detect_gated_content(html)
                hint_msg = "; ".join(hints) if hints else "返回HTML页面，可能需要回复或购买附件"
                return False, hint_msg, None, spent_coin, remain_after

            with open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    f.write(chunk)

        if os.path.exists(file_path):
            logger.debug(f"[SSB 下载] 下载完成: {file_path}")
            if remain_after is None and (not is_admin) and self._get_user_coin_limit() > 0:
                usage = await self._load_coin_usage()
                user_spent = int(usage["users"].get(str(user_id), 0))
                remain_after = max(self._get_user_coin_limit() - user_spent, 0)
            return True, "下载成功", file_path, spent_coin, remain_after
        return False, "下载失败，文件未落盘", None, spent_coin, remain_after

    async def schedule_cleanup(self, file_path: str) -> None:
        if self.cleanup_delay <= 0:
            return
        delay = self.cleanup_delay
        logger.debug(f"[SSB 下载] 文件将在 {delay} 秒后清理: {file_path}")
        await asyncio.sleep(delay)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.debug(f"[SSB 下载] 已清理文件: {file_path}")
        except Exception as e:
            logger.warning(f"[SSB 下载] 清理文件失败: {e}")
