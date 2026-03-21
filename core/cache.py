from dataclasses import dataclass, field
from typing import List, Optional
import time


@dataclass
class SsbSearchItem:
    title: str
    link: str
    time_text: str = "未知"


@dataclass
class SsbAttachment:
    name: str
    url: str
    aid: str | None = None
    pay_url: str | None = None
    download_url: str | None = None
    post_url: str | None = None


@dataclass
class UserSearchState:
    search_items: List[SsbSearchItem] = field(default_factory=list)
    pending_post: Optional[SsbSearchItem] = None
    pending_attachments: List[SsbAttachment] = field(default_factory=list)
    updated_at: float = field(default_factory=lambda: time.time())


class UserSearchCache:
    def __init__(self, ttl_seconds: int = 1800):
        self._ttl_seconds = ttl_seconds
        self._data: dict[str, UserSearchState] = {}

    def _is_expired(self, state: UserSearchState) -> bool:
        return (time.time() - state.updated_at) > self._ttl_seconds

    def _get_state(self, user_id: str) -> UserSearchState:
        state = self._data.get(user_id)
        if state and self._is_expired(state):
            self._data.pop(user_id, None)
            state = None
        if not state:
            state = UserSearchState()
            self._data[user_id] = state
        return state

    def set_search_items(self, user_id: str, items: List[SsbSearchItem]) -> None:
        state = self._get_state(user_id)
        state.search_items = items
        state.pending_post = None
        state.pending_attachments = []
        state.updated_at = time.time()

    def get_search_items(self, user_id: str) -> List[SsbSearchItem]:
        state = self._get_state(user_id)
        return state.search_items

    def set_pending_attachments(
        self,
        user_id: str,
        post: SsbSearchItem,
        attachments: List[SsbAttachment],
    ) -> None:
        state = self._get_state(user_id)
        state.pending_post = post
        state.pending_attachments = attachments
        state.updated_at = time.time()

    def get_pending_attachments(self, user_id: str) -> List[SsbAttachment]:
        state = self._get_state(user_id)
        return state.pending_attachments

    def clear_pending_attachments(self, user_id: str) -> None:
        state = self._get_state(user_id)
        state.pending_post = None
        state.pending_attachments = []
        state.updated_at = time.time()
