import aiohttp
import asyncio
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.star.star_handler import EventType

from .cache import UserSearchCache, SsbSearchItem, SsbAttachment
from .http_session import ProxyError
from .search_service import SearchService
from .download_service import SsbDownloadService


class SsbFlow:
    def __init__(
        self,
        search_service: SearchService,
        download_service: SsbDownloadService,
        cache: UserSearchCache,
        target_domains: list[str],
        session_factory,
    ):
        self.search_service = search_service
        self.download_service = download_service
        self.cache = cache
        self.target_domains = target_domains
        self.session_factory = session_factory

    def _get_user_id(self, event) -> str:
        return str(event.get_sender_id())

    def _is_index_arg(self, arg: str) -> bool:
        return arg.isdigit()

    def _is_url_arg(self, arg: str) -> bool:
        return arg.startswith("http://") or arg.startswith("https://")

    async def _send_chain_with_hooks(self, event, chain: list):
        previous_result = event.get_result()
        result = event.chain_result(chain)
        event.set_result(result)
        try:
            if await call_event_hook(event, EventType.OnDecoratingResultEvent):
                return
            result = event.get_result()
            if not result or not result.chain:
                return
            await event.send(result.derive(result.chain))
            await call_event_hook(event, EventType.OnAfterMessageSentEvent)
        finally:
            if previous_result is None:
                event.clear_result()
            else:
                event.set_result(previous_result)

    async def _send_plain_immediately(self, event, text: str):
        await event.send(event.plain_result(text))

    async def _exec_ssb_search(self, keyword: str):
        """Execute ssb_search with proxy fallback. Returns (ok, message, items)."""
        try:
            async with self.session_factory() as session:
                return await self.search_service.ssb_search(
                    session, keyword, self.target_domains
                )
        except ProxyError:
            logger.warning("[SSB] 代理连接失败，回退直连搜索...")
            async with aiohttp.ClientSession() as session:
                return await self.search_service.ssb_search(
                    session, keyword, self.target_domains
                )

    async def _exec_find_ssb_url(self):
        """Execute find_ssb_latest_url with proxy fallback. Returns url or None."""
        try:
            async with self.session_factory() as session:
                return await self.search_service.find_ssb_latest_url(
                    session, self.target_domains
                )
        except ProxyError:
            logger.warning("[SSB] 代理连接失败，回退直连获取网址...")
            async with aiohttp.ClientSession() as session:
                return await self.search_service.find_ssb_latest_url(
                    session, self.target_domains
                )

    async def _exec_post_selection(self, session, event, post, user_id):
        """Run post-selection logic with a given session. Returns list of results."""
        results = []
        base_url = self.download_service.url_resolver.normalize_base_url(post.link)
        ok, msg = await self.download_service.ensure_login(session, base_url)
        logger.debug(f"[SSB 登录] {msg}")
        if not ok:
            return [event.plain_result(f"❌ {msg}")]

        attachments = await self.download_service.fetch_post_attachments(
            session, post.link
        )
        if not attachments:
            return [event.plain_result("❌ 未解析到附件，可能需要回复或购买附件。")]

        if len(attachments) == 1:
            await self._send_plain_immediately(event, "检测到 1 个附件，开始下载...")
            download_results = await self._download_and_send(
                event, session, attachments[0]
            )
            results.extend(download_results)
            return results

        self.cache.set_pending_attachments(user_id, post, attachments)
        lines = []
        for idx, att in enumerate(attachments, 1):
            lines.append(f"【{idx}】{att.name}")
        tips = "\n".join(lines)
        return [
            event.plain_result(
                "检测到多个附件，请发送 /ssb 序号 下载对应文件，"
                "或 /ssb 0 下载全部：\n" + tips
            )
        ]

    async def _handle_search(self, event, keyword: str):
        if not self.search_service.check_ssb_rate_limit(40):
            yield event.plain_result("搜书吧在 40 秒内只能进行一次搜索")
            return

        yield event.plain_result(f"🔍 正在搜书吧搜索: {keyword}...")
        ok, message, items = await self._exec_ssb_search(keyword)
        yield event.plain_result(message)
        if not ok:
            return
        user_id = self._get_user_id(event)
        self.cache.set_search_items(user_id, items)
        logger.debug(f"[SSB 缓存] 用户 {user_id} 缓存搜索结果 {len(items)} 条")

    async def _handle_post_selection(
        self, event, index: int, post: SsbSearchItem | None = None
    ):
        if not self.search_service.is_download_allowed(
            self._get_user_id(event), event.is_admin()
        ):
            yield event.plain_result("抱歉，你没有权限使用下载附件功能。")
            return
        user_id = self._get_user_id(event)
        if post is None:
            items = self.cache.get_search_items(user_id)
            if not items:
                yield event.plain_result(
                    "未找到可用的搜索结果，请先执行一次 /ssb 关键词 搜索。"
                )
                return
            if index < 1 or index > len(items):
                yield event.plain_result("选择序号超出范围，请重新选择。")
                return
            post = items[index - 1]
        logger.debug(f"[SSB 选择] 用户 {user_id} 选择帖子: {post.title} {post.link}")
        try:
            async with self.session_factory() as session:
                results = await self._exec_post_selection(session, event, post, user_id)
        except ProxyError:
            logger.warning("[SSB] 代理连接失败，回退直连获取帖子信息...")
            async with aiohttp.ClientSession() as session:
                results = await self._exec_post_selection(session, event, post, user_id)
        for result in results:
            yield result

    async def _exec_attachment_selection(
        self, session, event, index, user_id, attachments
    ):
        """Run attachment-selection logic with a given session. Returns list of results."""
        results = []
        if index == 0:
            await self._send_plain_immediately(
                event, f"开始下载全部附件，共 {len(attachments)} 个..."
            )
            for att in attachments:
                download_results = await self._download_and_send(event, session, att)
                results.extend(download_results)
            self.cache.clear_pending_attachments(user_id)
            return results

        if index < 1 or index > len(attachments):
            return [event.plain_result("附件序号超出范围，请重新选择。")]

        att = attachments[index - 1]
        download_results = await self._download_and_send(event, session, att)
        results.extend(download_results)
        self.cache.clear_pending_attachments(user_id)
        return results

    async def _handle_attachment_selection(self, event, index: int):
        if not self.search_service.is_download_allowed(
            self._get_user_id(event), event.is_admin()
        ):
            yield event.plain_result("抱歉，你没有权限使用下载附件功能。")
            return
        user_id = self._get_user_id(event)
        attachments = self.cache.get_pending_attachments(user_id)
        if not attachments:
            yield event.plain_result("当前没有待选择的附件，请先选择帖子。")
            return

        try:
            async with self.session_factory() as session:
                results = await self._exec_attachment_selection(
                    session, event, index, user_id, attachments
                )
        except ProxyError:
            logger.warning("[SSB] 代理连接失败，回退直连下载附件...")
            async with aiohttp.ClientSession() as session:
                results = await self._exec_attachment_selection(
                    session, event, index, user_id, attachments
                )
        for result in results:
            yield result

    async def _download_and_send(
        self, event, session: aiohttp.ClientSession, att: SsbAttachment
    ) -> list:
        (
            ok,
            msg,
            file_path,
            spent_coin,
            remain_after,
        ) = await self.download_service.download_attachment(
            session, att, self._get_user_id(event), event.is_admin()
        )
        if not ok:
            msg_text = str(msg)
            if "购买失败：" in msg_text:
                msg_text = msg_text[msg_text.find("购买失败：") :]
                return [event.plain_result(f"❌ {att.name} {msg_text}")]
            return [event.plain_result(f"❌ {att.name} 下载失败：{msg}")]
        tip = ""
        user_limit = self.download_service._get_user_coin_limit()
        if (not event.is_admin()) and user_limit > 0:
            remain_show = remain_after if remain_after is not None else user_limit
            tip = f"（本次下载花费 {spent_coin} 银币，今日还可花费 {remain_show} 银币）"
        chain = [
            Comp.Plain(f"✅ 已下载：{att.name}{tip}"),
            Comp.File(name=att.name, file=file_path),
        ]
        try:
            await self._send_chain_with_hooks(event, chain)
        except Exception as e:
            err = str(e)
            if "rich media transfer failed" in err or "retcode=1200" in err:
                fallback = f"⚠️ {att.name} 文件发送失败，建议自行通过网站下载。"
                try:
                    await event.send(event.plain_result(fallback))
                    return []
                except Exception:
                    return [event.plain_result(fallback)]
            return [event.plain_result(f"❌ {att.name} 发送失败：{err}")]

        try:
            asyncio.get_running_loop().create_task(
                self.download_service.schedule_cleanup(file_path)
            )
        except RuntimeError:
            pass
        return []

    async def handle(self, event, arg: str | None):
        if not arg:
            link_url = await self._exec_find_ssb_url()
            if link_url:
                yield event.plain_result(f"📖 成功找到搜书吧最新网址：\n{link_url}")
                return
            yield event.plain_result(
                "❌ 抱歉，所有导航网站均无法访问或未找到可用链接。"
            )
            return

        arg = arg.strip()
        if self._is_url_arg(arg):
            # 直接用 URL 走附件下载流程
            post = SsbSearchItem(title="直接链接", link=arg, time_text="")
            async for result in self._handle_post_selection(event, 1, post=post):
                yield result
            return

        if self._is_index_arg(arg):
            index = int(arg)
            pending = self.cache.get_pending_attachments(self._get_user_id(event))
            if pending:
                async for result in self._handle_attachment_selection(event, index):
                    yield result
                return
            async for result in self._handle_post_selection(event, index):
                yield result
            return

        async for result in self._handle_search(event, arg):
            yield result
