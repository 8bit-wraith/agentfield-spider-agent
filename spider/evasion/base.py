"""EvasionBase — composable bot-detection countermeasures.

Applies to a ``BrowserContext`` + ``Page`` returned from any backend
(cloak / lightpanda). Internals live in sibling modules:

  identity.py    persona presets (UA, locale, TZ, viewport, geo,
                 Accept-Language, Sec-CH-UA) pinned together for one
                 session.
  network.py     WebRTC IP-leak block.
  behavioral.py  post-goto dwell + scroll + small mouse jitter that
                 look like a human reading the page.
  rate.py        per-session token bucket so one session can't burn
                 its credibility with too many requests.

Four lifecycle hooks
--------------------

  context_kwargs()
    Kwargs to merge into ``browser.new_context(**kwargs)``. Persona-
    derived: user_agent, viewport, locale, timezone_id, geolocation,
    extra_http_headers.

  apply_to_context(context)
    One-shot setup on the freshly created context. Adds init scripts
    (WebRTC block, etc.) that run before any page script on every
    navigation.

  pre_goto(page, target_url)
    Runs before each target ``page.goto()``. Optional session warm-up
    that establishes basic navigation history in the same browser
    context (e.g. search -> click path) before the real target load.

  post_goto(page)
    Runs after each successful ``page.goto()`` in the fetch pipeline.
    Light behavioral simulation. No-op when
    ``config.behavioral_dwell`` is False.

Plus ``check_rate()`` — gates ``page.goto`` on the per-session token
bucket. No-op when ``rate_per_min <= 0``.

Backend awareness
-----------------

Cloak already applies most fingerprint patches itself. EvasionBase
skips overlapping JS patches on the cloak backend to avoid signal
collision (double-patching is its own fingerprint). Lightpanda gets
the full treatment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote_plus, urlparse

from spider.evasion.identity import Persona, get_persona
from spider.evasion.network import WEBRTC_BLOCK_JS
from spider.evasion.rate import RateLimiter
from spider.log import log


@dataclass(slots=True)
class EvasionConfig:
    """User-tunable evasion configuration. One per session.

    ``persona`` is a named preset from ``identity.py`` (see
    ``list_personas()``). The persona pins UA + locale + TZ + viewport
    + geo + Sec-CH-UA as a coherent identity — never mix locales
    across the same session.

    ``rate_per_min`` is the per-session request budget. ``0`` (or any
    value < 1) disables the limiter. Recommended: 30-60 for crawls,
    200+ for short-lived bursts.

    ``backend`` is the canonical name of the backend the session runs
    on. Passed by ``SessionManager.create()``; used here to skip
    overlapping techniques (mouse on lightpanda, WebRTC block on
    cloak).
    """

    persona: str | None = None
    block_webrtc: bool = True
    behavioral_dwell: bool = True
    rate_per_min: int = 60
    backend: str = "cloak"
    extra_headers: dict[str, str] = field(default_factory=dict)
    warming: Literal["off", "search-click", "hub"] = "off"
    warming_queries: list[str] | None = None


_WARMUP_TIMEOUT_MS = 10_000
_HUB_WARMUP_URLS: tuple[str, ...] = ("https://dev.to/", "https://github.com/trending")
_SEARCH_CLICK_SELECTORS: tuple[str, ...] = (
    "a[href^='https://']:not([href*='google.'])",
    "a[href^='http://']:not([href*='google.'])",
)


def _google_domain_for_locale(locale: str | None) -> str:
    """Map locale to a reasonable Google domain."""
    if not locale:
        return "google.com"

    lower = locale.lower()
    if lower.startswith("en-gb"):
        return "google.co.uk"
    if lower.startswith("pt-br"):
        return "google.com.br"

    language = lower.split("-", 1)[0]
    by_language = {
        "de": "google.de",
        "fr": "google.fr",
        "es": "google.es",
        "it": "google.it",
        "pt": "google.pt",
        "nl": "google.nl",
    }
    return by_language.get(language, "google.com")


class EvasionBase:
    """Evasion techniques applied to one BrowserContext + its pages.

    Constructed by ``SessionManager.create()`` and stored on the
    Session record. The ``context_kwargs()`` return value is merged
    into ``browser.new_context(**kwargs)``; ``apply_to_context()``
    runs once on the context; ``post_goto()`` runs after every
    ``page.goto()``.
    """

    __slots__ = ("config", "persona", "_rate")

    def __init__(self, config: EvasionConfig | None = None) -> None:
        self.config = config or EvasionConfig()
        self.persona: Persona | None = (
            get_persona(self.config.persona) if self.config.persona else None
        )
        # Disabled when rate_per_min <= 0; RateLimiter rejects < 1.
        self._rate: RateLimiter | None = (
            RateLimiter(self.config.rate_per_min) if self.config.rate_per_min > 0 else None
        )

    # ─── lifecycle hooks ────────────────────────────────────────────
    def context_kwargs(self) -> dict[str, Any]:
        """Return kwargs to merge into ``browser.new_context(**kwargs)``.

        Persona-derived; empty dict when no persona is selected.
        Callers merge with explicit user kwargs — explicit user kwargs
        should win (caller's responsibility).
        """
        if self.persona is None:
            return {}
        return self.persona.context_kwargs(extra_headers=self.config.extra_headers)

    async def apply_to_context(self, context: Any) -> None:
        """Apply one-shot techniques to a freshly created context.

        Currently:
          * WebRTC IP-leak block (skip on cloak — it has its own).

        Errors are logged but don't propagate; init-script failures
        on Lightpanda's limited CDP shouldn't kill the session.
        """
        if not self.config.block_webrtc:
            return
        # Cloak already applies its own WebRTC patches via the
        # CloakBrowser source-level mods. Double-patching from here
        # is detectable; skip.
        if self.config.backend == "cloak":
            return
        try:
            await context.add_init_script(WEBRTC_BLOCK_JS)
        except Exception as e:
            log(
                "evasion",
                state="webrtc-block-failed",
                backend=self.config.backend,
                msg=f"{type(e).__name__}: {e}",
            )

    async def check_rate(self) -> None:
        """Block until the per-session rate budget allows another request.

        No-op when the limiter is disabled. Called by the fetch
        pipeline before ``page.goto``.
        """
        if self._rate is not None:
            await self._rate.acquire()

    async def pre_goto(self, page: Any, target_url: str) -> None:
        """Warm session state before navigating to the target URL.

        Best effort by design: warm-up failures are logged and swallowed
        so target navigation can still proceed.
        """
        if self.config.warming == "off":
            return

        try:
            if self.config.warming == "search-click":
                await self._search_click_warmup(page, target_url)
            elif self.config.warming == "hub":
                await self._hub_warmup(page)
        except Exception as e:
            log(
                "evasion",
                state="pre-goto-warmup-failed",
                backend=self.config.backend,
                msg=f"{type(e).__name__}: {e}",
            )

    async def post_goto(self, page: Any) -> None:
        """Light behavioral simulation after ``page.goto`` succeeds.

        Backend-aware: ``dwell_and_scroll`` itself decides what to skip
        for lightpanda.
        """
        if not self.config.behavioral_dwell:
            return
        # Lazy import — behavioral functions stay private to that
        # module so callers don't depend on the impl.
        from .behavioral import dwell_and_scroll

        await dwell_and_scroll(page, backend=self.config.backend)

    async def _search_click_warmup(self, page: Any, target_url: str) -> None:
        domain = _google_domain_for_locale(self.persona.locale if self.persona else None)
        query = self._warmup_query(target_url)
        search_url = f"https://www.{domain}/search?q={quote_plus(query)}"
        await page.goto(
            search_url,
            wait_until="domcontentloaded",
            timeout=_WARMUP_TIMEOUT_MS,
        )

        # Lightpanda's click reliability is still partial; search-only
        # warmup still establishes plausible history without brittle
        # click behavior.
        if self.config.backend == "lightpanda":
            return

        for selector in _SEARCH_CLICK_SELECTORS:
            try:
                await page.click(selector, timeout=2_000)
                break
            except Exception:
                continue

    async def _hub_warmup(self, page: Any) -> None:
        await page.goto(
            _HUB_WARMUP_URLS[0],
            wait_until="domcontentloaded",
            timeout=_WARMUP_TIMEOUT_MS,
        )

    def _warmup_query(self, target_url: str) -> str:
        if self.config.warming_queries:
            for query in self.config.warming_queries:
                if query.strip():
                    return query.strip()
        host = urlparse(target_url).hostname
        if host:
            return host
        return "latest technology news"


__all__ = ["EvasionBase", "EvasionConfig"]
