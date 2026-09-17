#!/usr/bin/env python3
"""opensea-scraper — Playwright edition (primary engine)

Scrapes OpenSea out of one of three views:

    --mode items        (default)  /collection/{slug} — one row per NFT:
                                   listing price, best offer, last sale,
                                   traits, owner, rarity rank
    --mode collections             /collections — one row per COLLECTION in
                                   OpenSea's own ranking: floor, volume,
                                   owners, supply
    --mode activity                /collection/{slug}/activity — one row per
                                   EVENT: sale, listing, offer, transfer,
                                   mint

The mode is inferred from the URL, so passing it is a way to be explicit or
to be told you are wrong.

HOW A PAGE IS FETCHED HERE, AND WHY IT IS NOT LIKE ITS SIBLINGS
---------------------------------------------------------------
Page 1 is a NAVIGATION. The browser opens the human URL and the run reads
the state OpenSea inlined into it — fifty rows, complete, before a pixel
paints. That is what proves the site served us the page.

Pages 2..N are not navigations, because OpenSea has no address for them.
Every feed here is cursor-paginated: page 1's state carries a
`nextPageCursor`, and the only thing that can spend it is the same GraphQL
endpoint the site's own grid calls. So this engine calls it FROM THE PAGE —
`fetch()` inside the document the browser already has open, same origin,
same cookies, same TLS session — and parses the response with the same
normaliser that read page 1 (49 of 50 rows identical across 18 stable
columns when the two routes were compared, 2026-09-17).

Three things follow, and all three are deliberate:

* **`--concurrency` above 1 is refused, in every mode.** Page 5's request
  does not exist until page 4 has been read. The flag stays, and says so.
* **The readiness wait and the scroll loop are safety nets**, not the main
  event: they run only if the inlined state did not parse and the run has
  dropped to reading anchors.
* **`--dump-html` writes a `.pageN.json` for the cursor pages**, because
  what the parser saw there was a JSON body and not a document.

WHAT IS DIFFERENT ABOUT THIS SITE
---------------------------------
* **Its CSP has no `unsafe-eval`.** `script-src 'self' 'unsafe-inline'
  'wasm-unsafe-eval'`, read from a collection page's own response header on
  2026-09-17. Playwright's `wait_for_function` hands the browser a STRING to
  evaluate and that policy refuses it outright — on a sibling site with the
  same gap it died with `EvalError` and exit 1. Every wait here is a count
  poll over the protocol, and every `evaluate` is given a real function
  object, which goes through `Runtime.callFunctionOn` rather than through
  eval.

* **Nothing refused us — except one User-Agent.** From one Hetzner address
  in Helsinki on 2026-09-17 every page kind and locale answered HTTP 200 to
  a real Chrome UA, to `curl/8.x`, to no UA at all and to a made-up
  `opensea-scraper/0.1`. `Python-urllib/3.13` is refused with 403 on both
  the HTML route and the endpoint, while `python-requests` from the same
  address is served. That is a Cloudflare rule against one signature, and
  naming it is what stops a reader concluding the site blocks scrapers.

* **A price-ordered walk ends before the collection does, on purpose.** The
  cursor encodes the last row's price; at the boundary between listed and
  unlisted items it goes null and the next request errors. This engine
  recognises that cursor and stops COMPLETE, because it holds every item
  that has a price. `--sort created` walks the whole collection instead.

Examples
--------
    python3 playwright_scraper.py \\
        --url "https://opensea.io/collection/boredapeyachtclub" --pages 3

    # the whole collection, oldest first, as a spreadsheet
    python3 playwright_scraper.py --collection boredapeyachtclub \\
        --sort created --pages 20 --format csv

    # OpenSea's own ranking over the last 24 hours
    python3 playwright_scraper.py --mode collections --timeframe 1d --pages 2

    # every sale in a collection's feed
    python3 playwright_scraper.py --collection pudgypenguins \\
        --mode activity --activity sales --pages 5
"""

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS, detect_turnstile,
                            wait_for_turnstile, TURNSTILE_INTERCEPT_JS,
                            TURNSTILE_INJECT_JS)
from product_parser import (parse_rows, rows_from_graphql, graphql_body,
                            graphql_errors, SELECTORS, PAGE_CAP, MAX_LIMIT,
                            SORTS, DEFAULT_SORT, TIMEFRAMES, DEFAULT_TIMEFRAME,
                            RANKING_SLUGS, ACTIVITY_FILTERS,
                            DEFAULT_ACTIVITY_FILTER, GRAPHQL_ENDPOINT,
                            detect_bot_challenge, page_kind, normalize_url,
                            next_cursor, collection_slug_from_url, locale_of,
                            mode_for_url, served_by_opensea, site_host,
                            is_supported_host, source_of, unsupported_reason,
                            url_for_mode, row_class_for, ssr_matches_request,
                            ssr_mismatch_note)
from output_writer import merge_pages, finish_run, EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


# No browser channel is forced, and that is a measurement rather than an
# omission: Playwright's own Chromium was served HTTP 200 and the full
# inlined state on every capture taken for this repo, and so was a `curl`
# announcing itself as `curl/8.x`. Named here rather than at the call site so
# the smoke suite can assert the three engines agree on it.
DEFAULT_BROWSER_CHANNEL = None

# How long to wait for a remote browser to accept the CDP connection.
# Deliberately above the server's own give-up point, so the client is never
# the one that walks away first and leaves a profile half-open.
CDP_CONNECT_TIMEOUT_MS = 150_000

# The lowest share of rows that must carry the mode's own key field before
# the read is suspect. Measured 2026-09-17 on the captures this repo was
# built from: 50/50 items carried a title and a sku on the Ethereum and the
# Solana collection alike, 50/50 ranking rows carried a slug and a floor,
# 32/32 activity rows carried an event type and a time.
#
# There is deliberately no PRICE floor beside it, and that is this site
# rather than an oversight: an unlisted item has no price and most of a
# collection is unlisted — 282 of 9,998 on Bored Ape Yacht Club. A price
# floor would fire on every correct `--sort created` run.
FIELD_FLOOR = 95


def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer Chrome
    ships, and a UA claiming an older Chrome than what the JS engine, WebGL
    strings and TLS ClientHello all report is itself a mismatch a
    fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes: dedupe that mutates a running set inside the loop
    makes the OUTPUT depend on the order pages happened to arrive in (§8).
    Pages are strictly sequential here, which is exactly why keeping the
    merge order-independent costs nothing and keeps the family's contract.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # The cursor this page handed back, which is the ONLY way to ask for the
    # next one. None means the feed ended here.
    next_cursor: Optional[str] = None
    # What OpenSea says about the whole collection — total supply, listed
    # count, floor. Page 1 only; the cursor pages do not carry it.
    totals: Optional[dict] = None
    # None, always, on this site: OpenSea numbers nothing, and an unknown gap
    # must not read the same as a gap of zero (§8).
    gap: Optional[int] = None
    # How many rows page 1 rendered that this run could not use, because it
    # asked a different question than the page answers. Reported rather than
    # silently discarded.
    ssr_dropped: int = 0
    scroll: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# ---------------------------------------------------------------------------
# page_flow, bound to Playwright
# ---------------------------------------------------------------------------
# Every decision about WHAT to do with a page lives in page_flow.py so all
# three engines make it identically. What lives here is only HOW to ask this
# particular driver.
#
# The primitives are NAMED OPERATIONS rather than JavaScript (§1). Selenium's
# execute_script takes a function BODY with an explicit `return` while
# Playwright and pyppeteer take `() => expr`, so a shared module handing JS
# across this boundary would quietly acquire one driver's dialect.
def _count(page, selector: str) -> int:
    """How many elements match, or 0 if the page moved under us.

    GUARDED, like its twins in the other two engines. A scroll batch polling
    the card count while the page navigates makes Playwright raise
    `Execution context was destroyed`, which would leave the run at exit 1 —
    a CRASH — where the correct answer is "blocked" or "still loading".

    0 is the safe reading rather than a lie: every caller treats it as "no
    cards seen this poll", which makes a readiness wait keep waiting and a
    scroll batch report no growth.
    """
    try:
        return len(page.query_selector_all(selector))
    except (PWError, PWTimeout) as e:
        logger.debug("count(%s) failed: %s", selector, e)
        return 0


def _scroll_to_bottom(page) -> None:
    """Scroll the WINDOW to the end of the document.

    To `document.body.scrollHeight` rather than by a fixed wheel distance: a
    fixed wheel stopped three rounds short of the bottom on a sibling site's
    7,600px grid, so the lazy-load trigger was never reached and a run took
    30 of 50 cards while looking settled (§8).

    A real function object, not a string — see the module docstring on this
    site's CSP.
    """
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    except (PWError, PWTimeout) as e:
        logger.debug("scroll failed: %s", e)


def _page_height(page) -> Optional[int]:
    """The document's scroll height, or None if the page is mid-navigation."""
    try:
        return page.evaluate("() => document.body.scrollHeight")
    except (PWError, PWTimeout):
        return None


# Any status at or above 400 on a request to the site's own hosts, and the
# SAME threshold in all three engines. A threshold that differed between them
# would mean one engine reporting `complete` where its twins report `partial`
# on the identical run, which is precisely the drift the shared modules exist
# to prevent (§6).
_SITE_HOST_FRAGMENTS = ("opensea.io",)


def _watch_refusals(session) -> None:
    """Start counting refused responses from the site's own hosts."""
    session._refused = 0

    def _on_response(response):
        try:
            if response.status >= 400 and any(
                    fragment in response.url
                    for fragment in _SITE_HOST_FRAGMENTS):
                session._refused += 1
        except Exception:  # noqa: BLE001 — a listener must never break a run
            pass

    session.page.on("response", _on_response)


def _refused_count(session) -> int:
    return getattr(session, "_refused", 0)


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    return page_flow.min_matches(args.mode)


def _classify(page, html: str, status=None, mode: str = "items") -> str:
    return page_flow.classify(html, status, page.url, mode)


def _same_url(a: str, b: str) -> bool:
    from product_parser import strip_tracking
    return strip_tracking(a or "") == strip_tracking(b or "")


# Chromium's own names for "the proxy is the problem, not the site". Matched
# on the error text because Playwright surfaces them as a generic Error.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    Distinguishing this from an ordinary timeout matters because the two want
    opposite responses: a timeout deserves a retry from the same exit, while
    an unusable exit deserves a different one — retrying it unchanged just
    spends the budget on a proxy that is not going to answer (§8).
    """
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch a browser on `pool`'s current exit; return (browser, context, page).

    Factored out so a proxy rotation can tear the whole browser down and call
    it again. Swapping the proxy under a live session would be cheaper and
    wrong: cookies a bot manager issued against one exit, replayed from
    another, are a stronger signal than either address alone (§8).
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    channel = args.browser_channel
    if channel:
        try:
            browser = pw.chromium.launch(channel=channel, **launch_kwargs)
        except (PWError, PWTimeout) as e:
            # A fallback, not a downgrade: this site was measured serving the
            # bundled Chromium the full inlined state, so losing the
            # requested channel costs nothing that is known.
            logger.info(
                "Could not launch the %r channel (%s) — using Playwright's "
                "own Chromium instead, which this site was measured serving "
                "normally. Install the channel with `playwright install %s` "
                "if you want it.", channel, str(e)[:160], channel)
            browser = pw.chromium.launch(**launch_kwargs)
    else:
        browser = pw.chromium.launch(**launch_kwargs)

    ctx_kwargs = {"user_agent": _chrome_ua(browser.version),
                  "locale": args.locale,
                  "viewport": {"width": 1440, "height": 900}}
    init_script = None
    if args.fingerprint:
        # Only meaningful on this branch. Over --cdp-endpoint the Scraping
        # Browser already has its own fingerprint, and layering a second one
        # on top produces a mismatch rather than better cover.
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)",
                    fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        context.add_init_script(init_script)
    # THE ONLY MOMENT a Cloudflare Turnstile's parameters can be captured.
    # A Challenge page calls `turnstile.render(container, params)` once and
    # keeps nothing: `sitekey`, `action`, `cData` and `chlPageData` live only
    # inside that call, and `TurnstileTaskProxyless` needs all four. No static
    # read of the HTML, however careful, can produce a solvable task — so the
    # hook goes on the CONTEXT, before any page script runs, and covers every
    # document including the one a redirect lands on (§19).
    #
    # Harmless where there is no Turnstile, which on this site is every page
    # measured so far. Installed anyway because the alternative is
    # discovering on the day it matters that the one chance to capture the
    # parameters has already passed.
    context.add_init_script(TURNSTILE_INTERCEPT_JS)
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    Exists because a rotation replaces all three handles at once, and passing
    three mutable locals through every helper is how one of them ends up
    stale.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        _watch_refusals(self)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        On a remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    try:
        browser = pw.chromium.connect_over_cdp(
            args.cdp_endpoint, timeout=args.cdp_connect_timeout * 1000)
    except (PWError, PWTimeout) as e:
        # Playwright puts the endpoint it tried into the exception text, and
        # that endpoint is a URL with a password in it — repeated five times,
        # in the message plus a four-line call log. Unmasked it lands in the
        # terminal, in CI output and in any log the run is piped to, which is
        # the one thing this project promises does not happen. The host and
        # port are KEPT: which endpoint failed is the useful half and is not
        # the secret (§8).
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"A Scraping Browser profile allows ONE live connection at a "
            f"time, so `profile_locked` means something holds this `pid`. "
            f"Use a different pid, or reset the profile from the 2Captcha "
            f"dashboard."
        ) from None
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    # The Scraping Browser API exposes a documented CDP domain
    # (`Captcha.setAutoSolve` / `Captcha.solve`) that clears supported
    # challenges inside the browser. Tried first when --cdp-endpoint is set;
    # this script's own detect+solve logic still runs as a fallback.
    #
    # No challenge was met on this site while this repo was built, so what
    # follows is a capability rather than a measurement. Nothing is charged
    # for a page that carries no widget.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve",
                         {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA sent to 2captcha for solving."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning(
            "[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
    except Exception as e:  # noqa: BLE001
        logger.info("Captcha.setAutoSolve not available on this "
                    "--cdp-endpoint (%s) — relying on this script's own "
                    "detect+solve logic instead.", e)
    return browser, context, page


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching GLOBALLY rather than once is the point: a Playwright connection
# error repeats the endpoint five times, so a masker that handled only the
# first occurrence would print the password four times and look like it was
# working (§8).
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright RAISES rather than returning empty while a navigation is in
    flight ("Unable to retrieve content because the page is navigating"), and
    a challenge handler resolves by navigating — so the one moment this is
    called is the one moment it can fail. Returns None if the page will not
    hold still, so a caller can skip a check instead of failing the run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — "
                               "continuing without a snapshot.", attempts)
                return None
            logger.info("Page is navigating — retrying content() in %dms "
                        "(%d/%d).", pause_ms, attempt, attempts)
            page.wait_for_timeout(pause_ms)
    return None


# ---------------------------------------------------------------------------
# The cursor pages
# ---------------------------------------------------------------------------
# A REAL FUNCTION OBJECT, not a string. opensea.io's CSP has no
# `unsafe-eval`, so `page.evaluate("<expression string>")` and
# `wait_for_function` are both refused by the browser; a function passed like
# this goes through `Runtime.callFunctionOn` and is unaffected (§18).
#
# Issued from INSIDE the page rather than from Python for three reasons, and
# each of them is something a separate HTTP client would get wrong:
#
#   * it is same-origin, so the browser sends the cookies OpenSea set on the
#     navigation — including `__cf_bm`, which is Cloudflare's bot-management
#     cookie and the thing a challenge would key on;
#   * it reuses the TLS session and the HTTP/2 connection the page already
#     has, so the request looks exactly like the grid's own;
#   * whatever the engine is driving — a local Chromium, a remote Scraping
#     Browser over CDP — the request leaves from the SAME exit as the page,
#     with no second proxy configuration to keep in step.
#
# The body is built by `product_parser.graphql_body` and not here, so all
# three engines send byte-identical requests (§6).
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

    Never raises for an EXPECTED failure: a transport error, an HTTP status
    and a GraphQL `errors` array are all recorded on the outcome instead.
    That last one matters more here than in most of the family — this
    endpoint answers HTTP 200 with an `errors` array rather than a 4xx, so a
    caller that only checks the status code reads a failure as an empty page
    (§8, fail loudly).
    """
    outcome = PageOutcome(page_num=page_num, url=session.page.url)
    body = graphql_body(
        args.mode, slug=args.collection_slug, limit=args.limit, after=cursor,
        sort=args.sort, ranking=args.ranking, timeframe=args.timeframe,
        activity_filter=args.activity)

    for attempt in range(1, args.retries + 1):
        try:
            result = session.page.evaluate(
                GRAPHQL_FETCH_JS,
                {"endpoint": GRAPHQL_ENDPOINT, "body": body})
        except (PWError, PWTimeout) as e:
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
                # The one error this site produces on a healthy run is the
                # price-boundary one, and it is caught BEFORE the request is
                # spent (see `page_flow.price_boundary_reached`). Anything
                # reaching here is a real failure and is reported as one.
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
                payload, session.page.url, mode=args.mode, page=page_num,
                timeframe=TIMEFRAMES.get(args.timeframe, "ONE_DAY"),
                first_rank=args.rank_offset if args.mode == "collections" else None)
            data = payload.get("data") or {}
            feed = next((v for v in data.values() if isinstance(v, dict)), {})
            outcome.products = rows
            outcome.next_cursor = feed.get("nextPageCursor")
            outcome.state = "content"
            outcome.final_url = session.page.url
            return outcome

        if status in (403, 429, 503):
            # The endpoint refusing is a REFUSAL, not an empty page. Reported
            # as blocked so the run's exit code says so (§8).
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


def handle_captcha_if_present(page, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Runs after EVERY navigation, for ANY page. The static-HTML and runtime
    reCAPTCHA detectors are run and RECONCILED against each other rather than
    short-circuited, because they can disagree about the variant and the
    parameters for one are rejected for the other (§8).

    WHAT IS AND IS NOT KNOWN HERE. No challenge was met in the captures taken
    for this repo, and the pages carry no captcha configuration at all: 0
    occurrences of `challenges.cloudflare.com`, `cdn-cgi/challenge-platform`,
    `recaptcha`, `hcaptcha`, `turnstile`, `datadome`, `perimeterx` or
    `data-sitekey` across seven captures on 2026-09-17. So this path is
    UNEXERCISED on opensea.io, and saying that plainly is the point — a
    sibling repo shipped a README sentence claiming a captcha here "cannot be
    solved", which was a statement about its own missing code dressed up as a
    fact about a paid product, and it cost a release (§19).

    What IS true: the site sits behind Cloudflare, a managed challenge
    renders a Turnstile, and 2Captcha solves that with
    `TurnstileTaskProxyless`. The parameters for it can only be captured by
    the interception installed at context creation, because a Challenge page
    publishes no sitekey in its markup.
    """
    html = _content_when_settled(page)
    if html is None:
        return False

    # Detected is not the same as blocking. A challenge on a page whose rows
    # are already rendered guards nothing, and counting the anchors is
    # instant — which is why this check sits here rather than after the
    # readiness wait. The other way round would cost 25 wasted seconds on a
    # page the challenge genuinely gates, where solving FIRST is what makes
    # the content appear (§8).
    already_rendered = _count(page, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        # No reCAPTCHA. Turnstile is the other thing 2Captcha can solve, and
        # the RUNTIME reading is the one that matters: a Cloudflare Challenge
        # page publishes no sitekey in its markup, so only the interception
        # installed at context creation can produce a solvable challenge.
        challenge = (wait_for_turnstile(lambda js: page.evaluate(js),
                                        lambda sec: page.wait_for_timeout(sec * 1000),
                                        page_url=page.url)
                     or detect_turnstile(html, page.url))
        if challenge and not challenge.sitekey:
            logger.warning(
                "A Cloudflare Turnstile is on this page but no sitekey was "
                "captured, so it cannot be solved and nothing will be "
                "charged for it. That means the page rendered its widget "
                "before this run's interception script was installed — which "
                "should not happen on a page this engine navigated to, and "
                "does happen if the browser was attached to mid-flight.")
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

    if challenge.is_turnstile:
        # Turnstile hands its token back through `cf-turnstile-response` and
        # the page's own callback, not through `g-recaptcha-response`.
        called_back = page.evaluate(TURNSTILE_INJECT_JS, token)
        logger.info("Turnstile token injected%s.",
                    " and handed to the page's callback" if called_back
                    else " (no callback was captured — relying on the form "
                         "field)")
        if challenge.solved_user_agent:
            logger.info("2captcha solved it against user agent %r. Cloudflare "
                        "checks that on a Challenge page, so a mismatch here "
                        "is the likeliest reason a paid token is refused.",
                        challenge.solved_user_agent[:60] + "…")
    else:
        page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading page to continue.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def _scroll_the_feed(session, args, page_num: int) -> dict:
    """Scroll to the bottom until the grid stops growing.

    RUN ONLY WHEN THE INLINED STATE DID NOT PARSE, and that condition is the
    whole design. OpenSea server-renders fifty rows into the page's own
    state, so page 1 has everything it will write before a pixel paints —
    and the pages after it come from the endpoint rather than from the DOM.

    Where it DOES matter is the fallback: if the state's shape changes and
    the run drops to reading anchors, a tile that has not painted is a row
    that is not written. So the caller checks `page_flow.state_answered`
    first and only spends the rounds when it is False.
    """
    selector = _ready_selector(args)
    trace = page_flow.scroll_until_settled(
        lambda sel: _count(session.page, sel),
        lambda: _scroll_to_bottom(session.page),
        lambda: _page_height(session.page),
        session.page.wait_for_timeout,
        selector=selector)
    logger.info("Scrolled page %d: %d card(s) at first paint, %d after "
                "%d round(s).", page_num, trace["cards_before"],
                trace["cards_after"], trace["rounds"])
    return {"first_paint": trace["cards_before"],
            "reached": trace["cards_after"], "rounds": trace["rounds"]}


def _fetch_first_page(session, args, pool) -> PageOutcome:
    """Navigate to the run's URL and read the state the site inlined into it.

    Page 1 is always a real navigation, for a reason that is not cosmetic:
    it is what proves OpenSea served this request, it is what carries the
    collection's own totals, and it is what sets the cookies the cursor
    pages ride on.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a refusal, a challenge page and a dead exit are all recorded on
    the outcome instead.

    Always goes through `session.page`, never a captured local: a rotation
    replaces the browser, context and page together, and a stale handle is
    exactly the bug _BrowserSession exists to prevent.
    """
    url = args.url
    outcome = PageOutcome(page_num=1, url=url)
    has_pool = bool(pool and len(pool) > 1)
    # `RETRY_ON_BLOCKED` is CONSULTED, not merely documented — a policy
    # constant nothing reads is the same defect as dead code (§17).
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    # Counted across the whole block-retry loop, not per attempt: a page that
    # keeps coming back as a challenge would otherwise buy one solve per
    # rotation, which is how a run quietly turns into a bill.
    solves_bought = 0
    html, state, load_failed = None, "ok", False
    status = None

    for block_attempt in range(block_retries + 1):
        load_failed, exit_failed = False, None
        logger.info("Fetching page 1: %s", url)
        for attempt in range(1, args.retries + 1):
            try:
                response = session.page.goto(url, wait_until="domcontentloaded",
                                             timeout=60000)
                status = response.status if response else None
                load_failed = False
                break
            except (PWTimeout, PWError) as e:
                # A dead or misconfigured proxy raises PWError
                # (net::ERR_PROXY_CONNECTION_FAILED), not PWTimeout —
                # catching only the latter lets it escape as a traceback,
                # which is the likeliest failure the first time anyone points
                # --proxy-file at a real list (§8).
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

        if handle_captcha_if_present(session.page, args):
            session.page.wait_for_timeout(1000)

        html = _content_when_settled(session.page) or ""
        state = _classify(session.page, html, status, args.mode)

        # "Not painted yet" is not a fault, and telling it apart from one is
        # the distinction §8 is about. Wait for the anchor and re-classify
        # BEFORE the retry decision — retrying a shell buys another shell.
        if page_flow.is_unpainted(state, html):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page 1 is a shell the site served but has not filled "
                        "in (%d bytes, no cards) — waiting up to %.0fs for "
                        "the grid rather than spending a retry.",
                        len(html), wait_timeout / 1000)
            found = page_flow.wait_for_count(
                lambda sel: _count(session.page, sel),
                session.page.wait_for_timeout,
                _ready_selector(args), _min_matches(args, html), wait_timeout)
            if found < _min_matches(args, html):
                logger.info("The grid still had not painted after %.0fs "
                            "(%d match(es)).", wait_timeout / 1000, found)
            html = _content_when_settled(session.page) or html
            state = _classify(session.page, html, status, args.mode)

        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session.page, args):
                session.page.wait_for_timeout(1000)
                html = _content_when_settled(session.page) or html
                state = _classify(session.page, html, status, args.mode)
                # The VERIFIED outcome, and the only one worth reporting: a
                # "ready" task result is not evidence the token works (§19).
                if state == "content":
                    logger.info("The solve was accepted — page 1 is content "
                                "now.")
                else:
                    logger.warning("The solve was NOT accepted: page 1 is "
                                   "still %s. The purchase is spent.", state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty feed is
            # a CORRECT one, so retrying it would spend the budget
            # re-confirming the same right answer and rotating the exit would
            # blame an address for the URL it was given.
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
                # No pool, so nowhere else to go. A fresh browser CONTEXT
                # clears an edge challenge where a reload does not, which is
                # what `page_flow.RETRY_NEEDS_FRESH_CONTEXT` says — and a
                # constant no engine consulted would be dead policy dressed
                # up as enforcement (§17).
                #
                # NOT relaunched over --cdp-endpoint: a Scraping Browser
                # profile allows one live connection, so reconnecting risks
                # `profile_locked` and would lose the cookies the retry is
                # meant to build on.
                fresh = (page_flow.RETRY_NEEDS_FRESH_CONTEXT
                         and not args.cdp_endpoint)
                logger.warning("Page 1 came back as %s — waiting %.1fs and "
                               "re-fetching %s (%d/%d).", state, pause,
                               "in a FRESH browser context, which is what "
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
            session.page.screenshot(path=f"{args.out}_page1_debug.png")
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
        outcome.final_url = session.page.url
        return outcome

    if page_flow.should_parse(state) and page_flow.state_answered(html, args.mode):
        # THE FAST PATH, and the normal one: the page's own state already
        # names every row page 1 will write, so there is nothing to wait for
        # and nothing to scroll.
        logger.info("Page 1 shipped its rows in the page's own state — "
                    "parsing it directly (no readiness wait, no scroll).")
    elif page_flow.should_parse(state):
        selector, threshold = _ready_selector(args), _min_matches(args, html)
        content_timeout = page_flow.content_timeout_ms(args.mode)
        # A POLL, not wait_for_function: this site's CSP has no
        # `unsafe-eval` and would refuse an evaluated string outright (§18).
        found = page_flow.wait_for_count(
            lambda sel: _count(session.page, sel),
            session.page.wait_for_timeout, selector, threshold, content_timeout)
        session.page.wait_for_timeout(500)
        if found < threshold:
            logger.info("No tiles appeared within %.0fs. If this feed "
                        "genuinely holds nothing, that is the expected answer "
                        "and the run will report 0 rows (exit 4).",
                        content_timeout / 1000)
        outcome.scroll = _scroll_the_feed(session, args, 1)
        html = _content_when_settled(session.page) or html

    # Dumping on success, not only on failure: a run can return the right
    # NUMBER of rows with a field silently unpopulated, and then the only way
    # to tell a parsing bug from a too-early snapshot is the exact bytes (§9).
    if args.dump_html:
        dump_path = args.dump_html if args.pages == 1 else f"{args.dump_html}.page1"
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html or ""))

    rows = parse_rows(html or "", session.page.url, page=1, mode=args.mode,
                      timeframe=TIMEFRAMES.get(args.timeframe, "ONE_DAY"),
                      first_rank=1 if args.mode == "collections" else None)
    logger.info("Parsed %d row(s) from page 1.", len(rows))

    outcome.totals = page_flow.collection_totals_on_page(html or "") or None
    if args.ssr_usable:
        outcome.products = rows
        outcome.next_cursor = next_cursor(html or "", args.mode)
    else:
        # The rows page 1 rendered answer a DIFFERENT question than this run
        # is asking, and its cursor belongs to that other ordering. Dropped
        # rather than mixed in: two orderings in one file are two samples,
        # and `sort` in the sidecar could only be right about one of them
        # (§21 — the ordering is a column, not a presentation detail).
        outcome.ssr_dropped = len(rows)
        logger.info("%s", ssr_mismatch_note(
            args.mode, sort=args.sort, ranking=args.ranking,
            timeframe=args.timeframe, activity_filter=args.activity))
    outcome.gap = page_flow.page_gap(html or "", len(rows))
    outcome.final_url = session.page.url

    if not rows:
        debug_html = f"{args.out}_page1_debug.html"
        debug_png = f"{args.out}_page1_debug.png"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            session.page.screenshot(path=debug_png)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        if page_flow.parsed_nothing_from_a_served_page(html, len(rows)):
            # §20: a page that was SERVED, links to N items and parses to
            # zero rows is THIS PARSER'S bug, not an empty collection.
            # Reported as what it is, so a reader opens product_parser.py
            # instead of checking their URL for a typo.
            outcome.state = "parser_found_nothing"
            logger.error(
                "0 rows parsed from a page the site SERVED, which links to "
                "%d item(s). That is a parser regression rather than an empty "
                "collection: the inlined state's shape or the anchor pattern "
                "has changed. Saved what the browser saw to %s and %s — "
                "please open an issue with the .html attached.",
                page_flow.count_cards(html or ""), debug_html, debug_png)
        else:
            logger.warning("0 rows parsed — saved what the browser actually "
                           "saw to %s and %s. Open the .png to see it.",
                           debug_html, debug_png)
    return outcome


def _report_coverage(rows: List, page_num: int, mode: str) -> None:
    """Log what share of this page carries the columns the mode is for.

    Reported every time, not only when it looks wrong, so a consumer gets the
    number rather than a threshold someone guessed (§13).
    """
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
            "measured floor of %d%%. Every row of every capture this repo was "
            "built from had both, so this is the read breaking rather than "
            "the feed being unusual. Re-run with --dump-html; the data_source "
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
        # Refused WITH the reason rather than quietly running one worker,
        # which would look like the flag did something (§18).
        logger.warning("--concurrency %d is refused: %s.", concurrency,
                       page_flow.concurrency_refusal(args.url, args.mode))
        concurrency = 1

    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
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
                # `--pages` counts pages OF ROWS. Where page 1's own rows were
                # dropped, the navigation was infrastructure rather than a
                # data page, so the walk is one page longer and the flag keeps
                # meaning what it says.
                last_page = args.pages + (0 if args.ssr_usable else 1)
                # Where page 1's rows were dropped there is no cursor to
                # spend, and a null cursor means the START of the feed rather
                # than the end of it. The first endpoint request therefore
                # skips the exhaustion checks — without this the run reads its
                # own dropped cursor as "the feed ended" and writes nothing,
                # which it did once before this line existed.
                feed_started = args.ssr_usable
                for page_num in range(2, last_page + 1):
                    if page_flow.page_cap_reached(page_num):
                        logger.warning("Stopping at the %d-page cap.", PAGE_CAP)
                        stop_reason = "page_cap"
                        break
                    if feed_started and page_flow.price_boundary_reached(cursor):
                        # Caught BEFORE the request is spent: passing this
                        # cursor back answers `Something went wrong`, and the
                        # run would report an error for having reached the
                        # end of what it asked for.
                        stop_reason = "listed_items_exhausted"
                        logger.info("%s", page_flow.sort_note(
                            args.sort, rank_seen, first.totals))
                        break
                    if feed_started and page_flow.cursor_exhausted(cursor):
                        stop_reason = "cursor_exhausted"
                        logger.info("The site handed back no cursor after "
                                    "page %d — that is the end of this feed, "
                                    "and the run is COMPLETE holding "
                                    "everything it has.", page_num - 1)
                        break

                    if pool and pool.rotates_per_page():
                        # Honoured, and said out loud: a relaunch loses the
                        # document the cursor pages are issued from, so the
                        # run re-navigates to page 1's URL before continuing.
                        logger.info("--proxy-rotate per-page: rotating and "
                                    "re-opening %s, because the cursor "
                                    "request is issued from inside the open "
                                    "document.", args.url)
                        pool.advance(f"per-page rotation, page {page_num}")
                        session.relaunch()
                        session.page.goto(args.url, wait_until="domcontentloaded",
                                          timeout=60000)

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
                        # The data-side termination condition §7 asks for: a
                        # page with no rows on it is the end of the feed,
                        # whatever the cursor says.
                        stop_reason = "no_new_products"
                        logger.info("Page %d came back with no rows — "
                                    "treating that as the end of the feed.",
                                    page_num)
                        break
                    rank_seen += len(outcome.products)
                    cursor = outcome.next_cursor
                else:
                    # The loop ran to --pages without stopping early. If the
                    # site still has a cursor, the run is complete for what
                    # was ASKED for and there is more behind it; the sidecar
                    # records both.
                    if args.pages > 1 and cursor:
                        logger.info("Fetched the %d page(s) asked for and the "
                                    "feed goes on — pass a higher --pages to "
                                    "keep walking.", args.pages)
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

    # One-per-run context, in the sidecar rather than repeated down a column.
    extra = {
        "rows_new_per_page": fresh_by_page,
        "scroll": {o.page_num: o.scroll for o in outcomes if o.scroll},
        # The ORDERING, because on this site it decides WHICH rows are in the
        # file rather than what order they sit in — two runs under different
        # sorts are different samples, and diff_runs.py refuses that pair.
        "sort": args.sort if args.mode == "items" else None,
        "timeframe": args.timeframe if args.mode == "collections" else None,
        "ranking": args.ranking if args.mode == "collections" else None,
        "activity_filter": args.activity if args.mode == "activity" else None,
        "collection": args.collection_slug or None,
        "locale": locale_of(final_url),
        # §21's arithmetic: what the site says the collection holds, beside
        # what this run read. `complete` means the walk finished, not that it
        # read the whole collection.
        "collection_totals": totals,
        "rows_from_state": sum(1 for r in all_rows if r.data_source == "ssr"),
        "rows_from_endpoint": sum(1 for r in all_rows if r.data_source == "graphql"),
        "rows_from_dom": sum(1 for r in all_rows if r.data_source == "dom"),
        # Rows page 1 rendered that this run could not use. Non-zero means
        # the run asked for an ordering or a filter the page does not show,
        # which is legitimate and worth recording rather than hiding.
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
        description="OpenSea NFT marketplace scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="An OpenSea URL: a collection "
                        "(/collection/{slug}), its activity feed "
                        "(/collection/{slug}/activity), the ranking "
                        "(/collections), or one item (/item/{chain}/…). A "
                        "locale path works too — /ja/collection/{slug} — and "
                        "changes the wording, not the ids or the prices. "
                        "Required unless --collection or --mode collections "
                        "is given, or OPENSEA_URL is set in the environment "
                        "or in .env.")
    p.add_argument("--collection", default=None, metavar="SLUG",
                   help="A collection slug to read instead of --url, e.g. "
                        "`boredapeyachtclub`. Combined with --mode it builds "
                        "the right address: the items grid, or the activity "
                        "feed.")
    p.add_argument("--mode", choices=["items", "collections", "activity"],
                   default=None,
                   help="Which view to take. Inferred from the URL by "
                        "default. `items` is one row per NFT; `collections` "
                        "is OpenSea's own ranking, one row per collection; "
                        "`activity` is one row per event (sale, listing, "
                        "offer, transfer, mint). The three yield DIFFERENT "
                        "row classes — see output_writer.py — and "
                        "diff_runs.py refuses to compare two of them.")
    p.add_argument("--sort", choices=sorted(SORTS), default=DEFAULT_SORT,
                   help=f"How to order a collection's items (default "
                        f"{DEFAULT_SORT}, which is what the collection page "
                        f"itself shows). THIS DECIDES WHICH ROWS YOU GET, not "
                        f"just their order: under `price` the cursor carries "
                        f"the last row's price and the walk stops where the "
                        f"LISTED items end — 282 of 9,998 on Bored Ape Yacht "
                        f"Club, measured 2026-09-17 — which is exactly right "
                        f"for a floor scrape and is not the whole collection. "
                        f"Use `created` to walk all of it.")
    p.add_argument("--timeframe", choices=sorted(TIMEFRAMES),
                   default=DEFAULT_TIMEFRAME,
                   help=f"Which window --mode collections ranks and reports "
                        f"volume over (default {DEFAULT_TIMEFRAME}). The "
                        f"volume, sales and floor-change columns follow it, "
                        f"and the sidecar records which was used: a one-hour "
                        f"figure and an all-time one are both correct and not "
                        f"comparable.")
    p.add_argument("--ranking", choices=list(RANKING_SLUGS), default="TRENDING",
                   help="Which ranking --mode collections reads (default "
                        "TRENDING). TOP is by volume over the timeframe.")
    p.add_argument("--activity", choices=sorted(ACTIVITY_FILTERS),
                   default=DEFAULT_ACTIVITY_FILTER,
                   help=f"Which events --mode activity keeps (default "
                        f"{DEFAULT_ACTIVITY_FILTER}). `sales` is the one most "
                        f"callers want; `all` includes listings, offers, "
                        f"transfers and mints.")
    p.add_argument("--pages", type=int, default=1,
                   help=f"Pages to fetch (default 1, cap {PAGE_CAP}). Page 1 "
                        f"is the page itself and carries 50 rows; every page "
                        f"after it is a cursor request to the site's own "
                        f"endpoint and carries up to --limit. A run stops "
                        f"early, and reports COMPLETE, when the site hands "
                        f"back no cursor or when a price-ordered walk reaches "
                        f"the end of the listed items.")
    p.add_argument("--limit", type=int, default=MAX_LIMIT, metavar="N",
                   help=f"Rows per cursor page (default and maximum "
                        f"{MAX_LIMIT} — the endpoint states that limit "
                        f"itself). Page 1 always carries the 50 the site "
                        f"server-renders, whatever this says.")
    p.add_argument("--category", default=None,
                   help="Accepted for the family's shape and mapped onto "
                        "--collection, because on OpenSea the thing a run is "
                        "pointed at IS a collection.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Delay between pages, seconds (default 1.0). The "
                        "endpoint publishes its own budget in an "
                        "`x-ratelimit-remaining` header (400 when this was "
                        "written), so this is politeness with a number behind "
                        "it rather than a guess.")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for the family's shape and REFUSED above 1, "
                        "with the reason: OpenSea paginates with an opaque "
                        "cursor, so page 5's request does not exist until "
                        "page 4 has been read and there is nothing to hand a "
                        "second worker. Run several collections at once, one "
                        "process each.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "The pause between attempts doubles each time. A page "
                        "that comes back EMPTY is not retried — see "
                        "page_flow.STATE_POLICY — because an empty feed is a "
                        "correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter "
                        "(default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="opensea_items",
                   help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). It does NOT decide "
                        "the site language: the URL PATH does "
                        "(/ja/collection/{slug}). This only affects what the "
                        "browser claims about itself.")
    p.add_argument("--browser-channel", default=DEFAULT_BROWSER_CHANNEL,
                   metavar="CHANNEL",
                   help="Which installed browser to drive. Unset by default, "
                        "which means Playwright's own Chromium — and on this "
                        "site that is enough: it was served HTTP 200 and the "
                        "full inlined state on every capture taken, as was a "
                        "plain `curl`. Pass `chrome` to drive an installed "
                        "Chrome anyway (`playwright install chrome`).")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line (# comments and "
                        "blank lines skipped) to rotate across. Wins over "
                        "--proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES),
                   default="per-run",
                   help="per-run (default): one exit for the whole run. "
                        "per-page rotates between cursor pages, which costs a "
                        "re-navigation — the cursor request is issued from "
                        "inside the open document, so a relaunch has to "
                        "re-open page 1 first. The run says so when it "
                        "happens.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup, so concurrent runs do "
                        "not all begin on the first exit in the file.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back refused, retry it from this "
                        "many OTHER exits before giving up (default 2). Needs "
                        "a pool of more than one; ignored otherwise.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found. Off "
                        "by default so a failed run can't overwrite a good "
                        "result with an empty one; exit code is 4 either way.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's "
                        "Fingerprint API and apply it to the launched "
                        "browser. Needs --twocaptcha-key. Ignored with "
                        "--cdp-endpoint, where the Scraping Browser supplies "
                        "its own.")
    # ONE OS-family tag, not a list — and the default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop" across
    # this family, which the API rejects with HTTP 400, so --fingerprint
    # failed on every invocation in four repos at once (§17).
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. Use --fp-country to narrow further. "
                        "(default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country — a US fingerprint on a "
                        "German IP is a contradiction.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current "
                        "JSON API (api.2captcha.com/createTask); v1 is the "
                        "legacy in.php/res.php pair.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "challenge if the content is not already readable. "
                        "always: solve whenever one is detected. What this "
                        "repo implements: reCAPTCHA v2, v2-invisible, v3 and "
                        "enterprise, and Cloudflare Turnstile including the "
                        "Challenge-page form, whose parameters are captured "
                        "by an init script because the page publishes no "
                        "sitekey. No challenge was met on opensea.io while "
                        "this was written, so the path is unexercised HERE — "
                        "which is a fact about this repo's testing and not a "
                        "claim about what a solver can do (§19). A page "
                        "carrying no widget is reported unsolved rather than "
                        "charged for.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or "
                        "0.9 — the API only accepts these three). Ignored for "
                        "v2 widgets.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP "
                        "instead of launching one locally, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser API "
                        "endpoint, or any browser that exposes a CDP URL. "
                        "--proxy, --browser-channel and --headless/--headful "
                        "are ignored when this is set.")
    p.add_argument("--cdp-connect-timeout", type=float,
                   default=CDP_CONNECT_TIMEOUT_MS / 1000, metavar="SECONDS",
                   help=f"How long to wait for --cdp-endpoint to accept the "
                        f"connection (default "
                        f"{CDP_CONNECT_TIMEOUT_MS // 1000}). Deliberately "
                        f"high: a Scraping Browser provisions a browser when "
                        f"the WebSocket upgrade arrives, and giving up "
                        f"earlier than the server does leaves the profile "
                        f"held by a half-open session.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save exactly what the parser is given, on success as "
                        "well as failure. Page 1 is written as HTML; every "
                        "cursor page after it is written as "
                        "`<path>.pageN.json`, because what the parser saw "
                        "there was a JSON body and not a document.")
    # HEADLESS by default, and that is this site's own measurement rather
    # than the family's habit (§19 says to measure it once per site).
    p.add_argument("--headless", dest="headless", action="store_true",
                   default=True,
                   help="Run headless. THE DEFAULT, and measured identical to "
                        "headful on this site.")
    p.add_argument("--headful", dest="headless", action="store_false",
                   help="Run with a real browser window. Measured identical "
                        "to headless here, so this is for watching a run or "
                        "for trying a lever if the site ever starts refusing. "
                        "Needs a display.")
    args = p.parse_args()
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
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
        # Refused rather than attempted. The parser's state reader, its path
        # patterns and its whole GraphQL vocabulary are this site's, so
        # pointing it at another marketplace would not fail loudly — it would
        # return zero rows and look like an empty collection (§5).
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
                f"{kind} page and reads as --mode {inferred}. Leave --mode "
                f"off and it is inferred, or point --url at the right page: "
                f"/collection/{{slug}} for items, "
                f"/collection/{{slug}}/activity for activity, /collections "
                f"for the ranking.")

    args.collection_slug = collection_slug_from_url(args.url) or (args.collection or "")
    if args.mode in ("items", "activity") and not args.collection_slug and kind != "item":
        p.error(f"--mode {args.mode} needs a collection; {args.url!r} names "
                f"none.")
    # Whether page 1's own rows answer the question this run is asking. See
    # product_parser.SSR_REQUEST for what each page renders and how each of
    # those was measured.
    args.ssr_usable = ssr_matches_request(
        args.mode, sort=args.sort, ranking=args.ranking,
        timeframe=args.timeframe, activity_filter=args.activity)

    # Where `rank` starts on page 1 of a ranking. Threaded through the cursor
    # pages so a collection's position is its position in the WHOLE ranking
    # rather than within its page.
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
                    "--pages %d will fetch one page. The run is complete "
                    "holding it.", args.pages)
        args.pages = 1
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint "
                     "API uses the same key, though it's a separate "
                     "subscription from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second one on top creates a mismatch "
                       "rather than better cover.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). A harness that sees exit 1 goes looking for a bug in
        # the scraper instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
