"""page_flow.py — what to do with the page OpenSea just gave us.

OpenSea answers a request five ways and four of them want a different
response, which is why this module exists rather than the same triage being
written three times inside three engines and drifting apart:

    content    the page's inlined state names rows, or its tiles rendered
    empty      a collection slug that does not exist (a real HTTP 404), or a
               feed the site says is empty
    shell      served, built out of OpenSea's own assets, nothing shipped or
               painted yet. Wants a WAIT, not a refetch
    challenge  Cloudflare's managed challenge. Worth RETRYING in a fresh
               context and worth handing to a solver
    blocked    a refusal, or a document that is not OpenSea's HTML at all —
               including Chromium's own error page, which carries
               `<title>opensea.io</title>` and fools any title check

The policy lives in `STATE_POLICY` as DATA, so an engine cannot quietly
disagree with its twins about whether a page is worth retrying or worth
paying for.

`content` is the NORMAL first state
-----------------------------------
OpenSea server-renders every feed this scraper reads. The first response of
`/collection/boredapeyachtclub` already carries fifty items with their
listings, offers, last sales, traits, owners and rarity ranks inside the
urql transport payload — before a pixel has painted and identically to what
the site's own GraphQL endpoint returns for the same query (49 of 50 rows
identical across 18 stable columns, measured 2026-09-17; the one difference
was a listing whose price had changed in the forty minutes between the
capture and the query).

So the readiness wait and the scroll loop below are BOUNDED SAFETY NETS for
the payload shape changing under us, and not the main event. An engine that
parses the first response and then walks cursors has everything.

Everything here is pure or driven through small callables, so each engine
passes its own driver's primitives and keeps its browser plumbing to itself:

    count(selector) -> int              how many elements match
    scroll_to_bottom() -> None          scroll the window to the document end
    page_height() -> Optional[int]      document.body.scrollHeight
    sleep(ms) -> None                   wait

No JavaScript crosses that boundary in either direction: Selenium's
`execute_script` takes a function BODY with an explicit `return` where
Playwright and pyppeteer take `() => expr`, so this module names the
OPERATION and each engine spells it in its own driver's dialect.
"""

from __future__ import annotations

import argparse
import logging
from typing import Callable, Dict, List, Optional

from product_parser import (SELECTORS, PAGE_CAP, CONCURRENCY_REASON,
                            PAGE_STATES, PAGE_URL_REASON, collection_totals,
                            count_cards, cursor_key_is_null,
                            detect_block_marker, detect_page_state,
                            is_challenge_page, payload_answered,
                            served_by_opensea)

logger = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
READY_SELECTOR = SELECTORS["item_card"]

# What a MODE is waiting to see. `items` and `activity` are both made of
# links to items; the ranking is made of links to collections, and waiting
# for an item link there would wait out the whole timeout on a correct page.
READY_SELECTOR_BY_MODE = {
    "items": SELECTORS["item_card"],
    "activity": SELECTORS["item_card"],
    "collections": SELECTORS["collection_link"],
}

# Above 1: waiting for a single match resolves on the site's own
# navigation — the header links to /collections and to several collections
# by name — long before a grid paints. Measured first paints on 2026-09-17:
# 32 distinct item links on a collection page, 25 collection links on the
# ranking, 19 on an activity feed.
MIN_CARD_MATCHES = 2

# Generous against a measured first paint of 2-4s on a 2.3 MB collection
# page.
CONTENT_TIMEOUT_MS = 25_000


def ready_selector(mode: str = "") -> str:
    return READY_SELECTOR_BY_MODE.get(mode, READY_SELECTOR)


def min_matches(mode: str = "", expected: Optional[int] = None) -> int:
    """How many matches mean "painted".

    `expected` clamps it for a caller that knows better — an activity feed
    filtered to one event type can legitimately hold a single row, and a
    threshold of two would spend the whole timeout on a correct page.
    """
    floor = MIN_CARD_MATCHES
    if expected is None or expected <= 0:
        return floor
    return max(1, min(floor, expected))


def content_timeout_ms(mode: str = "") -> int:
    return CONTENT_TIMEOUT_MS


def state_answered(html: Optional[str], mode: str = "items") -> bool:
    """Whether the page's own inlined state already names rows.

    The fast path, and the reason page 1 costs one fetch: when this is True
    there is nothing to wait for and nothing to scroll.
    """
    return payload_answered(html, mode)


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, minimum: int, timeout_ms: int,
                   poll_ms: int = 500) -> int:
    """Poll until `minimum` elements match, or the timeout runs out.

    Polls through the driver's own element-count primitive rather than
    waiting on an evaluated STRING, and on THIS site that is not a
    precaution — it is required. opensea.io's Content-Security-Policy is

        script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' …

    with no `unsafe-eval` (read from the response header of a collection
    page, 2026-09-17). Playwright's `wait_for_function` hands the browser a
    string to evaluate, which that policy refuses outright: on a sibling site
    with the same gap it died with `EvalError` and took the run down with
    exit 1. Counting elements over the protocol is a CDP call, works
    under any CSP, and spells the same in all three drivers.

    Returns the count it ended on, whether or not it reached the floor:
    reporting a timeout is not the same as exiting on one.
    """
    waited = 0
    found = count(selector)
    while found < minimum and waited < timeout_ms:
        sleep(poll_ms)
        waited += poll_ms
        found = count(selector)
    if found < minimum:
        logger.info("readiness wait ended at %d matches (wanted %d) after "
                    "%dms", found, minimum, waited)
    return found


# ---------------------------------------------------------------------------
# Lazy loading
# ---------------------------------------------------------------------------
# Lazy loading. OpenSea's grids ARE infinite-scroll — the site's own UI
# fetches the next fifty over GraphQL as you reach the bottom — but this
# scraper never needs that, because it asks for the next page over the same
# endpoint the grid uses and gets a hundred rows instead of fifty.
#
# The loop is kept for one case: the DOM fallback. If the inlined state's
# shape changes and a run drops to reading anchors, a tile that has not
# painted is a row that is not written, and then scrolling is the difference
# between 32 rows and fewer. So it runs only when the state produced nothing.
#
# Scroll to `document.body.scrollHeight` rather than wheeling a fixed
# distance — a fixed wheel stops short on a long grid and the trigger is
# never reached — and require the count AND the height to hold still for
# THREE rounds, because the next batch takes longer to arrive than a single
# pause.
SCROLL_STABLE_ROUNDS = 3
SCROLL_MAX_ROUNDS = 6
SCROLL_PAUSE_MS = 2_000


def scroll_until_settled(count: Callable[[str], int],
                         scroll_to_bottom: Callable[[], None],
                         page_height: Callable[[], Optional[int]],
                         sleep: Callable[[int], None],
                         selector: str = "",
                         max_rounds: int = SCROLL_MAX_ROUNDS) -> Dict[str, int]:
    """Scroll to the bottom until neither the card count nor the height moves.

    Returns a small trace — rounds spent, cards before and after — which goes
    into the run's sidecar.
    """
    selector = selector or READY_SELECTOR
    started = count(selector)
    stable = 0
    last_height = None
    rounds = 0
    while rounds < max_rounds and stable < SCROLL_STABLE_ROUNDS:
        before = count(selector)
        scroll_to_bottom()
        sleep(SCROLL_PAUSE_MS)
        rounds += 1
        height = page_height()
        after = count(selector)
        if after == before and height == last_height:
            stable += 1
        else:
            stable = 0
        last_height = height
    ended = count(selector)
    return {"rounds": rounds, "cards_before": started, "cards_after": ended}


# ---------------------------------------------------------------------------
# Classification and the policy that follows from it
# ---------------------------------------------------------------------------
def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "", mode: str = "items") -> str:
    """Which of the five states this response is.

    `status` is positional and comes SECOND, matching `detect_page_state`.
    Getting that wrong is not a style question: a sibling repo shipped two of
    three engines calling this as `classify(html, url=…)`, both crashed on
    their first fetch, and nothing short of a live run or a signature-binding
    check saw it. This repo's smoke suite binds every shared-module
    call in every engine for that reason.

    `mode` matters here and does not in the siblings: which inlined operation
    counts as content depends on what is being read, and a collection page
    classified against the ranking's field would come back as a shell.
    """
    if html is None:
        return "blocked"
    return detect_page_state(html, status, url, mode)


# The retry/solve/blocked decision as DATA rather than as three copies of an
# if-chain in three engines.
#
#   parse    is there anything on this page worth writing down?
#   retry    would fetching it again, in a fresh context, plausibly help?
#   solve    is there something to pay a solver for?
#   blocked  does this count towards exit 3?
STATE_POLICY: Dict[str, Dict[str, bool]] = {
    "content":   {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # A collection slug that does not exist, under a real HTTP 404, or a feed
    # the site itself says is empty. Asked and answered; a second fetch
    # returns the same thing. Not blocked: exit 4 is "ran fine, found
    # nothing", which is exactly what happened.
    "empty":     {"parse": False, "retry": False, "solve": False, "blocked": False},
    # Served and still painting. Parsed rather than discarded, because by the
    # time an engine asks, the readiness wait has already run.
    "shell":     {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # Retried in a fresh context, HANDED TO THE SOLVER, and counted as
    # blocked if neither works.
    #
    # OpenSea sits behind Cloudflare — `server: cloudflare` and a `cf-ray` on
    # every response measured — and Cloudflare's managed challenge renders a
    # Turnstile, which 2Captcha solves with `TurnstileTaskProxyless`. No
    # challenge was met while this repo was built (see the README for the
    # address and the date), so `solve: True` here is a CAPABILITY and not a
    # measurement of this site. The one thing that must never be written is
    # that it cannot be solved, because that would be a sentence about this
    # repo's code dressed up as a fact about a paid product.
    #
    # Nothing is charged for a page that carries no widget: the solver raises
    # rather than building a task from a sitekey-less detection, which is
    # "detected is not paying", with a price tag on it.
    "challenge": {"parse": False, "retry": True,  "solve": True,  "blocked": True},
    "blocked":   {"parse": False, "retry": True,  "solve": False, "blocked": True},
}


# Every state the parser can return has a row here, and every row here is a
# state the parser can return. Asserted at import rather than in a test,
# because a state with no policy falls back to `blocked` — which is the safe
# direction and also the silent one (a policy that looks enforced and is
# not).
assert set(STATE_POLICY) == set(PAGE_STATES), (
    "STATE_POLICY and product_parser.PAGE_STATES disagree: "
    f"{sorted(set(STATE_POLICY) ^ set(PAGE_STATES))}")


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["parse"]


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["blocked"]


def is_unpainted(state: str, html: Optional[str]) -> bool:
    """Whether this page is served but has not painted its grid yet."""
    if state != "shell":
        return False
    return served_by_opensea(html or "")


# ---------------------------------------------------------------------------
# Blocks, and what is actually known about them here
# ---------------------------------------------------------------------------
# NOTHING WAS REFUSED WHILE THIS REPO WAS BUILT, with one sharp exception
# that is worth more than the rule. From one Hetzner datacentre address in
# Helsinki (AS24940) on 2026-09-17, every page kind and every locale tried
# answered HTTP 200 — to a real Chrome User-Agent, to `curl/8.x`, to no
# User-Agent at all, to a `HeadlessChrome/140` token and to a made-up
# `opensea-scraper/0.1`.
#
# THE EXCEPTION: `Python-urllib/3.13` is refused with HTTP 403, on the HTML
# route and on the GraphQL endpoint alike, while `python-requests/2.32` from
# the same address in the same minute is served normally. Cloudflare has a
# rule against that one signature. It is named here because the failure it
# produces is the most misleading one this site can give: a reader who tries
# the endpoint with `urllib` gets a 403 and concludes OpenSea blocks
# scrapers, when what it blocks is that string.
#
# The constants below are the family's defaults and are NOT measurements of
# this site. They are here so a run that does meet a refusal behaves like its
# siblings rather than inventing something, and they stay unmeasured until
# someone meets one — at which point the number, the date and the address
# belong in this comment.
RETRY_ON_BLOCKED = True
BLOCK_RETRIES_WITHOUT_POOL = 3
SOLVES_PER_PAGE = 1


class SolveBudget:
    """One page's allowance of paid captcha solves.

    Every engine creates ONE of these per page and passes it to EVERY call
    that may buy a solve: the check before the page is classified and the
    one after. It is spent immediately BEFORE the solver is called, so a
    detection that pays for nothing (no key, rows already on the page, no
    sitekey) costs nothing, and it is never reset by a retry, a fresh
    context or a rotation to another exit: a page that keeps coming back as
    a challenge is a reason to re-fetch, not a new allowance.
    """

    def __init__(self, limit: int = None):
        self.limit = SOLVES_PER_PAGE if limit is None else int(limit)
        self.spent = 0

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def spend(self) -> bool:
        """Take one solve from the allowance. False when none is left."""
        if self.spent >= self.limit:
            return False
        self.spent += 1
        return True

# There is deliberately NO `BLOCK_RETRIES_WITH_POOL` beside the constant
# above, and its absence is the point. It used to be here, set to 4, and
# nothing read it: with a pool the budget is `--proxy-block-retries`, which
# defaults to 2, so the constant was a second number claiming a different
# policy while the flag quietly decided the real one. That is a
# policy constant nothing consults — the same defect as dead code, and
# harder to see, because the prose reads like enforcement. The with-pool
# budget is the FLAG; the constant above is the fallback for when there is
# no pool and therefore no flag to honour.

# Whether a retry has to discard the browser context rather than reload the
# page. True, on the family's rule that a challenge issued against one
# session is not cleared by asking that same session again — and on the rule
# "a rotation is a fresh browser", which is the same rule with a proxy in it.
# Consulted by all three engines; a constant no engine read would be the
# defect of a policy that looks enforced and is not.
RETRY_NEEDS_FRESH_CONTEXT = True


# ---------------------------------------------------------------------------
# A throttle is not a block
# ---------------------------------------------------------------------------
# A lesson carried here from a sibling site that could measure it: a
# refusal and a throttle want OPPOSITE responses. A block says "this exit is
# unwelcome", so the answer is a different exit. A throttle says "too fast",
# so the answer is the SAME exit and a longer wait — rotating away from it
# spends the block budget and an exit rotation on a page that was about to
# come back, and reports exit 3 to a reader who then goes and buys a proxy
# they do not need.
#
# WHAT IS AND IS NOT MEASURED HERE. OpenSea's endpoint publishes an
# `x-ratelimit-remaining` header, which reads 399 and — measured 2026-10-13
# — does not move: a burst of 60 requests in a few seconds returned HTTP 200
# sixty times with the counter unchanged at 399 every time, and no
# `retry-after` on any of them. So no throttle has ever been reached from
# here, and this path is IMPLEMENTED AND UNEXERCISED on this site, in the
# same sense as the captcha path. That is a statement about this repo's
# testing, not a claim that the endpoint has no limit: a counter that does
# not move is a counter that is not telling us anything, which is a reason to
# handle the status correctly rather than a reason to assume it cannot
# arrive.
#
# A third-party audit on 2026-10-13 asked for a cooldown policy keyed on the
# KIND of response. This is that, narrowed to the distinction the family has
# actually measured elsewhere.
THROTTLE_STATUSES = (429, 503)
BLOCK_STATUSES = (403,)

# Its OWN budget, separate from `--retries`. `--retries` is the caller's
# budget for transient faults; spending it on a throttle means a page that
# would have come back is reported as a failure instead.
THROTTLE_RETRIES = 3
# The first wait, doubling thereafter. Deliberately longer than the
# transport retry's: a throttle is answered by waiting, not by trying again
# promptly.
THROTTLE_BASE_SECONDS = 5.0
# A server asking for longer than this is not something to sit through
# inside one page fetch.
THROTTLE_MAX_SECONDS = 120.0


def is_throttle(status) -> bool:
    """Whether this status means "too fast" rather than "not you"."""
    return status in THROTTLE_STATUSES


def is_refusal(status) -> bool:
    """Whether this status means the request was refused outright."""
    return status in BLOCK_STATUSES


def throttle_wait(attempt: int, retry_after=None,
                  base: float = THROTTLE_BASE_SECONDS) -> float:
    """How long to wait before asking the same exit again.

    `retry_after` is the server's own answer and WINS when it gives one —
    guessing over a number the other side stated is how a client gets its
    budget cut further. It arrives as a header value, so it may be a string,
    may be a date rather than a count of seconds (unparsed here: the
    date form is rare on an API and a wrong parse would be worse than the
    fallback), or may be absent.

    Capped, because a run should report a long throttle rather than sit
    through it silently.
    """
    seconds = None
    if retry_after is not None:
        try:
            seconds = float(str(retry_after).strip())
        except (TypeError, ValueError):
            seconds = None
    if seconds is None or seconds <= 0:
        seconds = base * (2 ** max(0, attempt - 1))
    return min(float(seconds), THROTTLE_MAX_SECONDS)


# Errors that mean "the browser never started", as opposed to "the site
# refused us". A missing Chromium, a sandbox that will not let one run, a
# driver that cannot be launched — none of these is a fault of this code or
# of the site, and all three used to reach the user as a traceback and exit
# 1, which says "there is a bug here" to anyone reading exit codes.
#
# Raised by a third-party audit on 2026-10-13, whose own first attempt died
# this way under a macOS sandbox (`MachPortRendezvousServer: Permission
# denied`). They correctly called it an environment limit; the scraper
# should have said so itself.
_LAUNCH_FAILURE_MARKERS = (
    "executable doesn't exist",
    "please run the following command to download new browsers",
    "playwright install",
    "browsertype.launch",
    "machportrendezvousserver",
    "no usable sandbox",
    "failed to launch",
    "chrome not reachable",
    "cannot find chrome binary",
    "session not created",
)


def is_launch_failure(error) -> bool:
    """Whether this error means the BROWSER never started."""
    text = str(error or "").lower()
    return any(marker in text for marker in _LAUNCH_FAILURE_MARKERS)


def launch_failure_advice(error) -> str:
    """What to do about a browser that would not start.

    Short on purpose: the full traceback is a stack through a driver the
    reader did not write, and the actionable part is one line.
    """
    text = str(error or "")
    hint = ("Install the browser this engine drives — `playwright install "
            "chromium` for the Playwright engine, a matching chromedriver "
            "plus Chrome for Selenium, or `--chromium-path` for pyppeteer.")
    if "sandbox" in text.lower() or "machport" in text.lower():
        hint = ("The operating system refused to let a browser start. That "
                "is usually a sandbox or a container without the privileges "
                "Chromium needs — try outside it, or pass --cdp-endpoint to "
                "drive a browser somewhere else.")
    return ("the browser did not start, so nothing was fetched and this is "
            "not a site problem: %s\n%s"
            % (text.strip().splitlines()[0][:300] if text.strip() else
               "no detail given", hint))


def block_advice(html: Optional[str], headless: bool, has_pool: bool,
                 credentials_stripped: bool = False) -> str:
    """What a reader should actually DO about this block.

    Says what is measured and says what is not. The temptation is to repeat a
    sibling repo's ordering — "use a real window first" — but that ordering
    came from ITS measurements. Here headless and headful were measured
    identical, so repeating it would be an inherited number wearing a
    fact's clothes.
    """
    marker = detect_block_marker(html or "") or "no vendor marker"
    lead = f"blocked ({marker})"
    hints: List[str] = []

    # SAY WHAT WAS OBSERVED BEFORE SAYING WHY. When the engine itself
    # stripped the proxy credentials it knows the likeliest cause, and the
    # general advice below — raise --delay, try --headful — points a reader
    # away from it. Measured 2026-10-13: Selenium with an authenticated
    # proxy produced a 39-byte empty document on every attempt and the same
    # three modes succeeded through the same proxy on the two engines that
    # CAN authenticate.
    if credentials_stripped:
        return (f"{lead}. This engine STRIPPED the proxy credentials before "
                f"launching — it has no way to send them — so the exit "
                f"almost certainly refused the request and the browser never "
                f"got a page: {len(html or '')} bytes came back. That is the "
                f"documented Selenium limitation rather than the site "
                f"refusing you. Use playwright_scraper.py or "
                f"puppeteer_scraper.py for an authenticated proxy, or drop "
                f"--proxy.")

    if is_challenge_page(html or ""):
        lead = f"blocked by a challenge page ({marker})"
        hints.append("a challenge is transient and clears in a FRESH BROWSER "
                     "CONTEXT rather than on a reload — the retry loop "
                     "already opens a new one between attempts")
        hints.append("--solve-captcha when-blocked (the default) hands a "
                     "Cloudflare Turnstile to 2Captcha if one is actually "
                     "rendered; a page carrying no widget is reported "
                     "unsolved rather than charged for")
    else:
        hints.append("no challenge widget was found on the page, so there is "
                     "nothing to solve here — this is a refusal rather than "
                     "a puzzle")

    if headless:
        hints.append("headless and headful were measured IDENTICAL on this "
                     "site, so --headful is worth trying but is not the known "
                     "fix it is on some sibling sites")
    if not has_pool:
        hints.append("this site served every address measured — a Finnish "
                     "datacentre, a US Scraping Browser exit, a GitHub "
                     "runner and a Russian residential exit — so a refusal "
                     "is more likely to be the request RATE than the "
                     "address: raise --delay before reaching for "
                     "--proxy-file")
    else:
        hints.append("with a pool in play, raise --delay before raising the "
                     "request rate: N exits still means N times the traffic")
    return lead + ". " + "; ".join(hints) + "."


# ---------------------------------------------------------------------------
# Pagination — cursors, and the one that ends a walk early on purpose
# ---------------------------------------------------------------------------
def number_at_least(flag: str, minimum, kind=int):
    """An argparse `type=` that refuses a value below `minimum`, naming `flag`.

    Counts and delays are refused at parse time rather than clamped: a
    negative `--delay` or a `--pages 0` would otherwise be quietly reinterpreted
    and look honoured while not being.
    """
    def parse(value):
        try:
            number = kind(value)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(
                "%s must be a %s, got %r"
                % (flag, "whole number" if kind is int else "number", value))
        if number < minimum:
            raise argparse.ArgumentTypeError(
                "%s must be at least %s, got %r" % (flag, minimum, value))
        return number
    parse.__name__ = "number_at_least_%s" % flag.strip("-").replace("-", "_")
    return parse


def attempts(value) -> int:
    """`--retries` as a number of ATTEMPTS, never zero.

    Used as argparse's `type=`, so the refusal happens at parse time with a
    usage message rather than four seconds into a run.

    The bug this closes was found by a third-party audit on 2026-10-13, and
    the audit was right about the symptom and wrong about the cause.
    Its live run passed `--retries 0`; the load loop is
    `for attempt in range(1, args.retries + 1)`, so with 0 it never executes,
    `goto` is never called, and the engine then classifies `about:blank` —
    **39 bytes** — as a refusal. The audit reported "OpenSea returned 39
    bytes with no vendor markers" and concluded availability was unproven.

    Reproduced and settled here with one variable changed:

        --retries 0   exit 3, 39 bytes, four attempts, 37 s
        --retries 1   exit 0, 1,395,922 bytes, 50 rows, 100% coverage, 11 s
        defaults, 3 pages   exit 0, 250 rows, complete, 14 s

    So the site was available throughout and the refusal was ours. Note the
    two tells, both cheap and both general: a "refusal" that returns in
    milliseconds is not a round trip, and 39 bytes is an empty DOM
    (`<html><head></head><body></body></html>`) rather than anything a
    server sent.

    Zero is refused rather than clamped. Clamping would make `--retries 0`
    mean `--retries 1`, which is a different lie: the flag would look
    honoured and not be.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            "--retries must be a whole number of attempts, got %r" % (value,))
    if number < 1:
        raise argparse.ArgumentTypeError(
            "--retries must be at least 1: it counts ATTEMPTS, not extra "
            "tries, so 0 would mean 'never fetch the page'. A run that never "
            "navigates reports the empty document as a block (exit 3), which "
            "is how a third-party audit came to report this site as "
            "unavailable while it was serving normally.")
    return number


def page_cap_reached(page_num: int) -> bool:
    return page_num >= PAGE_CAP


def cursor_exhausted(cursor: Optional[str]) -> bool:
    """Whether this cursor means the feed is finished.

    Two ways a feed ends, and they are different facts:

      * the site handed back NO cursor — there is nothing after the page just
        read, which is the ordinary end of a feed and the cleanest completion
        signal any repo in this family has;
      * the cursor's sort key is null, which happens only under the price
        ordering and only at the boundary between listed and unlisted items.
        Everything sorted after that point has no price, so a floor scrape
        is finished — see `price_boundary_reached`, whose docstring records
        why this stop no longer rests on the error it originally did.

    `price_boundary_reached` tells the two apart for the log and the sidecar.
    """
    return not cursor or cursor_key_is_null(cursor)


def price_boundary_reached(cursor: Optional[str]) -> bool:
    """Whether the walk has reached the end of the LISTED items.

    The cursor under the price ordering carries the last row's price, so a
    null key means that row had none — and everything sorted after it has
    none either. Stopping there is what a price-ordered walk is FOR: it is a
    floor scrape, and the rows beyond the boundary are the unpriced
    remainder of the collection in no particular order.

    THE REASON FOR THIS STOP CHANGED, and the first version of it has
    rotted, which is worth leaving written down.

    Measured 2026-09-17: passing such a cursor back answered
    `{"errors":[{"message":"Something went wrong"}]}`, so this was written as
    "recognise it before the request is spent", and the README, the
    CHANGELOG and the engines all said so.

    Re-measured 2026-10-13: the same cursor now paginates perfectly. A walk
    went **1,000 rows** deep with no error at all — and pages 4 to 10, seven
    hundred rows, carried **zero** priced items. OpenSea fixed their end.
    The error is gone; the fact it stood for is not.

    So the stop is unchanged and its justification is now the DATA rather
    than an error message: see `prices_ran_out`, which the engines check
    beside this. A terminating condition must be a property of the
    catalogue rather than of the markup, and "the endpoint rejects this
    cursor" was the markup.
    """
    return cursor_key_is_null(cursor)


def prices_ran_out(rows, sort: str) -> bool:
    """Whether this page proves the listed items are behind us.

    The data-side half of the stop above, and the half that survives the
    site changing its mind about the cursor. Under a price ordering a page
    with not one priced row means the boundary was crossed on the page
    before it, and the terminating condition should be exactly this
    kind of fact.

    Only meaningful under a price ordering: under `created` an unpriced page
    is the normal case and stopping on it would end every run at page 1.
    """
    if not rows or not str(sort).startswith("price"):
        return False
    return all(getattr(row, "price", None) is None for row in rows)


def sort_note(sort: str, rows: int, totals: Optional[dict] = None) -> str:
    """The sentence a price-ordered run prints when it stops at the boundary.

    Printed rather than swallowed, because "I asked for 20 pages and got 3"
    with no explanation is indistinguishable from the family's most expensive
    bug — a dead next-page selector quietly returning a third of the data
    with exit 0. Here the run really did get every item that has a
    price, and the difference has to be visible in the log and in the
    sidecar.
    """
    listed = (totals or {}).get("listed_items")
    supply = (totals or {}).get("total_supply")
    tail = ""
    if listed is not None:
        tail = (f" OpenSea's own count for this collection is {listed} listed "
                f"item(s)")
        if supply:
            tail += f" out of {supply}"
        tail += "."
    return (f"Stopped at the end of the LISTED items: under --sort {sort} the "
            f"cursor carries the last row's price, and it goes null where the "
            f"listed items end. The run holds {rows} row(s) and is COMPLETE "
            f"for the question --sort {sort} asks — every item that has a "
            f"price is in it.{tail} The feed itself does NOT end here: "
            f"measured 2026-10-13, it pages on for at least another 700 rows, "
            f"all of them unpriced. Use --sort created to walk the whole "
            f"collection in a defined order.")


def totals_note(totals: Optional[dict], rows: int) -> Optional[str]:
    """What share of the collection this run actually holds.

    "Complete" and "exhaustive" are different words, with the site doing
    the arithmetic: a three-page run of a 9,998-item collection is genuinely
    complete under its ordering AND is 3% of the collection. A sidecar that
    records only the first is lying by omission.
    """
    supply = (totals or {}).get("total_supply")
    if not supply or not rows:
        return None
    return (f"This run holds {rows} of the {supply} item(s) OpenSea says this "
            f"collection has ({100.0 * rows / supply:.1f}%). `complete` in "
            f"the sidecar means the walk finished, not that it read the whole "
            f"collection.")


def collection_totals_on_page(html: Optional[str]) -> dict:
    """The collection's own totals, where the page carries them."""
    return collection_totals(html)


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------
def page_gap(html: Optional[str], parsed: int) -> Optional[int]:
    """None, always, on this site — and that is the honest answer.

    Rank arithmetic works where a site NUMBERS its items: 30 rows
    spanning ranks 1-50 proves 20 cards never loaded. OpenSea numbers
    nothing. A token's rarity RANK exists but is a property of the token
    rather than of this page's ordering, and a ranking's position is one this
    scraper assigns from the row order rather than one the site publishes.

    None rather than 0, because an unknown gap is not a gap of zero and the
    two must not read the same in a sidecar.
    """
    return None


def degraded_to_dom(rows) -> bool:
    """Whether every row came from the DOM fallback.

    The fallback exists so a site change does not cost the run everything,
    and it emits an address and a name and NO prices (see
    `product_parser.rows_from_dom`). So a file built entirely out of it has
    the right columns, the right row count and no money in it — which is the
    failure this family keeps meeting: a tool doing less than it says
    while reporting success.

    Measured 2026-10-13, which is why this exists: opensea.io stopped
    inlining its state on two of three page kinds, both modes fell through
    to the fallback, and the runs reported `complete` with 23 and 24 rows
    against the 160 and 200 the endpoint served the same minute. Every row
    said `data_source: dom`. The column was right and nothing read it.

    Rows with no `data_source` at all do not count as degraded — that is a
    different fault and `parsed_nothing_from_a_served_page` is its check.
    """
    sources = {getattr(row, "data_source", None) for row in rows}
    return bool(rows) and sources == {"dom"}


DEGRADED_STOP_REASON = "degraded_to_dom"


def parsed_nothing_from_a_served_page(html: Optional[str], rows: int) -> bool:
    """A page that was SERVED, links to items, and produced no rows.

    That is this parser's bug and not an empty collection, and the two must
    not report the same. The engines give it its own stop reason so a
    reader opens `product_parser.py` instead of checking their URL for a
    typo.

    This CAN fire here, which is what makes it worth having: the DOM fallback
    emits a row for every distinct item link it finds, so reaching zero rows
    on a page that carries links means the state did not parse AND the
    anchors did not match — both halves of the parser, at once.
    """
    if rows or not html:
        return False
    return served_by_opensea(html) and count_cards(html) >= MIN_CARD_MATCHES


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def concurrency_limit(url: str = "", mode: str = "") -> Optional[int]:
    """1, for every mode. See `concurrency_refusal`."""
    return 1


def concurrency_refusal(url: str = "", mode: str = "") -> Optional[str]:
    """Why concurrency above 1 is refused for this run.

    Refused WITH the reason rather than silently running one worker, which
    would look like the flag did something.

    This is the one place OpenSea is strictly worse than a site with page
    numbers: the family's `--concurrency` exists because `?page=5` can be
    built without fetching page 4, and here it cannot. A run that wants more
    throughput runs more COLLECTIONS at once, one process each, which is what
    the README says.
    """
    return (f"{PAGE_URL_REASON}, so {CONCURRENCY_REASON}. Run several "
            f"collections in parallel instead, one process each — that is "
            f"the same parallelism against independent feeds")
