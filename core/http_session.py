from __future__ import annotations
from collections.abc import Callable
import aiohttp


class ProxyError(Exception):
    """Raised when the proxy itself is unreachable or returns an error."""

    def __init__(self, message: str, original_exc: BaseException | None = None):
        super().__init__(message)
        self.original_exc = original_exc


def is_definitely_proxy_error(exc: BaseException) -> bool:
    return isinstance(
        exc, (aiohttp.ClientProxyConnectionError, aiohttp.ClientHttpProxyError)
    )


def is_possibly_proxy_error(exc: BaseException) -> bool:
    return isinstance(exc, aiohttp.ClientConnectionError)


def is_proxy_configured(plugin_config: dict | None) -> bool:
    return bool(str((plugin_config or {}).get("proxy_url", "") or "").strip())


class ProxyClientSession:
    """Light wrapper that injects a default proxy into all requests."""

    def __init__(self, *args, proxy_url: str = "", **kwargs) -> None:
        self._default_proxy = str(proxy_url or "").strip() or None
        self._session = aiohttp.ClientSession(*args, **kwargs)

    def _merge_request_kwargs(self, kwargs: dict) -> dict:
        if self._default_proxy and "proxy" not in kwargs:
            kwargs["proxy"] = self._default_proxy
        return kwargs

    def request(self, method: str, url: str, **kwargs):
        return self._session.request(method, url, **self._merge_request_kwargs(kwargs))

    def get(self, url: str, **kwargs):
        return self._session.get(url, **self._merge_request_kwargs(kwargs))

    def post(self, url: str, **kwargs):
        return self._session.post(url, **self._merge_request_kwargs(kwargs))

    async def close(self) -> None:
        await self._session.close()

    async def __aenter__(self) -> "ProxyClientSession":
        await self._session.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return await self._session.__aexit__(exc_type, exc, tb)

    def __getattr__(self, item):
        return getattr(self._session, item)


def build_session_factory(
    plugin_config: dict | None,
) -> Callable[..., ProxyClientSession]:
    proxy_url = str((plugin_config or {}).get("proxy_url", "") or "").strip()

    def _factory(*args, **kwargs) -> ProxyClientSession:
        return ProxyClientSession(*args, proxy_url=proxy_url, **kwargs)

    return _factory
