#!/usr/bin/env python3
"""opensea-scraper — pyppeteer edition

The same three modes, the same rows and the same exit codes as
`playwright_scraper.py`; only the driver differs.

    --mode items        (default)  /collection/{slug} — one row per NFT
    --mode collections             /collections — OpenSea's own ranking
    --mode activity                /collection/{slug}/activity — one row per
                                   sale, listing, offer, transfer or mint

pyppeteer is EFFECTIVELY UNMAINTAINED and its own README points at
Playwright. It is here for parity — a second independent implementation the
primary engine has to agree with — and because an authenticated remote CDP
endpoint works from it, which is something Selenium cannot do.

pyppeteer is async and `page_flow.py` is not. Rather than grow an async copy
of the shared policy, which would drift, this engine runs pyppeteer's
coroutines on a private event loop through `_AsyncBridge` and hands
`page_flow` the same plain synchronous callables its twins do. The bridge
also gives every call an explicit, enforced timeout, which pyppeteer's own
API does not offer (§8: every remote call is bounded).

Example
-------
    python3 puppeteer_scraper.py \\
        --url "https://opensea.io/collection/boredapeyachtclub" --pages 3
"""

import argparse
import asyncio
import concurrent.futures
import json
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pyppeteer import launch, connect

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS, detect_turnstile,
                            wait_for_turnstile, TURNSTILE_INTERCEPT_JS,
                            TURNSTILE_INJECT_JS)
from product_parser import (parse_rows, rows_from_graphql, graphql_body,
                            graphql_errors, PAGE_CAP, MAX_LIMIT,
                            SORTS, DEFAULT_SORT, TIMEFRAMES, DEFAULT_TIMEFRAME,
                            RANKING_SLUGS, ACTIVITY_FILTERS,
                            DEFAULT_ACTIVITY_FILTER, GRAPHQL_ENDPOINT,
                            detect_bot_challenge, page_kind, normalize_url,
                            next_cursor, collection_slug_from_url, locale_of,
                            mode_for_url, served_by_opensea, source_of,
                            unsupported_reason, url_for_mode,
                            ssr_matches_request, ssr_mismatch_note)
from output_writer import merge_pages, finish_run, EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, split_credentials,
                        mask, ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

DEFAULT_BROWSER_CHANNEL = None
# See playwright_scraper.FIELD_FLOOR — the same number, measured on the same
# captures, and it must stay the same in all three engines.
FIELD_FLOOR = 95
# Every bridged call is bounded. 120s is generous against a 2.3 MB collection
# page and still returns control if the browser never answers.
DEFAULT_OP_TIMEOUT = 120
CDP_CONNECT_TIMEOUT_MS = 150_000
CONNECT_TIMEOUT = 150


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Exists so this engine can reuse page_flow.py unchanged. That module holds
    the policy all three engines must share, and it is written against plain
    synchronous callables — the right shape for two of the three drivers.
    Bridging here keeps the policy in one place rather than growing an async
    copy of it that would drift.

    The second benefit is what the family's rules actually require: every
    call gets an explicit, enforced timeout. `.result(timeout)` returns
    control even when the browser never answers.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each one at ERROR level, AFTER a successful run has
        # printed its results. Five of those under a "Saved 250 products" line
        # read as a failed run. Only that shape is swallowed; anything else
        # still gets the default handler, because silencing the loop wholesale
        # would hide real faults.
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # BOTH, not one or the other. asyncio puts its own words in `message`
        # ("Future exception was never retrieved") and the library's in
        # `exception`, and an `or` between them looks at the exception and
        # never sees the message.
        message = " | ".join(
            str(context.get(k)) for k in ("exception", "message")
            if context.get(k))
        if any(m in message for m in (
                "Target closed", "Connection closed",
                "Task was destroyed but it is pending",
                "Future exception was never retrieved",
                "No session with given id",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING whatever it still has in flight.

        Stopping the loop outright leaves pyppeteer's background tasks
        pending — its websocket reader and keepalive — and asyncio then prints
        "Task was destroyed but it is pending!" plus a traceback for each.
        That happens AFTER the output is written, so the run is fine and the
        log looks like a crash. Cancelling first is the fix, and it has to
        happen ON the loop thread.
        """
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            if pending:
                logger.debug("Cancelled %d pending pyppeteer task(s) on "
                             "teardown.", len(pending))
            self.loop.stop()

        self.loop.call_soon_threadsafe(_cancel_and_stop)
        self._thread.join(timeout=5)


@dataclass
class PageOutcome:
    """What one page produced. Mirrors playwright_scraper.PageOutcome."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    next_cursor: Optional[str] = None
    totals: Optional[dict] = None
    gap: Optional[int] = None
    ssr_dropped: int = 0
    scroll: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version.

    `browser.version()` answers "HeadlessChrome/140.0.0.0", so the token is
    replaced: claiming HeadlessChrome is a giveaway on any site with a bot
    manager in front of it.
    """
    number = version.split("/")[-1] if "/" in version else version
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{number} Safari/537.36")


def _install_turnstile_intercept(session) -> None:
    """Hook `turnstile.render` before any page script can run.

    THE ONLY MOMENT a Cloudflare Turnstile's parameters can be captured: a
    Challenge page calls `turnstile.render(container, params)` once and keeps
    nothing, while `TurnstileTaskProxyless` needs the sitekey, action, cData
    and chlPageData that live only inside that call. pyppeteer spells it
    `evaluateOnNewDocument`; the other two engines spell the same thing
    `context.add_init_script` and `Page.addScriptToEvaluateOnNewDocument`,
    which is why this cannot live in the shared module (§1).
    """
    try:
        session.bridge.run(
            session.page.evaluateOnNewDocument(TURNSTILE_INTERCEPT_JS))
    except Exception as e:  # noqa: BLE001 — never break a run over this
        logger.debug("Could not install the Turnstile interception: %s", e)


class _Session:
    """One pyppeteer browser + page, relaunchable onto a different exit."""

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            # pyppeteer's browserWSEndpoint takes the full ws://user:pass@host
            # form and authenticates on the WebSocket upgrade, so an
            # authenticated Scraping Browser endpoint works here — unlike
            # Selenium's debuggerAddress, which has nowhere to put a password.
            self.browser = self.bridge.run(
                connect(browserWSEndpoint=self.args.cdp_endpoint,
                        ignoreHTTPSErrors=True),
                timeout=getattr(self.args, "cdp_connect_timeout",
                                CONNECT_TIMEOUT))
            self.page = self.bridge.run(self.browser.newPage())
            _install_turnstile_intercept(self)
            _watch_refusals(self)
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                       "--disable-blink-features=AutomationControlled"]
        if self.args.locale:
            launch_args.append(f"--lang={self.args.locale}")
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
            logger.info("Using the browser at %s instead of pyppeteer's own.",
                        self.args.chromium_path)
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= becomes part of the browser's
            # argv, readable by anything that can run `ps` (§8).
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))

        # handleSIGINT/TERM/HUP off, and not for tidiness: pyppeteer installs
        # signal handlers inside launch(), and `signal.signal` raises "signal
        # only works in main thread of the main interpreter" because the event
        # loop here lives on a worker thread.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        _install_turnstile_intercept(self)
        version = self.bridge.run(self.browser.version())
        self.bridge.run(self.page.setUserAgent(_chrome_ua(version)))
        # 1440x900, matching the captures the numbers in this repo come from.
        self.bridge.run(self.page.setViewport({"width": 1440, "height": 900}))
        if self.args.locale:
            # The launch flag sets the UI language; the header is what the
            # site actually reads. Both, because a browser claiming one and
            # sending the other is a mismatch of exactly the kind a
            # fingerprint is meant to avoid.
            self.bridge.run(self.page.setExtraHTTPHeaders(
                {"accept-language": self.args.locale}))
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        if self.args.fingerprint:
            self._apply_fingerprint()
        _watch_refusals(self)
        return self

    def _apply_fingerprint(self):
        """Apply a 2Captcha fingerprint to this page.

        THROUGH THE SHARED HELPERS, never by reaching into the API's response
        shape here: the user agent lives at `userAgent.userAgent` in one
        format and at `data.ua` in the other, and a key that exists in
        neither makes `--fingerprint` silently set no user agent at all —
        which defeats the flag rather than breaking it, and was live in four
        sibling repos at once (§16).

        The init script is the same one the Playwright engine installs on its
        context. Shared deliberately: two engines applying different halves
        of one fingerprint would be a contradiction of exactly the kind a
        fingerprint is meant to avoid.
        """
        from fingerprint_client import (get_fingerprint, fingerprint_user_agent,
                                        playwright_init_script)
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = fingerprint_user_agent(fp)
        try:
            if ua:
                self.bridge.run(self.page.setUserAgent(ua))
            self.bridge.run(self.page.evaluateOnNewDocument(
                playwright_init_script(fp)))
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not apply the fingerprint (%s) — continuing "
                           "without it.", e)

    def relaunch(self):
        if self.remote:
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to pyppeteer
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. pyppeteer takes `() => expr` like
# Playwright and unlike Selenium, which is exactly why page_flow names
# operations rather than passing JavaScript across the boundary (§1).
def _count(session, selector: str) -> int:
    try:
        return len(session.bridge.run(session.page.querySelectorAll(selector)))
    except Exception as e:  # noqa: BLE001
        logger.debug("count(%s) failed: %s", selector, e)
        return 0


def _sleep(ms: int) -> None:
    time.sleep(ms / 1000.0)


# Any status at or above 400 on a request to the site's own hosts, and the
# SAME threshold in all three engines. This engine also keeps the MAIN
# DOCUMENT's status, which is the only unambiguous signal that an unknown
# collection slug is `empty` rather than a `shell` that has not painted —
# without it this engine would record a different state than its twins on the
# identical URL (§6).
_SITE_HOST_FRAGMENTS = ("opensea.io",)


def _watch_refusals(session) -> None:
    """Start counting refused responses, and keep the document's status."""
    session._refused = 0
    session._document_status = None

    def _on_response(response):
        try:
            url = getattr(response, "url", "")
            status = int(getattr(response, "status", 0))
            if not any(fragment in url for fragment in _SITE_HOST_FRAGMENTS):
                return
            request = getattr(response, "request", None)
            if getattr(request, "resourceType", None) == "document":
                session._document_status = status
            if status >= 400:
                session._refused += 1
        except Exception:  # noqa: BLE001 — a listener must never break a run
            pass

    session._response_hook = _on_response
    try:
        session.page.on("response", _on_response)
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not attach the response watcher: %s", e)


def _refused_count(session) -> int:
    return getattr(session, "_refused", 0)


def _document_status(session) -> Optional[int]:
    return getattr(session, "_document_status", None)


def _content(session) -> Optional[str]:
    try:
        return session.bridge.run(session.page.content())
    except Exception as e:  # noqa: BLE001
        logger.debug("content() unavailable (page navigating?): %s", e)
        return None


def _current_url(session) -> str:
    try:
        return session.bridge.run(session.page.evaluate("() => location.href"))
    except Exception:  # noqa: BLE001
        return ""


def _scroll_to_bottom(session) -> None:
    """Scroll the WINDOW to the end of the document. See page_flow."""
    try:
        session.bridge.run(session.page.evaluate(
            "() => window.scrollTo(0, document.body.scrollHeight)"))
    except Exception as e:  # noqa: BLE001
        logger.debug("scroll failed: %s", e)


def _page_height(session) -> Optional[int]:
    try:
        return session.bridge.run(session.page.evaluate(
            "() => document.body.scrollHeight"))
    except Exception:  # noqa: BLE001
        return None


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    return page_flow.min_matches(args.mode)


def _classify(session, html: str, status=None, mode: str = "items") -> str:
    # `status` POSITIONAL and second, matching the other two engines and the
    # callee's real signature (§17).
    return page_flow.classify(html, status, _current_url(session), mode)


_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one."""
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


# The cursor pages. pyppeteer takes an async arrow function like Playwright
# does; the request, the endpoint and the body are identical across all three
# engines, and the body is built by `product_parser.graphql_body` so the
# requests are byte-identical (§6).
#
# Issued from INSIDE the page: same-origin cookies (including Cloudflare's
# `__cf_bm`), the connection the page already has, and the same exit as the
# navigation with no second proxy configuration to keep in step.
GRAPHQL_FETCH_JS = """
async ({endpoint, body}) => {
  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify(body),
      credentials: 'include',
    });
    return {status: response.status, text: await response.text()};
  } catch (e) {
    return {status: 0, text: '', error: String(e)};
  }
}
"""


def _graphql_page(session, args, cursor: Optional[str], page_num: int):
    """Fetch one cursor page through the open document. Returns a PageOutcome.

    Never raises for an EXPECTED failure. This endpoint answers HTTP 200 with
    an `errors` array rather than a 4xx, so a caller that only checks the
    status code reads a failure as an empty page (§8, fail loudly).
    """
    outcome = PageOutcome(page_num=page_num, url=_current_url(session))
    body = graphql_body(
        args.mode, slug=args.collection_slug, limit=args.limit, after=cursor,
        sort=args.sort, ranking=args.ranking, timeframe=args.timeframe,
        activity_filter=args.activity)

    for attempt in range(1, args.retries + 1):
        try:
            result = session.bridge.run(session.page.evaluate(
                GRAPHQL_FETCH_JS,
                {"endpoint": GRAPHQL_ENDPOINT, "body": body}))
        except Exception as e:  # noqa: BLE001
            result = {"status": 0, "text": "", "error": str(e)}
        status = (result or {}).get("status") or 0
        text = (result or {}).get("text") or ""

        if status == 200 and text:
            try:
                payload = json.loads(text)
            except ValueError:
                logger.error("Page %d: the endpoint answered 200 with "
                             "something that is not JSON (%d bytes).",
                             page_num, len(text))
                outcome.load_failed = True
                return outcome
            errors = graphql_errors(payload)
            if errors:
                logger.error("Page %d: the endpoint answered with errors: %s",
                             page_num, "; ".join(errors[:3]))
                outcome.load_failed = True
                outcome.state = "graphql_error"
                return outcome

            if args.dump_html:
                dump_path = f"{args.dump_html}.page{page_num}.json"
                with open(dump_path, "w", encoding="utf-8") as f:
                    f.write(text)
                logger.info("Saved the response the parser sees to %s "
                            "(%d bytes).", dump_path, len(text))

            rows = rows_from_graphql(
                payload, _current_url(session), mode=args.mode, page=page_num,
                timeframe=TIMEFRAMES.get(args.timeframe, "ONE_DAY"),
                first_rank=args.rank_offset if args.mode == "collections" else None)
            data = payload.get("data") or {}
            feed = next((v for v in data.values() if isinstance(v, dict)), {})
            outcome.products = rows
            outcome.next_cursor = feed.get("nextPageCursor")
            outcome.state = "content"
            outcome.final_url = _current_url(session)
            return outcome

        if status in (403, 429, 503):
            logger.error("Page %d: the endpoint answered HTTP %d.",
                         page_num, status)
            outcome.blocked_by = "endpoint-http-%d" % status
            outcome.state = "blocked"
            return outcome

        detail = (result or {}).get("error") or f"HTTP {status}"
        if attempt < args.retries:
            pause = args.retry_delay * (2 ** (attempt - 1))
            logger.warning("Page %d: the endpoint did not answer (%s) — "
                           "retrying in %.1fs (%d/%d).",
                           page_num, detail[:160], pause, attempt, args.retries)
            time.sleep(pause)
        else:
            logger.error("Page %d: gave up after %d attempt(s) (%s).",
                         page_num, args.retries, detail[:160])
            outcome.load_failed = True
    return outcome


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Identical policy to the other two engines — see
    `playwright_scraper.handle_captcha_if_present` for what is and is not
    known about challenges on this site.
    """
    html = _content(session)
    if html is None:
        return False

    already_rendered = _count(session, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"
    url = _current_url(session)

    def _evaluate(js):
        return session.bridge.run(session.page.evaluate(js))

    html_challenge = detect_recaptcha_v3(html, url)
    runtime_challenge = detect_recaptcha_in_page(_evaluate, page_url=url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        challenge = (wait_for_turnstile(_evaluate, time.sleep, page_url=url)
                     or detect_turnstile(html, url))
        if challenge and not challenge.sitekey:
            logger.warning(
                "A Cloudflare Turnstile is on this page but no sitekey was "
                "captured, so it cannot be solved and nothing will be charged "
                "for it.")
            return False
    if not challenge:
        return False

    if when_blocked and already_rendered > page_flow.MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d cards are already on the page "
                    "— not solving it. Pass --solve-captcha always to solve "
                    "it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting "
                   "to solve.", challenge.kind, challenge.source,
                   challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    try:
        if challenge.is_turnstile:
            called_back = session.bridge.run(
                session.page.evaluate(TURNSTILE_INJECT_JS, token))
            logger.info("Turnstile token injected%s.",
                        " and handed to the page's callback" if called_back
                        else " (no callback was captured — relying on the "
                             "form field)")
        else:
            session.bridge.run(session.page.evaluate(INJECT_TOKEN_JS, token))
    except Exception as e:  # noqa: BLE001
        logger.error("Could not inject the solved token (%s) — continuing "
                     "with whatever the page holds.", e)
        return False
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    try:
        session.bridge.run(session.page.reload(
            {"waitUntil": "domcontentloaded", "timeout": 60000}))
    except Exception as e:  # noqa: BLE001
        logger.warning("Reload after the solve failed: %s", e)
    return True


def _scroll_the_feed(session, args, page_num: int) -> dict:
    """Scroll to the bottom until the grid stops growing.

    RUN ONLY WHEN THE INLINED STATE DID NOT PARSE — see
    `playwright_scraper._scroll_the_feed` for why that condition is the whole
    design.
    """
    trace = page_flow.scroll_until_settled(
        lambda sel: _count(session, sel),
        lambda: _scroll_to_bottom(session),
        lambda: _page_height(session),
        _sleep,
        selector=_ready_selector(args))
    logger.info("Scrolled page %d: %d card(s) at first paint, %d after "
                "%d round(s).", page_num, trace["cards_before"],
                trace["cards_after"], trace["rounds"])
    return {"first_paint": trace["cards_before"],
            "reached": trace["cards_after"], "rounds": trace["rounds"]}


def _fetch_first_page(session, args, pool) -> PageOutcome:
    """Navigate to the run's URL and read the state the site inlined into it.

    Mirrors `playwright_scraper._fetch_first_page` decision for decision;
    only the driver calls differ.
    """
    url = args.url
    outcome = PageOutcome(page_num=1, url=url)
    has_pool = bool(pool and len(pool) > 1)
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    solves_bought = 0
    html, state, load_failed = None, "ok", False
    status = None

    for block_attempt in range(block_retries + 1):
        load_failed, exit_failed = False, None
        logger.info("Fetching page 1: %s", url)
        for attempt in range(1, args.retries + 1):
            try:
                response = session.bridge.run(session.page.goto(
                    url, {"waitUntil": "domcontentloaded", "timeout": 60000}))
                status = getattr(response, "status", None) if response else None
                load_failed = False
                break
            except Exception as e:  # noqa: BLE001
                reason = _proxy_failure(e)
                if reason:
                    exit_failed, load_failed = reason, True
                    break  # a different exit is the only thing that helps
                load_failed = True
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Timeout loading %s (attempt %d/%d) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, pause)
                    time.sleep(pause)

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if load_failed:
            break

        if handle_captcha_if_present(session, args):
            time.sleep(1.0)

        html = _content(session) or ""
        status = status if status is not None else _document_status(session)
        state = _classify(session, html, status, args.mode)

        if page_flow.is_unpainted(state, html):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page 1 is a shell the site served but has not filled "
                        "in (%d bytes, no cards) — waiting up to %.0fs for "
                        "the grid rather than spending a retry.",
                        len(html), wait_timeout / 1000)
            found = page_flow.wait_for_count(
                lambda sel: _count(session, sel), _sleep,
                _ready_selector(args), _min_matches(args, html), wait_timeout)
            if found < _min_matches(args, html):
                logger.info("The grid still had not painted after %.0fs "
                            "(%d match(es)).", wait_timeout / 1000, found)
            html = _content(session) or html
            state = _classify(session, html, status, args.mode)

        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session, args):
                time.sleep(1.0)
                html = _content(session) or html
                state = _classify(session, html, status, args.mode)
                if state == "content":
                    logger.info("The solve was accepted — page 1 is content "
                                "now.")
                else:
                    logger.warning("The solve was NOT accepted: page 1 is "
                                   "still %s. The purchase is spent.", state)

        if not page_flow.should_retry(state):
            break

        if block_attempt < block_retries:
            pause = args.retry_delay * (block_attempt + 1)
            if has_pool:
                logger.warning("Page 1 came back as %s from %s — retrying "
                               "from another exit in %.1fs (%d/%d).",
                               state, mask(pool.current), pause,
                               block_attempt + 1, block_retries)
                pool.advance(f"{state} on page 1")
                session.relaunch()
                time.sleep(pause)
            else:
                fresh = (page_flow.RETRY_NEEDS_FRESH_CONTEXT
                         and not args.cdp_endpoint)
                logger.warning("Page 1 came back as %s — waiting %.1fs and "
                               "re-fetching %s (%d/%d).", state, pause,
                               "in a FRESH browser, which is what clears a "
                               "challenge elsewhere in this family"
                               if fresh else "through the same access path",
                               block_attempt + 1, block_retries)
                time.sleep(pause)
                if fresh:
                    session.relaunch()

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if page_flow.counts_as_blocked(state):
        debug_html = f"{args.out}_page1_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            session.bridge.run(session.page.screenshot(
                {"path": f"{args.out}_page1_debug.png"}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        served = served_by_opensea(html or "")
        vendor = detect_bot_challenge(html or "")
        logger.error(
            "The site did not serve this request — %d bytes, %s the site's "
            "own asset hosts, saved to %s. This is exit 3, distinct from a "
            "genuinely empty result (exit 4).%s",
            len(html or ""),
            "which references" if served else "with no reference to",
            debug_html,
            (f" Tried {block_retries + 1} exit(s)." if has_pool
             else f" Re-fetched {block_retries + 1} time(s)."))
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        outcome.blocked_by = vendor or ("no-response" if not html else "bot-or-not")
        outcome.final_url = _current_url(session)
        return outcome

    if page_flow.should_parse(state) and page_flow.state_answered(html, args.mode):
        logger.info("Page 1 shipped its rows in the page's own state — "
                    "parsing it directly (no readiness wait, no scroll).")
    elif page_flow.should_parse(state):
        selector, threshold = _ready_selector(args), _min_matches(args, html)
        content_timeout = page_flow.content_timeout_ms(args.mode)
        found = page_flow.wait_for_count(
            lambda sel: _count(session, sel), _sleep, selector, threshold,
            content_timeout)
        _sleep(500)
        if found < threshold:
            logger.info("No tiles appeared within %.0fs. If this feed "
                        "genuinely holds nothing, that is the expected answer "
                        "and the run will report 0 rows (exit 4).",
                        content_timeout / 1000)
        outcome.scroll = _scroll_the_feed(session, args, 1)
        html = _content(session) or html

    if args.dump_html:
        dump_path = args.dump_html if args.pages == 1 else f"{args.dump_html}.page1"
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html or ""))

    rows = parse_rows(html or "", _current_url(session), page=1, mode=args.mode,
                      timeframe=TIMEFRAMES.get(args.timeframe, "ONE_DAY"),
                      first_rank=1 if args.mode == "collections" else None)
    logger.info("Parsed %d row(s) from page 1.", len(rows))

    outcome.totals = page_flow.collection_totals_on_page(html or "") or None
    if args.ssr_usable:
        outcome.products = rows
        outcome.next_cursor = next_cursor(html or "", args.mode)
    else:
        # The rows page 1 rendered answer a DIFFERENT question than this run
        # is asking, and its cursor belongs to that other ordering (§21).
        outcome.ssr_dropped = len(rows)
        logger.info("%s", ssr_mismatch_note(
            args.mode, sort=args.sort, ranking=args.ranking,
            timeframe=args.timeframe, activity_filter=args.activity))

    outcome.gap = page_flow.page_gap(html or "", len(rows))
    outcome.final_url = _current_url(session)

    if not rows:
        debug_html = f"{args.out}_page1_debug.html"
        debug_png = f"{args.out}_page1_debug.png"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            session.bridge.run(session.page.screenshot({"path": debug_png}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        if page_flow.parsed_nothing_from_a_served_page(html, len(rows)):
            outcome.state = "parser_found_nothing"
            logger.error(
                "0 rows parsed from a page the site SERVED, which links to "
                "%d item(s). That is a parser regression rather than an empty "
                "collection. Saved what the browser saw to %s and %s — please "
                "open an issue with the .html attached.",
                page_flow.count_cards(html or ""), debug_html, debug_png)
        else:
            logger.warning("0 rows parsed — saved what the browser actually "
                           "saw to %s and %s. Open the .png to see it.",
                           debug_html, debug_png)
    return outcome


def _report_coverage(rows: List, page_num: int, mode: str) -> None:
    """Log what share of this page carries the columns the mode is for."""
    if not rows:
        return
    with_key = sum(1 for r in rows if r.sku)
    with_title = sum(1 for r in rows if r.title)
    worst = min(with_key, with_title)
    share = 100.0 * worst / len(rows)
    logger.info("Key/title coverage on page %d: %d and %d of %d (%.0f%% at "
                "worst); the measured floor is %d%%.",
                page_num, with_key, with_title, len(rows), share, FIELD_FLOOR)
    if share < FIELD_FLOOR:
        logger.warning(
            "Only %.0f%% of page %d carries both an id and a name, against a "
            "measured floor of %d%%. Re-run with --dump-html; the data_source "
            "column will say whether the rows came from the inlined state or "
            "from the anchors alone.", share, page_num, FIELD_FLOOR)
    if mode == "items":
        listed = sum(1 for r in rows if r.price is not None)
        logger.info("Listed items on page %d: %d of %d carry a price. An "
                    "unlisted item has none, and most of a collection is "
                    "unlisted — that is the site, not the read.",
                    page_num, listed, len(rows))


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    blocked = False
    stop_reason = "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        logger.warning("--concurrency %d is refused: %s.", concurrency,
                       page_flow.concurrency_refusal(args.url, args.mode))
        concurrency = 1

    bridge = _AsyncBridge()
    session = None
    try:
        session = _Session(bridge, args, pool).open()
        first = _fetch_first_page(session, args, pool)
        outcomes.append(first)
        _report_coverage(first.products, 1, args.mode)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None
        elif first.state == "parser_found_nothing":
            stop_reason = "parser_found_nothing"
        else:
            cursor = first.next_cursor
            rank_seen = len(first.products)
            last_page = args.pages + (0 if args.ssr_usable else 1)
            # A null cursor means the START of the feed where page 1's rows
            # were dropped, not the end of it. See the primary engine.
            feed_started = args.ssr_usable
            for page_num in range(2, last_page + 1):
                if page_flow.page_cap_reached(page_num):
                    logger.warning("Stopping at the %d-page cap.", PAGE_CAP)
                    stop_reason = "page_cap"
                    break
                if feed_started and page_flow.price_boundary_reached(cursor):
                    stop_reason = "listed_items_exhausted"
                    logger.info("%s", page_flow.sort_note(
                        args.sort, rank_seen, first.totals))
                    break
                if feed_started and page_flow.cursor_exhausted(cursor):
                    stop_reason = "cursor_exhausted"
                    logger.info("The site handed back no cursor after page "
                                "%d — that is the end of this feed, and the "
                                "run is COMPLETE holding everything it has.",
                                page_num - 1)
                    break

                if pool and pool.rotates_per_page():
                    logger.info("--proxy-rotate per-page: rotating and "
                                "re-opening %s, because the cursor request is "
                                "issued from inside the open document.",
                                args.url)
                    pool.advance(f"per-page rotation, page {page_num}")
                    session.relaunch()
                    session.bridge.run(session.page.goto(
                        args.url,
                        {"waitUntil": "domcontentloaded", "timeout": 60000}))

                time.sleep(args.delay)
                args.rank_offset = rank_seen + 1
                outcome = _graphql_page(session, args, cursor, page_num)
                feed_started = True
                outcomes.append(outcome)
                _report_coverage(outcome.products, page_num, args.mode)

                if not outcome.ok:
                    stop_reason = ("endpoint_error" if outcome.load_failed
                                   else f"blocked_{outcome.blocked_by}")
                    blocked = outcome.blocked_by is not None
                    break
                if not outcome.products:
                    stop_reason = "no_new_products"
                    logger.info("Page %d came back with no rows — treating "
                                "that as the end of the feed.", page_num)
                    break
                rank_seen += len(outcome.products)
                cursor = outcome.next_cursor
            else:
                if args.pages > 1 and cursor:
                    logger.info("Fetched the %d page(s) asked for and the "
                                "feed goes on — pass a higher --pages to keep "
                                "walking.", args.pages)
    finally:
        if session is not None:
            session.close()
        bridge.close()

    all_rows, fresh_by_page, _ = merge_pages(
        [(o.page_num, o.products) for o in outcomes], key="sku")

    totals = next((o.totals for o in outcomes if o.totals), None)
    if all_rows:
        note = page_flow.totals_note(totals, len(all_rows))
        if note:
            logger.info("%s", note)
        from collections import Counter
        views = Counter(r.data_source for r in all_rows)
        logger.info("Rows by route: %s.",
                    ", ".join(f"{k}={v}" for k, v in sorted(views.items())))

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    extra = {
        "rows_new_per_page": fresh_by_page,
        "scroll": {o.page_num: o.scroll for o in outcomes if o.scroll},
        "sort": args.sort if args.mode == "items" else None,
        "timeframe": args.timeframe if args.mode == "collections" else None,
        "ranking": args.ranking if args.mode == "collections" else None,
        "activity_filter": args.activity if args.mode == "activity" else None,
        "collection": args.collection_slug or None,
        "locale": locale_of(final_url),
        "collection_totals": totals,
        "rows_from_state": sum(1 for r in all_rows if r.data_source == "ssr"),
        "rows_from_endpoint": sum(1 for r in all_rows if r.data_source == "graphql"),
        "rows_from_dom": sum(1 for r in all_rows if r.data_source == "dom"),
        "rows_dropped_from_page_1": sum(o.ssr_dropped for o in outcomes),
        "pagination": "cursor",
    }

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=source_of(final_url),
                      start_url=args.url, final_url=final_url,
                      extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="OpenSea NFT marketplace scraper (pyppeteer edition)")
    p.add_argument("--url", default=None,
                   help="An OpenSea URL: a collection (/collection/{slug}), "
                        "its activity feed (/collection/{slug}/activity), the "
                        "ranking (/collections), or one item "
                        "(/item/{chain}/…). A locale path works too. Required "
                        "unless --collection or --mode collections is given, "
                        "or OPENSEA_URL is set in the environment or in .env.")
    p.add_argument("--collection", default=None, metavar="SLUG",
                   help="A collection slug to read instead of --url, e.g. "
                        "`boredapeyachtclub`.")
    p.add_argument("--mode", choices=["items", "collections", "activity"],
                   default=None,
                   help="Which view to take. Inferred from the URL by "
                        "default. The three yield DIFFERENT row classes and "
                        "diff_runs.py refuses to compare two of them.")
    p.add_argument("--sort", choices=sorted(SORTS), default=DEFAULT_SORT,
                   help=f"How to order a collection's items (default "
                        f"{DEFAULT_SORT}, which is what the collection page "
                        f"itself shows). THIS DECIDES WHICH ROWS YOU GET: "
                        f"under `price` the walk stops where the LISTED items "
                        f"end. Use `created` to walk the whole collection.")
    p.add_argument("--timeframe", choices=sorted(TIMEFRAMES),
                   default=DEFAULT_TIMEFRAME,
                   help=f"Which window --mode collections ranks and reports "
                        f"volume over (default {DEFAULT_TIMEFRAME}).")
    p.add_argument("--ranking", choices=list(RANKING_SLUGS), default="TRENDING",
                   help="Which ranking --mode collections reads (default "
                        "TRENDING). TOP is by volume over the timeframe.")
    p.add_argument("--activity", choices=sorted(ACTIVITY_FILTERS),
                   default=DEFAULT_ACTIVITY_FILTER,
                   help=f"Which events --mode activity keeps (default "
                        f"{DEFAULT_ACTIVITY_FILTER}).")
    p.add_argument("--pages", type=int, default=1,
                   help=f"Pages to fetch (default 1, cap {PAGE_CAP}).")
    p.add_argument("--limit", type=int, default=MAX_LIMIT, metavar="N",
                   help=f"Rows per cursor page (default and maximum "
                        f"{MAX_LIMIT} — the endpoint states that limit "
                        f"itself).")
    p.add_argument("--category", default=None,
                   help="Accepted for the family's shape and mapped onto "
                        "--collection.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Delay between pages, seconds (default 1.0).")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for the family's shape and REFUSED above 1, "
                        "with the reason: page 5's request does not exist "
                        "until page 4 has been read.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3).")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter "
                        "(default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="opensea_items",
                   help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). It does NOT decide "
                        "the site language: the URL PATH does.")
    p.add_argument("--browser-channel", default=DEFAULT_BROWSER_CHANNEL,
                   metavar="CHANNEL",
                   help="Accepted for parity with the other engines. Use "
                        "--chromium-path to point pyppeteer at an installed "
                        "browser instead of its own download.")
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Drive the browser at this path instead of "
                        "pyppeteer's own download.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL. Credentials are sent through "
                        "page.authenticate(), never on the command line.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES),
                   default="per-run",
                   help="per-run (default): one exit for the whole run. "
                        "per-page rotates between cursor pages, which costs a "
                        "re-navigation.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back refused, retry it from this "
                        "many OTHER exits before giving up (default 2).")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's "
                        "Fingerprint API and apply it. Needs "
                        "--twocaptcha-key.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "challenge if the content is not already readable. No "
                        "challenge was met on opensea.io while this was "
                        "written, so the path is unexercised HERE — a fact "
                        "about this repo's testing and not a claim about what "
                        "a solver can do (§19).")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser API "
                        "endpoint. Authentication on the WebSocket upgrade "
                        "works from this engine.")
    p.add_argument("--cdp-connect-timeout", type=float,
                   default=CDP_CONNECT_TIMEOUT_MS / 1000, metavar="SECONDS",
                   help=f"How long to wait for --cdp-endpoint to accept the "
                        f"connection (default "
                        f"{CDP_CONNECT_TIMEOUT_MS // 1000}).")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save exactly what the parser is given. Page 1 as "
                        "HTML; every cursor page as `<path>.pageN.json`.")
    p.add_argument("--headless", dest="headless", action="store_true",
                   default=True,
                   help="Run headless. THE DEFAULT, and measured identical to "
                        "headful on this site.")
    p.add_argument("--headful", dest="headless", action="store_false",
                   help="Run with a real browser window. Needs a display.")
    args = p.parse_args()
    env_config.apply(args)

    if args.category and not args.collection:
        args.collection = args.category
    if not args.url:
        if args.collection:
            mode = args.mode or "items"
            if mode == "collections":
                p.error("--collection names one collection and --mode "
                        "collections reads the ranking of all of them. Drop "
                        "one of the two.")
            args.url = url_for_mode(mode, args.collection)
            logger.info("Reading %s", args.url)
        elif args.mode == "collections":
            args.url = url_for_mode("collections")
            logger.info("Reading OpenSea's own ranking: %s", args.url)
    if not args.url:
        p.error("no --url given, no --collection given, and OPENSEA_URL is "
                "not set in the environment or in .env.")

    why = unsupported_reason(args.url)
    if why:
        p.error(why)

    normalized = normalize_url(args.url)
    if normalized != args.url:
        logger.info("Fetching %s instead of %s — the site's own click "
                    "parameters are stripped so one row has one address.",
                    normalized, args.url)
        args.url = normalized

    kind = page_kind(args.url)
    inferred = mode_for_url(args.url)
    if args.mode is None:
        args.mode = inferred or "items"
        logger.info("Reading %s as a %s run.", args.url, args.mode)
    elif inferred and args.mode != inferred:
        p.error(f"--mode {args.mode} does not match {args.url!r}, which is a "
                f"{kind} page and reads as --mode {inferred}.")

    args.collection_slug = collection_slug_from_url(args.url) or (args.collection or "")
    if args.mode in ("items", "activity") and not args.collection_slug and kind != "item":
        p.error(f"--mode {args.mode} needs a collection; {args.url!r} names "
                f"none.")
    args.ssr_usable = ssr_matches_request(
        args.mode, sort=args.sort, ranking=args.ranking,
        timeframe=args.timeframe, activity_filter=args.activity)
    args.rank_offset = 1

    if args.pages > PAGE_CAP:
        logger.warning("--pages %d is above this scraper's %d-page cap; it "
                       "will stop there.", args.pages, PAGE_CAP)
    if args.limit > MAX_LIMIT:
        logger.warning("--limit %d is above the endpoint's own maximum of "
                       "%d; it will ask for %d.", args.limit, MAX_LIMIT,
                       MAX_LIMIT)
    if kind == "item" and args.pages > 1:
        logger.info("An item page is ONE row and has no feed under it, so "
                    "--pages %d will fetch one page.", args.pages)
        args.pages = 1
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key.")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "remote browser supplies its own.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except TimeoutError as e:
        # A remote browser that never answered is a REMOTE API failure (exit
        # 5), not a crash in this code (exit 1) and not bad usage (exit 2).
        logger.error("%s", _mask_credentials(str(e)))
        sys.exit(EXIT_API_ERROR)
