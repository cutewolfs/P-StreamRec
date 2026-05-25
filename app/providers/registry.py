from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from .base import BaseProvider
from .browser import BrowserCaptureProvider
from .builtin import CAM4Provider, ChaturbateProvider
from .sessions import ProviderSessionStore
from .ytdlp import YtDlpProvider


class ProviderRegistry:
    def __init__(self, providers: Optional[Iterable[BaseProvider]] = None):
        self._providers: dict[str, BaseProvider] = {}
        for provider in providers or []:
            self.register(provider)

    def register(self, provider: BaseProvider) -> None:
        self._providers[provider.source_type] = provider

    def get(self, source_type: str) -> BaseProvider:
        key = (source_type or "").strip().lower()
        if key not in self._providers:
            raise KeyError(key)
        return self._providers[key]

    def has(self, source_type: str) -> bool:
        return (source_type or "").strip().lower() in self._providers

    def source_types(self) -> set[str]:
        return set(self._providers.keys())

    def all(self) -> list[BaseProvider]:
        return list(self._providers.values())

    def metadata(self) -> list[dict]:
        return [provider.metadata() for provider in self.all()]


def create_provider_registry(
    db,
    chaturbate_api=None,
    chaturbate_auth=None,
    cam4_auth=None,
    output_dir: Optional[Path] = None,
) -> ProviderRegistry:
    output_dir = Path(output_dir or "data")
    store = ProviderSessionStore(db)
    browser_root = output_dir / "provider-browser"
    registry = ProviderRegistry()

    registry.register(ChaturbateProvider(chaturbate_api, chaturbate_auth, store))
    registry.register(CAM4Provider(cam4_auth, store))

    def browser(
        source_type,
        display,
        templates,
        domains,
        login_templates=None,
        discover_templates=None,
        can_stream=True,
        can_record=True,
    ):
        return BrowserCaptureProvider(
            source_type=source_type,
            display_name=display,
            url_templates=templates,
            domains=domains,
            session_store=store,
            browser_root=browser_root,
            login_templates=login_templates,
            discover_templates=discover_templates,
            can_stream=can_stream,
            can_record=can_record,
        )

    stripchat_browser = browser(
        "stripchat",
        "Stripchat",
        ("https://stripchat.com/{username}",),
        ("stripchat.com",),
        ("https://stripchat.com/login", "https://stripchat.com/{username}"),
        ("https://stripchat.com/", "https://stripchat.com/girls", "https://stripchat.com/search/{query}"),
    )
    registry.register(
        YtDlpProvider(
            "stripchat",
            "Stripchat",
            "https://stripchat.com/{username}",
            ("stripchat.com",),
            store,
            browser_fallback=stripchat_browser,
        )
    )

    bongacams_browser = browser(
        "bongacams",
        "BongaCams",
        ("https://bongacams.com/{username}", "https://www.bongacams.com/{username}"),
        ("bongacams.com",),
        ("https://bongacams.com/login", "https://www.bongacams.com/login"),
        ("https://bongacams.com/", "https://www.bongacams.com/", "https://bongacams.com/search/{query}"),
    )
    registry.register(
        YtDlpProvider(
            "bongacams",
            "BongaCams",
            "https://bongacams.com/{username}",
            ("bongacams.com",),
            store,
            browser_fallback=bongacams_browser,
        )
    )

    camsoda_browser = browser(
        "camsoda",
        "CamSoda",
        ("https://www.camsoda.com/{username}", "https://camsoda.com/{username}"),
        ("camsoda.com",),
        ("https://www.camsoda.com/login", "https://camsoda.com/login"),
        ("https://www.camsoda.com/", "https://www.camsoda.com/girls", "https://www.camsoda.com/search/{query}"),
    )
    registry.register(
        YtDlpProvider(
            "camsoda",
            "CamSoda",
            "https://www.camsoda.com/{username}",
            ("camsoda.com",),
            store,
            browser_fallback=camsoda_browser,
        )
    )

    registry.register(browser(
        "myfreecams",
        "MyFreeCams",
        ("https://www.myfreecams.com/#{username}", "https://mfc.im/{username}/chat"),
        ("myfreecams.com", "mfc.im"),
        ("https://www.myfreecams.com/",),
        ("https://www.myfreecams.com/", "https://mfc.im/{query}/chat"),
    ))
    registry.register(browser(
        "livejasmin",
        "LiveJasmin",
        ("https://www.livejasmin.com/en/girls/{username}", "https://www.livejasmin.com/en/chat/{username}"),
        ("livejasmin.com",),
        ("https://www.livejasmin.com/en/login", "https://www.livejasmin.com/en/girls/{username}"),
        ("https://www.livejasmin.com/en/girls", "https://www.livejasmin.com/en/search?query={query}"),
    ))
    registry.register(browser(
        "streamate",
        "Streamate",
        ("https://streamate.com/cam/{username}",),
        ("streamate.com",),
        ("https://streamate.com/member/login", "https://streamate.com/cam/{username}"),
        ("https://streamate.com/cam", "https://streamate.com/search?query={query}"),
    ))
    registry.register(browser(
        "flirt4free",
        "Flirt4Free",
        (
            "https://www.flirt4free.com/search?q={username}",
            "https://www.flirt4free.com/models/{username}.html",
            "https://www.flirt4free.com/{username}",
        ),
        ("flirt4free.com",),
        ("https://www.flirt4free.com/login", "https://www.flirt4free.com/search?q={username}"),
        ("https://www.flirt4free.com/live/girls/", "https://www.flirt4free.com/search?q={query}"),
    ))
    registry.register(browser(
        "cams",
        "Cams.com",
        ("https://cams.com/{username}",),
        ("cams.com",),
        ("https://cams.com/login", "https://cams.com/{username}"),
        ("https://cams.com/", "https://cams.com/search?q={query}"),
    ))
    registry.register(browser(
        "xcams",
        "Xcams",
        ("https://www.xcams.com/chat/{username}", "https://www.xcams.com/{username}", "https://xcams.com/{username}"),
        ("xcams.com",),
        ("https://www.xcams.com/login", "https://www.xcams.com/{username}"),
        ("https://www.xcams.com/", "https://www.xcams.com/search/{query}"),
    ))
    return registry
