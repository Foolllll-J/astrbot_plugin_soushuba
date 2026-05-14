import asyncio
import aiohttp
from astrbot.api import logger
import astrbot.api.message_components as Comp

from .cache import UserSearchCache, SsbSearchItem, SsbAttachment
from .http_session import ProxyClientSession
from .search_service import SearchService
from .download_service import SsbDownloadService


class SxsyFlow:
    def __init__(
        self,
        search_service: SearchService,
        download_service: SsbDownloadService,
        cache: UserSearchCache,
        session_factory,
    ):
        self.search_service = search_service
        self.download_service = download_service
        self.cache = cache
        self.session_factory = session_factory

    def _get_user_id(self, event) -> str:
        return str(event.get_sender_id())

    def _is_index_arg(self, arg: str) -> bool:
        return arg.isdigit()

    def _is_url_arg(self, arg: str) -> bool:
        return arg.startswith("http://") or arg.startswith("https://")

    async def _handle_search(self, event, keyword: str):
        yield event.plain_result(f"🔍 正在尚香书苑搜索: {keyword}...")
        async with self.session_factory() as session:
            ok, message, items = await self.search_service.sxsy_search(session, keyword)
            yield event.plain_result(message)
            if not ok:
                return
            user_id = self._get_user_id(event)
            self.cache.set_search_items(user_id, items)
            logger.debug(
                f"[SXSY 缓存] 用户 {user_id} 缓存搜索结果 {len(items)} 条"
            )

    async def _handle_post_selection(self, event, index: int, post: SsbSearchItem | None = None):
        if not self.search_service.is_download_allowed(
            self._get_user_id(event), event.is_admin()
        ):
            yield event.plain_result("抱歉，你没有权限使用下载附件功能。")
            return
        user_id = self._get_user_id(event)
        if post is None:
            items = self.cache.get_search_items(user_id)
            if not items:
                yield event.plain_result("未找到可用的搜索结果，请先执行一次 /sxsy 关键词 搜索。")
                return
            if index < 1 or index > len(items):
                yield event.plain_result("选择序号超出范围，请重新选择。")
                return
            post = items[index - 1]

        async with self.session_factory() as session:
            attachments = await self.download_service.fetch_sxsy_post_attachments(
                session, post.link
            )
            if not attachments:
                yield event.plain_result("❌ 未解析到附件，可能帖子无附件或 Cookie 已失效。")
                return

            if len(attachments) == 1:
                yield event.plain_result("检测到 1 个附件，开始下载...")
                results = await self._download_and_send(event, session, attachments[0])
                for result in results:
                    yield result
                return

            self.cache.set_pending_attachments(user_id, post, attachments)
            lines = []
            for idx, att in enumerate(attachments, 1):
                lines.append(f"【{idx}】{att.name}")
            tips = "\n".join(lines)
            yield event.plain_result(
                "检测到多个附件，请发送 /sxsy 序号 下载对应文件，"
                "或 /sxsy 0 下载全部：\n" + tips
            )

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

        async with self.session_factory() as session:
            if index == 0:
                yield event.plain_result(f"开始下载全部附件，共 {len(attachments)} 个...")
                for att in attachments:
                    results = await self._download_and_send(event, session, att)
                    for result in results:
                        yield result
                self.cache.clear_pending_attachments(user_id)
                return

            if index < 1 or index > len(attachments):
                yield event.plain_result("附件序号超出范围，请重新选择。")
                return

            att = attachments[index - 1]
            results = await self._download_and_send(event, session, att)
            for result in results:
                yield result
            self.cache.clear_pending_attachments(user_id)

    async def _download_and_send(
        self, event, session: ProxyClientSession, att: SsbAttachment
    ) -> list:
        ok, msg, file_path, spent_coin, remain_after = await self.download_service.download_sxsy_attachment(
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
            tip = f"（本次下载花费 {spent_coin} 金钱，今日还可花费 {remain_show} 金钱）"
        chain = [
            Comp.Plain(f"✅ 已下载：{att.name}{tip}"),
            Comp.File(name=att.name, file=file_path),
        ]
        try:
            await event.send(event.chain_result(chain))
        except Exception as e:
            err = str(e)
            if "rich media transfer failed" in err or "retcode=1200" in err:
                fallback = (
                    f"⚠️ {att.name} 文件发送失败，建议自行通过网站下载。"
                )
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

    async def handle(self, event, arg: str):
        arg = arg.strip()
        if self._is_url_arg(arg):
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
