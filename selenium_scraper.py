#!/usr/bin/env python3
"""opensea-scraper — Selenium edition

The same three modes, the same rows and the same exit codes as
`playwright_scraper.py`; only the driver differs. Playwright is the primary
engine here — this one exists so a reader already standing in a Selenium
stack does not have to leave it, and so the family has a second
implementation that has to agree with the first.

    --mode items        (default)  /collection/{slug} — one row per NFT
    --mode collections             /collections — OpenSea's own ranking
    --mode activity                /collection/{slug}/activity — one row per
                                   sale, listing, offer, transfer or mint

TWO LIMITS THAT ARE SELENIUM'S AND NOT THIS SITE'S
--------------------------------------------------
* **An authenticated remote CDP endpoint cannot be used from here at all.**
  Playwright's `connect_over_cdp` and Puppeteer's `browserWSEndpoint` take a
  full `ws://user:pass@host:port` and authenticate on the WebSocket upgrade;
  chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
  put a password. The 2Captcha Scraping Browser API endpoint is
  authenticated, so `--cdp-endpoint` against it is refused with that reason
  rather than failing later as a connection error.

* **`--proxy` cannot carry credentials.** `--proxy-server` accepts an address
  only and there is no Selenium equivalent of pyppeteer's
  `page.authenticate`. They are stripped WITH A WARNING, because letting a
  reader believe a `user:pass` URL is doing something is worse than the
  missing feature.

Everything else is identical by construction: the page triage lives in
`page_flow.py`, the row building in `product_parser.py`, and the
status/exit-code mapping in `output_writer.finish_run`, so this engine
cannot quietly disagree with its twins about whether a run was complete.

Example
-------
    python3 selenium_scraper.py \\
        --url "https://opensea.io/collection/boredapeyachtclub" --pages 3
"""

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

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
from output_writer import merge_pages, finish_run
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, split_credentials,
                        mask, ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

DEFAULT_BROWSER_CHANNEL = None
CDP_CONNECT_TIMEOUT_MS = 150_000
PAGE_LOAD_TIMEOUT = 60
# Generous: the script timeout bounds the in-page GraphQL fetch, and a
# hundred rows of a busy collection is a real request rather than a local
# evaluation.
SCRIPT_TIMEOUT = 45

# See playwright_scraper.FIELD_FLOOR — the same number, measured on the same
# captures, and it must stay the same in all three engines.
FIELD_FLOOR = 95

_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced.

    Global, not first-match: an error can repeat an endpoint several times,
    and a masker that handles one occurrence prints the password for the rest
    while looking like it works (§8).
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version."""
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` out of a CDP endpoint, refusing one with credentials.

    See the module docstring. Silently stripping the credentials would
    produce a connection refusal a long way from its cause.
    """
    parts = urlsplit(endpoint)
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials, and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "The 2Captcha Scraping Browser API endpoint is authenticated, so "
            "it cannot be used from this engine — run playwright_scraper.py "
            "or puppeteer_scraper.py for it. Endpoint: %s",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


@dataclass
class PageOutcome:
    """What one page produced. Same shape as the other two engines'."""
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


class _Session:
    """One Chrome driver, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser: cookies a bot
    manager issued against one exit, replayed from another, are a stronger
    signal than either address alone (§8).
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None

    def open(self):
        options = Options()
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own, and stacking a second creates a contradiction
            # rather than better cover (§8).
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            self._install_turnstile_intercept()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # 1440x900 matches what the captures were taken at.
        options.add_argument("--window-size=1440,900")
        # Not a fingerprint measure, a correctness one: without it Chrome
        # advertises "HeadlessChrome", which is a giveaway on any site with a
        # bot manager in front of it.
        options.add_argument("--disable-blink-features=AutomationControlled")

        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only, and there "
                    "is no Selenium equivalent of pyppeteer's "
                    "page.authenticate. They have been stripped, so requests "
                    "will go out unauthenticated and the exit will most "
                    "likely refuse them. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        # Chrome's performance log, which is how this engine learns a
        # response's STATUS and counts refused responses — the other two get
        # both from a response listener, which Selenium has no equivalent of.
        # It has to be asked for at driver creation; there is no way to turn
        # it on later. Without it this engine could not tell a real 404 from
        # a shell, and would report `shell` where its twins report `empty` on
        # the identical URL (§6).
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()
        self._install_turnstile_intercept()
        _watch_refusals(self)

        version = self.driver.capabilities.get("browserVersion", "")
        if version:
            try:
                self.driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": _chrome_ua(version)})
            except WebDriverException as e:
                logger.debug("Could not override the user agent: %s", e)

        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        # THROUGH THE SHARED HELPER, never by reaching into the response
        # shape here: the UA lives at `userAgent.userAgent` in one format and
        # at `data.ua` in the other, and a key that exists in neither makes
        # `--fingerprint` silently set no user agent at all — which defeats
        # the flag rather than breaking it (§16).
        from fingerprint_client import (get_fingerprint, fingerprint_user_agent,
                                        playwright_init_script)
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = fingerprint_user_agent(fp)
        script = playwright_init_script(fp)
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                            {"userAgent": ua})
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": script})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) — "
                           "continuing without it.", e)

    def _install_turnstile_intercept(self):
        """Hook `turnstile.render` before any page script can run.

        THE ONLY MOMENT a Cloudflare Turnstile's parameters can be captured:
        a Challenge page calls `turnstile.render(container, params)` once and
        keeps nothing, while `TurnstileTaskProxyless` needs the sitekey,
        action, cData and chlPageData that live only inside that call.
        Selenium spells it `Page.addScriptToEvaluateOnNewDocument` over CDP;
        the other two engines spell the same thing `add_init_script` and
        `evaluateOnNewDocument`, which is why this cannot live in the shared
        module (§1).
        """
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": TURNSTILE_INTERCEPT_JS})
        except WebDriverException as e:
            logger.debug("Could not install the Turnstile interception: %s", e)

    def relaunch(self):
        if self.remote:
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() ends one window and leaves the
                # driver process running, which on a per-page rotation would
                # leak a chromedriver per page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during driver teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to Selenium
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. Note the JS dialect: Selenium's
# execute_script runs a function BODY and needs an explicit `return`, unlike
# the `() => expr` both other engines take — which is exactly why page_flow
# names operations instead of passing JavaScript across the boundary (§1).
def _count(session, selector: str) -> int:
    try:
        return len(session.driver.find_elements(By.CSS_SELECTOR, selector))
    except WebDriverException as e:
        logger.debug("count(%s) failed: %s", selector, e)
        return 0


def _sleep(ms: int) -> None:
    time.sleep(ms / 1000.0)


def _content(session) -> Optional[str]:
    try:
        return session.driver.page_source
    except WebDriverException as e:
        logger.debug("page_source unavailable (page navigating?): %s", e)
        return None


def _scroll_to_bottom(session) -> None:
    """Scroll the WINDOW to the end of the document. See page_flow."""
    try:
        session.driver.execute_script(
            "window.scrollTo(0, document.body.scrollHeight); return null;")
    except WebDriverException as e:
        logger.debug("scroll failed: %s", e)


def _page_height(session) -> Optional[int]:
    try:
        return session.driver.execute_script(
            "return document.body.scrollHeight;")
    except WebDriverException:
        return None


# Reading Chrome's performance log. This engine needs it for TWO things its
# twins get from a response listener: how many of the site's responses came
# back >= 400, and what status the MAIN DOCUMENT was served with.
#
# The second is not cosmetic. An unknown collection slug answers a real HTTP
# 404, and that status is the only unambiguous signal that a page is `empty`
# rather than a `shell` that has not painted — without it this engine would
# spend a 25-second readiness wait on every missing collection and record a
# different state than its twins on the identical URL (§6).
#
# The log DRAINS ON READ, so both facts are collected in one pass and cached
# on the session; two independent readers would each see half the entries.
_SITE_HOST_FRAGMENTS = ("opensea.io",)


def _drain_performance_log(session) -> None:
    total = getattr(session, "_refused", 0)
    try:
        entries = session.driver.get_log("performance")
    except Exception:  # noqa: BLE001 — an absent log must never break a run
        return
    for entry in entries:
        try:
            message = json.loads(entry.get("message", "{}"))["message"]
            if message.get("method") != "Network.responseReceived":
                continue
            params = message["params"]
            response = params["response"]
            url = response.get("url", "")
            status = int(response.get("status", 0))
            if params.get("type") == "Document" and any(
                    fragment in url for fragment in _SITE_HOST_FRAGMENTS):
                session._document_status = status
                session._document_url = url
            if status >= 400 and any(fragment in url
                                     for fragment in _SITE_HOST_FRAGMENTS):
                total += 1
        except Exception:  # noqa: BLE001
            continue
    session._refused = total


def _watch_refusals(session) -> None:
    """Reset the running counters. The log itself is enabled on the driver."""
    session._refused = 0
    session._document_status = None
    session._document_url = None
    _drain_performance_log(session)


def _refused_count(session) -> int:
    _drain_performance_log(session)
    return getattr(session, "_refused", 0)


def _document_status(session) -> Optional[int]:
    """The status the current document was served with, where it is known."""
    _drain_performance_log(session)
    return getattr(session, "_document_status", None)


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    return page_flow.min_matches(args.mode)


def _current_url(session) -> str:
    try:
        return session.driver.current_url
    except WebDriverException:
        return ""


def _classify(session, html: str, status=None, mode: str = "items") -> str:
    # `status` is POSITIONAL and second. Two engines in a sibling repo passed
    # it as a keyword and both crashed on their first fetch (§17); this
    # repo's smoke suite binds every shared-module call in every engine
    # against the callee's real signature for that reason.
    return page_flow.classify(html, status, _current_url(session), mode)


# Chromium's own names for "the proxy is the problem, not the site".
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


# The cursor pages, in Selenium's dialect: a function BODY whose LAST
# argument is the completion callback. The other two engines pass an async
# arrow function; the request, the endpoint and the body are identical, and
# the body itself is built by `product_parser.graphql_body` so all three send
# byte-identical requests (§6).
#
# Issued from INSIDE the page for the same three reasons as in the primary
# engine: same-origin cookies (including Cloudflare's `__cf_bm`), the
# connection the page already has, and the same exit as the navigation with
# no second proxy configuration to keep in step.
GRAPHQL_FETCH_JS = """
var endpoint = arguments[0], body = arguments[1], done = arguments[2];
fetch(endpoint, {
  method: 'POST',
  headers: {'content-type': 'application/json'},
  body: JSON.stringify(body),
  credentials: 'include'
}).then(function (response) {
  return response.text().then(function (text) {
    done({status: response.status, text: text});
  });
}).catch(function (e) {
  done({status: 0, text: '', error: String(e)});
});
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
            result = session.driver.execute_async_script(
                GRAPHQL_FETCH_JS, GRAPHQL_ENDPOINT, body)
        except WebDriverException as e:
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
    known about challenges on this site. The only difference is the dialect:
    Selenium's `execute_script` takes a function BODY, so the shared
    `() => …` snippets are wrapped in `return (…)();` here.
    """
    driver = session.driver
    html = _content(session)
    if html is None:
        return False

    already_rendered = _count(session, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, _current_url(session))
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: driver.execute_script(f"return ({js})();"),
        page_url=_current_url(session))
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        challenge = (wait_for_turnstile(
                        lambda js: driver.execute_script(f"return ({js})();"),
                        time.sleep, page_url=_current_url(session))
                     or detect_turnstile(html, _current_url(session)))
        if challenge and not challenge.sitekey:
            logger.warning(
                "A Cloudflare Turnstile is on this page but no sitekey was "
                "captured, so it cannot be solved and nothing will be "
                "charged for it.")
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
            called_back = driver.execute_script(
                f"return ({TURNSTILE_INJECT_JS})(arguments[0]);", token)
            logger.info("Turnstile token injected%s.",
                        " and handed to the page's callback" if called_back
                        else " (no callback was captured — relying on the "
                             "form field)")
        else:
            driver.execute_script(f"return ({INJECT_TOKEN_JS})(arguments[0]);",
                                  token)
    except WebDriverException as e:
        logger.error("Could not inject the solved token (%s) — continuing "
                     "with whatever the page holds.", e)
        return False
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    driver.refresh()
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
                session.driver.get(url)
                load_failed = False
                break
            except WebDriverException as e:
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
        status = _document_status(session)
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
                               "in a FRESH browser session, which is what "
                               "clears a challenge elsewhere in this family"
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
            session.driver.save_screenshot(f"{args.out}_page1_debug.png")
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
            session.driver.save_screenshot(debug_png)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        if page_flow.parsed_nothing_from_a_served_page(html, len(rows)):
            outcome.state = "parser_found_nothing"
            logger.error(
                "0 rows parsed from a page the site SERVED, which links to "
                "%d item(s). That is a parser regression rather than an empty "
                "collection. Saved what the browser saw to %s and %s — "
                "please open an issue with the .html attached.",
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

    session = _Session(args, pool).open()
    try:
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
                    session.driver.get(args.url)

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
        session.close()

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
        description="OpenSea NFT marketplace scraper (Selenium edition)")
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
                   help="Accepted for parity with the other engines. Selenium "
                        "drives whatever Chrome/Chromium chromedriver finds, "
                        "so this is recorded and not acted on.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL. CREDENTIALS CANNOT BE SENT from this "
                        "engine — see the module docstring.")
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
                        "Fingerprint API and apply it over CDP. Needs "
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
                   help="Attach to an already-running browser, host:port. "
                        "AN AUTHENTICATED endpoint cannot be used from this "
                        "engine — see the module docstring.")
    p.add_argument("--cdp-connect-timeout", type=float,
                   default=CDP_CONNECT_TIMEOUT_MS / 1000, metavar="SECONDS",
                   help="Accepted for parity with the other engines; "
                        "chromedriver attaches to `debuggerAddress` without a "
                        "connect timeout of its own.")
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
