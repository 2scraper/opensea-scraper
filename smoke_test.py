#!/usr/bin/env python3
"""opensea-scraper — offline smoke tests.

One file of plain functions with fixtures loaded from
`fixtures_generated.json`, no pytest required. `tests/test_smoke.py` wraps it
as a single pytest test so `pytest` works as an entry point without a second
copy of the checks.

    python3 smoke_test.py

It MUST pass with no engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is REPORTED, because "skipped, engine absent" reads
exactly like a passing run. CI's engine-smoke job installs each engine in its
own venv and fails if that skip list is non-empty.

The fixtures are cut from real captures by `make_fixtures.py`, which proves
each one parses IDENTICALLY to its untrimmed original, column for column,
classifies the same way, and replaces every real profile handle with a
placeholder. Do not hand-edit them.

WHAT THIS SUITE IS FOR, beyond the obvious
------------------------------------------
Most of these checks exist because of a specific failure, in this repo or in
a sibling. The ones worth knowing about before you change anything:

  * `test_values_on_real_fixtures` asserts VALUES, not coverage. A column can
    be 100% populated and entirely wrong. Two here would have been: a Solana
    item's `tokenId` REPEATS its contract address, so writing it through
    produced `/item/solana/{mint}/{mint}` — an address the site does not
    serve — on every row of every Solana collection, at 100% coverage; and
    `lastSale.token.symbol` is the empty string on 11 of 50 rows of the Bored
    Ape capture, which written through reads as a currency.

  * `test_markers_do_not_match_a_good_page` is the §18 rule as a test. Every
    marker in every set is asserted ABSENT from six pages known to be good.
    It is also what pins the decision NOT to treat OpenSea's own "404: This
    page could not be found." as an empty-state marker: Next.js ships that
    sentence inside every RSC payload, twice, on every page the site serves.

  * `test_engine_parity` binds every shared-module call in every engine
    against the callee's REAL signature. Two engines in a sibling repo called
    `classify(html, url=...)` where the parameter is positional, both crashed
    on their first fetch, and nothing short of a live run saw it.

  * `test_ssr_request_match` pins the trap that cost this repo a working run:
    page 1's rows are the ones the SITE chose to render, under the site's own
    ordering and filter. A `--sort created` run that keeps them is two
    samples in one file, and the cursor page 1 hands over is not even valid
    for the other ordering — the endpoint answers `Invalid cursor`.
"""

import ast
import contextlib
import csv as csv_module
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import fields as dataclass_fields

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)

import product_parser as P          # noqa: E402
import page_flow                    # noqa: E402
import output_writer                # noqa: E402
import env_config                   # noqa: E402
import proxy_pool                   # noqa: E402

FIXTURES_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")

# The floor this suite claims. A check that is deleted rather than fixed is
# the failure mode this guards: the number goes down and the run goes red.
CLAIMED_CHECK_FLOOR = 300

_total_checks = 0
_failures = []


def check(condition, message):
    global _total_checks
    _total_checks += 1
    if not condition:
        _failures.append(message)
        print("  FAIL  %s" % message)
        return False
    return True


def group(name):
    print("\n[%s]" % name)


def load_fixtures():
    if not os.path.exists(FIXTURES_PATH):
        raise SystemExit(
            "fixtures_generated.json is missing. Regenerate it with\n"
            "    python3 make_fixtures.py\n"
            "which needs your own captures in ../captures/ — see that file's "
            "docstring.")
    with open(FIXTURES_PATH, encoding="utf-8") as f:
        return json.load(f)


FIXTURES = load_fixtures()
GOOD_PAGES = ("collection", "collection_solana", "collection_ja", "ranking",
              "activity", "item", "cdp_scraping_browser")


def fixture(name):
    return FIXTURES[name]


def html_of(name):
    return FIXTURES[name]["html"]


# ===========================================================================
# The fixtures themselves
# ===========================================================================
def test_fixtures_are_present():
    group("fixtures")
    ok = True
    expected = ("collection", "collection_solana", "collection_ja", "ranking",
                "activity", "item", "notfound", "chromium_proxy_error")
    for name in expected:
        ok &= check(name in FIXTURES, "fixture %r is missing" % name)
    for name, data in FIXTURES.items():
        ok &= check(bool(data.get("html")), "fixture %r has no html" % name)
        ok &= check(bool(data.get("url")), "fixture %r has no url" % name)
        ok &= check(data.get("mode") in P.MODES,
                    "fixture %r has mode %r, which is not one of %s"
                    % (name, data.get("mode"), P.MODES))
        ok &= check("expect" in data,
                    "fixture %r carries no expectations — regenerate it"
                    % name)
    # A fixture nobody can regenerate is a fixture nobody can trust.
    ok &= check(os.path.exists(os.path.join(REPO_ROOT, "make_fixtures.py")),
                "make_fixtures.py is missing, so the fixtures cannot be "
                "regenerated from a capture")
    return ok


def test_values_on_real_fixtures():
    """Assert VALUES on real fixtures, not coverage (§10).

    Every expectation here was COMPUTED from the untrimmed capture by
    make_fixtures.py rather than typed by hand: a hand-typed expectation is a
    guess about the site, and this file is supposed to be a record of it.
    """
    group("values on real fixtures")
    ok = True
    for name, data in FIXTURES.items():
        expect = data["expect"]
        rows = P.parse_rows(data["html"], data["url"], page=1,
                            mode=data["mode"])
        ok &= check(len(rows) == expect["rows"],
                    "%s: parsed %d row(s), expected %d"
                    % (name, len(rows), expect["rows"]))
        ok &= check([r.sku for r in rows] == expect["skus"],
                    "%s: the skus changed" % name)
        if not rows:
            continue
        first = {k: v for k, v in rows[0].__dict__.items() if k != "scraped_at"}
        for key, value in expect["first_row"].items():
            ok &= check(first.get(key) == value,
                        "%s: first row's %s is %r, expected %r"
                        % (name, key, first.get(key), value))
        ok &= check(sum(1 for r in rows if r.price is not None)
                    == expect["with_price"],
                    "%s: the number of rows carrying a price changed" % name)
    return ok


def test_row_shape_per_mode():
    """The mode decides the row CLASS here, and the three share a prefix."""
    group("row shape")
    ok = True
    prefix = ("source", "scraped_at", "url", "sku", "title")
    for cls in (output_writer.Item, output_writer.Collection,
                output_writer.Activity):
        names = [f.name for f in dataclass_fields(cls)]
        ok &= check(tuple(names[:len(prefix)]) == prefix,
                    "%s does not open with the family prefix %s — it opens "
                    "with %s" % (cls.__name__, prefix, tuple(names[:5])))
        for tail in ("data_source", "page", "position"):
            ok &= check(tail in names,
                        "%s has no %s column" % (cls.__name__, tail))
    for mode, cls in output_writer.ROW_CLASS_BY_MODE.items():
        rows = P.parse_rows(html_of(_fixture_for_mode(mode)),
                            fixture(_fixture_for_mode(mode))["url"],
                            mode=mode)
        ok &= check(all(isinstance(r, cls) for r in rows),
                    "--mode %s did not yield %s rows" % (mode, cls.__name__))
    ok &= check(set(output_writer.ROW_CLASS_BY_MODE) == set(P.MODES),
                "ROW_CLASS_BY_MODE and product_parser.MODES disagree")
    ok &= check(output_writer.Product is output_writer.Item,
                "the family's `Product` alias no longer points at this "
                "repo's default row class")
    return ok


def _fixture_for_mode(mode):
    return {"items": "collection", "collections": "ranking",
            "activity": "activity"}[mode]


def test_solana_addresses():
    """A Solana item has no token id, and its address has two segments.

    The trap this pins: the payload sets `tokenId` to the MINT ADDRESS, the
    same string as `contractAddress`. Written through it produces
    `/item/solana/{mint}/{mint}` — an address the site does not serve — and a
    `sku` claiming a token id that does not exist, on every row, at 100%
    coverage.
    """
    group("solana addresses")
    ok = True
    rows = P.parse_rows(html_of("collection_solana"),
                        fixture("collection_solana")["url"], mode="items")
    ok &= check(bool(rows), "the Solana fixture parsed to nothing")
    for row in rows:
        ok &= check(row.chain == "solana",
                    "expected a solana row, got chain=%r" % row.chain)
        ok &= check(row.token_id is None,
                    "a Solana row carries token_id=%r; the mint IS the token "
                    "and the column must be null" % row.token_id)
        ok &= check(row.url.count("/") == 5,
                    "a Solana item URL should have two segments after "
                    "/item/, got %r" % row.url)
        ok &= check(row.sku == "solana/%s" % row.contract_address,
                    "a Solana sku should be chain/mint, got %r" % row.sku)
    evm = P.parse_rows(html_of("collection"), fixture("collection")["url"],
                       mode="items")
    for row in evm:
        ok &= check(row.token_id is not None,
                    "an Ethereum row must carry a token id")
        ok &= check(row.sku == "ethereum/%s/%s" % (row.contract_address,
                                                   row.token_id),
                    "an EVM sku should be chain/contract/token, got %r"
                    % row.sku)
    return ok


def test_empty_symbol_is_null():
    """An empty currency symbol becomes None, never an empty string.

    Measured on the Bored Ape capture: 11 of 50 last sales carried
    `"symbol": ""` with a real contract address behind them — OpenSea did not
    name the token. An empty string written into a currency column reads as a
    currency.
    """
    group("empty symbols")
    ok = True
    for name in ("collection", "collection_solana", "activity"):
        rows = P.parse_rows(html_of(name), fixture(name)["url"],
                            mode=fixture(name)["mode"])
        for row in rows:
            for column in ("currency", "best_offer_currency",
                           "last_sale_currency"):
                value = getattr(row, column, None)
                ok &= check(value != "",
                            "%s: %s is an empty string on %s"
                            % (name, column, row.sku))
    # And the unit test underneath it, so the rule is pinned rather than
    # depending on a capture happening to carry one.
    amount, currency, usd = P._price(
        {"token": {"unit": 1.5, "symbol": ""}, "usd": 3000.0})
    ok &= check(currency is None,
                "an empty symbol should read as None, got %r" % currency)
    ok &= check(amount == 1.5 and usd == 3000.0,
                "the amount and the usd figure must survive a null symbol")
    return ok


def test_prices_are_tokens_not_dollars():
    group("prices")
    ok = True
    rows = P.parse_rows(html_of("collection"), fixture("collection")["url"],
                        mode="items")
    priced = [r for r in rows if r.price is not None]
    ok &= check(bool(priced), "no row on the collection fixture has a price")
    for row in priced:
        ok &= check(row.currency is not None,
                    "%s has a price and no currency" % row.sku)
        ok &= check(row.currency == row.currency.upper(),
                    "a token symbol should be as the site writes it, got %r"
                    % row.currency)
        ok &= check(row.price_usd is None or row.price_usd > row.price,
                    "%s: price_usd (%r) should be the dollar figure and "
                    "price (%r) the token amount — they look swapped"
                    % (row.sku, row.price_usd, row.price))
    # No defaulted currency anywhere (§4's last rung: absent is null).
    for row in rows:
        if row.price is None:
            ok &= check(row.currency is None,
                        "%s has no price and a currency of %r"
                        % (row.sku, row.currency))
    return ok


def test_traits_and_rarity():
    group("traits and rarity")
    ok = True
    rows = P.parse_rows(html_of("collection"), fixture("collection")["url"],
                        mode="items")
    with_traits = [r for r in rows if r.traits]
    ok &= check(bool(with_traits), "no row carries traits")
    for row in with_traits:
        for trait in row.traits:
            ok &= check(": " in trait,
                        "a trait should read 'Type: Value', got %r" % trait)
    ranked = [r for r in rows if r.rarity_rank is not None]
    for row in ranked:
        ok &= check(isinstance(row.rarity_rank, int) and row.rarity_rank > 0,
                    "a rarity rank should be a positive integer, got %r"
                    % row.rarity_rank)
    return ok


def test_activity_rows():
    group("activity rows")
    ok = True
    rows = P.parse_rows(html_of("activity"), fixture("activity")["url"],
                        mode="activity")
    ok &= check(bool(rows), "the activity fixture parsed to nothing")
    for row in rows:
        ok &= check(row.event_type is not None,
                    "an activity row with no event type: %r" % row.sku)
        ok &= check(row.event_type == row.event_type.upper(),
                    "an event type should be the site's own upper-case name, "
                    "got %r" % row.event_type)
        ok &= check(row.event_time is not None,
                    "an activity row with no event time")
        ok &= check(row.url.startswith("https://opensea.io/item/"),
                    "an activity row should point at the ITEM, got %r"
                    % row.url)
    # The dedupe key is the EVENT, not the token: a feed shows one token
    # being listed, offered and sold, and keying on the token would drop two
    # of the three.
    ok &= check(len({r.sku for r in rows}) == len(rows),
                "two activity rows share an id")
    # `__typename` is the only type an SSR row carries — a queried one has
    # `type`. Reading only the second leaves the column null on every SSR row.
    from_typename = P.activity_row({"id": "x", "__typename": "TraitOffer"})
    ok &= check(from_typename.event_type == "TRAIT_OFFER",
                "an SSR activity row's __typename should map to an event "
                "type, got %r" % from_typename.event_type)
    return ok


def test_collection_rows():
    group("collection rows")
    ok = True
    rows = P.parse_rows(html_of("ranking"), fixture("ranking")["url"],
                        mode="collections", first_rank=1)
    ok &= check(bool(rows), "the ranking fixture parsed to nothing")
    for index, row in enumerate(rows):
        ok &= check(row.sku is not None, "a ranking row with no slug")
        ok &= check(row.url == "https://opensea.io/collection/%s" % row.sku,
                    "a ranking row's url should be its collection page, got "
                    "%r" % row.url)
        ok &= check(row.rank == index + 1,
                    "rank should follow the ranking's own order: row %d has "
                    "rank %r" % (index, row.rank))
        ok &= check(row.volume_window is not None,
                    "a ranking row must record WHICH window its volume "
                    "covers — a one-hour figure and an all-time one are both "
                    "correct and not comparable")
    return ok


def test_collection_totals():
    """§21's arithmetic: complete and exhaustive are different words."""
    group("collection totals")
    ok = True
    totals = P.collection_totals(html_of("collection"))
    ok &= check(totals.get("total_supply"),
                "the collection fixture carries no total supply")
    ok &= check(totals.get("listed_items") is not None,
                "the collection fixture carries no listed-item count")
    ok &= check(totals["listed_items"] < totals["total_supply"],
                "listed items should be a subset of the supply")
    # The ranking and an item page carry no such total, and {} is the right
    # answer rather than a failed read.
    ok &= check(P.collection_totals(html_of("ranking")) == {},
                "the ranking should carry no per-collection totals")
    note = page_flow.totals_note(totals, 350)
    ok &= check(note and "%" in note,
                "totals_note should state the share this run holds")
    return ok


# ===========================================================================
# Markers, and the ones deliberately absent
# ===========================================================================
def test_markers_do_not_match_a_good_page():
    """§18: count every marker on a page you know is good, FIRST.

    A marker that matches every page is worse than no marker: a sibling repo
    reported "Blocked by akamai" and exit 3 on a 191 KB page the site had
    plainly served, because the site's own performance script references an
    Akamai host on every page.
    """
    group("markers vs good pages")
    ok = True
    for name in GOOD_PAGES + ("notfound",):
        html = html_of(name)
        for marker in P.BOT_CHALLENGE_MARKERS:
            ok &= check(marker not in html,
                        "%s: the challenge marker %r appears on a page the "
                        "site served — it is a fact about the site, not a "
                        "marker" % (name, marker))
        for marker in P._BROWSER_ERROR_MARKERS:
            ok &= check(marker not in html,
                        "%s: the browser-error marker %r appears on a real "
                        "page" % (name, marker))
        for marker in P._EMPTY_STATE_MARKERS:
            ok &= check(marker not in html,
                        "%s: the empty-state marker %r appears on a page "
                        "that has rows on it" % (name, marker))
    return ok


def test_cf_turnstile_is_not_a_marker():
    """`cf-turnstile` must stay OUT of the set, and the reason is measured.

    2Captcha's own Scraping Browser auto-solve extension injects
    `data-ts-input="cf-turnstile-response"` into every page it loads, so the
    marker fires on GOOD pages fetched over --cdp-endpoint; two sibling repos
    measured it present on served pages and ABSENT from the real Cloudflare
    challenge. `challenges.cloudflare.com` is the one that works.
    """
    group("cf-turnstile")
    ok = True
    ok &= check("cf-turnstile" not in P.BOT_CHALLENGE_MARKERS,
                "`cf-turnstile` is back in BOT_CHALLENGE_MARKERS. It is "
                "measured useless: 2Captcha's auto-solve extension injects "
                "it into every page it loads, and it was absent from the real "
                "challenge in two sibling repos.")
    ok &= check("challenges.cloudflare.com" in P.BOT_CHALLENGE_MARKERS,
                "the Cloudflare marker that does work is missing")
    # And with it gone, nothing in the set matches anything that extension
    # injects — which is what makes an extension-tag strip dead code here.
    injected = ('<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenoo'
                'emhbpbo/content/captcha/turnstile/hunter.js" '
                'data-ts-input="cf-turnstile-response"></script>')
    ok &= check(P.detect_block_marker(injected) is None,
                "a page carrying only the auto-solve extension's own script "
                "is being read as a challenge")
    return ok


def test_extension_injection_does_not_read_as_a_challenge():
    """The §19 trap, pinned against the fixture that can actually spring it.

    2Captcha's Scraping Browser ships an auto-solve extension that injects
    its own Turnstile hunter into every page it loads. MEASURED on this site
    on 2026-09-17, on one collection page fetched two ways in the same hour:

        marker                      over --cdp-endpoint   local browser
        cf-turnstile                                  1               0
        cf-turnstile-response                         1               0
        data-ts-input                                 1               0
        hunter.js                                     4               0
        chrome-extension://…hbpbo                    16               0
        challenges.cloudflare.com                     0               0

    So `cf-turnstile` — the obvious marker for a Turnstile — fires on a
    perfectly good 1.26 MB page holding the full catalogue, and the marker
    that actually works is absent from it. Carrying the obvious one would
    report exit 3 on every page fetched over the paid path, which is exactly
    what happened to a sibling repo's first live run.

    THE FIXTURE IS THE POINT. A sibling repo shipped this check and it
    PASSED FOR THE WRONG REASON: it ran only against pages fetched with a
    plain client, which carry no injection at all (§21). This one runs
    against a real Scraping Browser capture that does.
    """
    group("scraping browser extension injection")
    ok = True
    html = html_of("cdp_scraping_browser")
    url = fixture("cdp_scraping_browser")["url"]

    # The fixture must still CARRY the injection, or this check is theatre.
    for marker in ("cf-turnstile", "data-ts-input", "hunter.js",
                   "chrome-extension://"):
        ok &= check(marker in html,
                    "the Scraping Browser fixture no longer carries %r, so "
                    "it can no longer spring the trap it exists for — "
                    "re-capture it over --cdp-endpoint" % marker)

    # And the parser must nonetheless call it content.
    ok &= check(P.detect_block_marker(html) is None,
                "a page the Scraping Browser SERVED is being read as a "
                "challenge because of its own auto-solve extension")
    ok &= check(P.detect_page_state(html, 200, url, "items") == "content",
                "the Scraping Browser fixture must classify as content")
    ok &= check(bool(P.parse_rows(html, url, mode="items")),
                "the Scraping Browser fixture must parse to rows")
    ok &= check("challenges.cloudflare.com" not in html,
                "the marker this repo DOES carry must be absent from a "
                "served page, or the measurement above has changed")
    return ok


def test_the_not_found_sentence_is_not_a_marker():
    """Pin the §18 trap this repo caught in the act.

    OpenSea's "not found" page reads `404: This page could not be found.`,
    which looks like the obvious marker for an unknown collection slug.
    Counted on pages known to be good first: it appears TWICE on every served
    page, because Next.js ships its bundled not-found component inside every
    RSC payload. Adding it would have classified the whole site as empty.
    """
    group("the not-found sentence")
    ok = True
    sentence = "This page could not be found"
    ok &= check(all(sentence not in m for m in P._EMPTY_STATE_MARKERS),
                "the Next.js not-found sentence is being used as an "
                "empty-state marker; it appears on every page the site "
                "serves")
    # The status is what settles it, and every engine has one.
    ok &= check(P.detect_page_state(html_of("notfound"), 404,
                                    fixture("notfound")["url"]) == "empty",
                "a real 404 should classify as empty")
    return ok


def test_classification():
    group("page states")
    ok = True
    for name in GOOD_PAGES:
        data = fixture(name)
        ok &= check(P.detect_page_state(data["html"], 200, data["url"],
                                        data["mode"]) == "content",
                    "%s should classify as content" % name)
    ok &= check(P.detect_page_state(html_of("chromium_proxy_error"), None,
                                    "https://opensea.io/collection/x")
                == "blocked",
                "Chromium's own error page should classify as blocked")
    ok &= check(P.detect_page_state(None) == "blocked",
                "no response at all should classify as blocked")
    ok &= check(P.detect_page_state("") == "blocked",
                "an empty body should classify as blocked")
    # The structural test, which is what reads the error page correctly: it
    # carries <title>opensea.io</title> and would fool any title check.
    ok &= check("<title>opensea.io</title>" in html_of("chromium_proxy_error"),
                "the proxy-error fixture no longer carries the site's own "
                "hostname in its title, which is the whole point of it")
    ok &= check(not P.served_by_opensea(html_of("chromium_proxy_error")),
                "the proxy-error page must not read as served by OpenSea")
    for name in GOOD_PAGES + ("notfound",):
        ok &= check(P.served_by_opensea(html_of(name)),
                    "%s should read as served by OpenSea" % name)
    # And the order of the signals: an unambiguous positive before a
    # threshold (§17's classification-order trap).
    challenge = ("<html><head><title>Just a moment...</title></head><body>"
                 "<script src='https://challenges.cloudflare.com/turnstile/"
                 "v0/api.js'></script></body></html>")
    ok &= check(P.detect_page_state(challenge, 403) == "challenge",
                "a Cloudflare challenge should classify as challenge, not "
                "blocked")
    return ok


def test_state_policy_is_complete():
    group("state policy")
    ok = True
    ok &= check(set(page_flow.STATE_POLICY) == set(P.PAGE_STATES),
                "STATE_POLICY and PAGE_STATES disagree")
    for state in P.PAGE_STATES:
        policy = page_flow.STATE_POLICY[state]
        ok &= check(set(policy) == {"parse", "retry", "solve", "blocked"},
                    "the policy for %r is missing a key" % state)
    ok &= check(page_flow.should_parse("content"), "content must be parsed")
    ok &= check(not page_flow.should_retry("empty"),
                "an empty feed is a correct answer and must not be retried")
    ok &= check(not page_flow.counts_as_blocked("empty"),
                "empty is exit 4, not exit 3 (§8)")
    ok &= check(page_flow.should_solve("challenge"),
                "a challenge must reach the solver — and note that saying it "
                "cannot be solved is the one sentence §19 forbids")
    ok &= check(page_flow.counts_as_blocked("blocked"),
                "blocked must count as blocked")
    # A state with no policy falls back to blocked, which is the safe
    # direction and also the silent one.
    ok &= check(page_flow.counts_as_blocked("a-state-that-does-not-exist"),
                "an unknown state should fall back to blocked")
    return ok


# ===========================================================================
# Cursors — the pagination model, and the boundary that ends a walk
# ===========================================================================
def test_cursors():
    group("cursors")
    ok = True
    import base64

    def cursor(value):
        return base64.b64encode(json.dumps(value).encode()).decode()

    price_cursor = cursor([6.89, "35f3f2a7-7c78-37eb-8a12-0af57eba4eb1"])
    null_cursor = cursor([None, "005db456-c8f0-3fcd-b6bc-263f9edafbb3"])
    offset_cursor = cursor([50])
    date_cursor = cursor(["2021-04-24T10:21:17Z", 99, "99b822d6"])

    ok &= check(not P.cursor_key_is_null(price_cursor),
                "a price cursor is not the end of the listed items")
    ok &= check(P.cursor_key_is_null(null_cursor),
                "a cursor whose sort key is null IS the end of the listed "
                "items — spending it answers `Something went wrong`")
    ok &= check(not P.cursor_key_is_null(offset_cursor),
                "an offset cursor is not a null key")
    ok &= check(not P.cursor_key_is_null(date_cursor),
                "a date cursor is not a null key")
    ok &= check(not P.cursor_key_is_null(None),
                "no cursor at all is the end of the FEED, which is a "
                "different fact — see page_flow.cursor_exhausted")
    ok &= check(not P.cursor_key_is_null("not base64 at all"),
                "an unreadable cursor is not a null one")

    ok &= check(page_flow.cursor_exhausted(None),
                "no cursor means the feed ended")
    ok &= check(page_flow.cursor_exhausted(null_cursor),
                "a null-key cursor ends the walk")
    ok &= check(not page_flow.cursor_exhausted(price_cursor),
                "a live cursor does not end the walk")
    ok &= check(page_flow.price_boundary_reached(null_cursor)
                and not page_flow.price_boundary_reached(price_cursor),
                "price_boundary_reached should tell the two endings apart")

    # The stop reason it produces has to count as COMPLETE, or every correct
    # floor scrape would report partial.
    ok &= check("listed_items_exhausted" in output_writer.COMPLETE_STOP_REASONS,
                "a price-ordered walk that reaches the end of the listed "
                "items is COMPLETE — it holds every item that has a price")
    ok &= check("cursor_exhausted" in output_writer.COMPLETE_STOP_REASONS,
                "a feed the site says has ended is COMPLETE")
    note = page_flow.sort_note("price", 283, {"listed_items": 283,
                                              "total_supply": 9998})
    ok &= check("283" in note and "9998" in note and "--sort created" in note,
                "the stop note must give the site's own numbers and the way "
                "to walk the whole collection")
    return ok


def test_page_url_is_none():
    """§7's layer 2 does not exist here, and saying so is the point."""
    group("page addresses")
    ok = True
    ok &= check(P.page_url("https://opensea.io/collection/x", 2) is None,
                "page_url must return None: OpenSea has no address for page "
                "2, and a made-up ?page=N would be answered with page 1")
    ok &= check(P.paginates_by_url() is False,
                "paginates_by_url must be False")
    refusal = page_flow.concurrency_refusal(
        "https://opensea.io/collection/x", "items")
    ok &= check(refusal and "cursor" in refusal,
                "concurrency must be refused WITH the reason (§18)")
    ok &= check(page_flow.concurrency_limit("", "items") == 1,
                "every mode here is single-worker")
    return ok


def test_ssr_request_match():
    """Page 1's rows answer the SITE's question, not always the run's.

    The trap that cost a working run: a `--sort created` run kept page 1's
    price-ordered rows and then passed page 1's PRICE cursor to a
    created-ordered query, which the endpoint answered `Invalid cursor`.
    """
    group("ssr matches the request")
    ok = True
    ok &= check(P.ssr_matches_request("items", sort="price"),
                "the site's own item ordering is price, so a price run may "
                "keep page 1's rows")
    ok &= check(not P.ssr_matches_request("items", sort="created"),
                "a created-ordered run must NOT keep page 1's price-ordered "
                "rows")
    ok &= check(not P.ssr_matches_request("items", sort="price-desc"),
                "descending is a different sample from ascending")
    ok &= check(P.ssr_matches_request("collections", ranking="TRENDING",
                                      timeframe="1d"),
                "the ranking page renders TRENDING over one day")
    ok &= check(not P.ssr_matches_request("collections", ranking="TOP",
                                          timeframe="1d"),
                "TOP is a different ranking from TRENDING")
    ok &= check(not P.ssr_matches_request("collections", ranking="TRENDING",
                                          timeframe="7d"),
                "a different timeframe is a different ranking")
    ok &= check(P.ssr_matches_request("activity", activity_filter="sales"),
                "the activity page renders sales")
    ok &= check(not P.ssr_matches_request("activity", activity_filter="all"),
                "the activity page is filtered to sales, so `all` is a "
                "different question")
    note = P.ssr_mismatch_note("items", sort="created")
    ok &= check("Invalid cursor" in note,
                "the mismatch note should name the error the other path "
                "produces")
    return ok


# ===========================================================================
# URLs
# ===========================================================================
def test_url_knowledge():
    group("urls")
    ok = True
    cases = {
        "https://opensea.io/collection/boredapeyachtclub": "collection",
        "https://opensea.io/collection/boredapeyachtclub/activity": "activity",
        "https://opensea.io/ja/collection/boredapeyachtclub": "collection",
        "https://opensea.io/collections": "collections",
        "https://opensea.io/rankings": "collections",
        "https://opensea.io/item/ethereum/0xabc/1": "item",
        "https://opensea.io/item/solana/3bF2maQ": "item",
        "https://opensea.io/tokens": "other",
        "https://opensea.io/": "other",
    }
    for url, expected in cases.items():
        ok &= check(P.page_kind(url) == expected,
                    "page_kind(%r) is %r, expected %r"
                    % (url, P.page_kind(url), expected))
    ok &= check(P.mode_for_url("https://opensea.io/collection/x") == "items",
                "a collection page reads as --mode items")
    ok &= check(P.mode_for_url(
        "https://opensea.io/collection/x/activity") == "activity",
        "an activity page reads as --mode activity")
    ok &= check(P.mode_for_url("https://opensea.io/collections")
                == "collections", "the ranking reads as --mode collections")

    ok &= check(P.locale_of("https://opensea.io/ja/collection/x") == "ja",
                "a locale prefix should be recognised")
    ok &= check(P.locale_of("https://opensea.io/collection/x") is None,
                "the default English path has no locale prefix")
    ok &= check(P.collection_slug_from_url(
        "https://opensea.io/ja/collection/bayc/activity") == "bayc",
        "the slug should be readable through a locale prefix")

    ok &= check(P.normalize_url("https://opensea.io/collection/x/")
                == "https://opensea.io/collection/x",
                "a trailing slash should be dropped so one page has one "
                "address")
    ok &= check(P.strip_tracking(
        "https://opensea.io/collection/x?ref=abc&utm_source=t")
        == "https://opensea.io/collection/x",
        "the site's own click parameters should be stripped")

    ok &= check(P.unsupported_reason(
        "https://opensea.io/collection/x") is None,
        "a collection URL must be accepted")
    for url, expect_in in (
            ("https://example.com/collection/x", "not OpenSea"),
            ("https://opensea.io/tokens", "does not read"),
            ("not-a-url", "http(s)")):
        why = P.unsupported_reason(url)
        ok &= check(why is not None and expect_in in why,
                    "unsupported_reason(%r) should say %r, said %r"
                    % (url, expect_in, why))

    ok &= check(P.item_url("ethereum", "0xabc", "7")
                == "https://opensea.io/item/ethereum/0xabc/7",
                "an EVM item URL has three segments")
    ok &= check(P.item_url("solana", "mint", None)
                == "https://opensea.io/item/solana/mint",
                "a Solana item URL has two")
    ok &= check(P.url_for_mode("activity", "bayc")
                == "https://opensea.io/collection/bayc/activity",
                "url_for_mode should build the activity feed")

    # A redirect that changes the collection is the one that matters.
    ok &= check(P.redirected_away("https://opensea.io/collection/a",
                                  "https://opensea.io/collection/b"),
                "a different collection slug must be reported")
    ok &= check(P.redirected_away("https://opensea.io/collection/a",
                                  "https://opensea.io/collection/a") is None,
                "the same address is not a redirect")
    return ok


# ===========================================================================
# The GraphQL route
# ===========================================================================
def test_graphql_bodies():
    group("graphql bodies")
    ok = True
    body = P.graphql_body("items", slug="bayc", limit=100, sort="price")
    ok &= check(body["variables"]["sort"] == {"by": "PRICE",
                                              "direction": "ASC"},
                "the price sort should map to PRICE/ASC")
    ok &= check("after" not in body["variables"],
                "page 1 of a feed sends no cursor")
    body = P.graphql_body("items", slug="bayc", after="abc", sort="created")
    ok &= check(body["variables"]["after"] == "abc",
                "a cursor should be sent as `after`")
    ok &= check(body["variables"]["sort"]["by"] == "CREATED_DATE",
                "the created sort should map to CREATED_DATE")

    over = P.graphql_body("items", slug="bayc", limit=500)
    ok &= check(over["variables"]["limit"] == P.MAX_LIMIT,
                "a limit above the endpoint's own maximum should be clamped "
                "rather than sent and refused")

    ranking = P.graphql_body("collections", ranking="TOP", timeframe="7d")
    ok &= check(ranking["variables"]["slug"] == "TOP",
                "the ranking slug should be sent as `slug`")
    ok &= check(ranking["variables"]["timeframe"] == "SEVEN_DAYS",
                "7d should map to SEVEN_DAYS")

    activity = P.graphql_body("activity", slug="bayc", activity_filter="sales")
    ok &= check(activity["variables"]["filter"] == {"activityTypes": ["SALE"]},
                "the sales filter should ask for SALE")
    every = P.graphql_body("activity", slug="bayc", activity_filter="all")
    ok &= check(every["variables"]["filter"] is None,
                "`all` means no filter at all, not a filter listing "
                "everything")

    for mode in P.MODES:
        query = P.QUERIES[mode]
        ok &= check("nextPageCursor" in query,
                    "the %s query must ask for the cursor, or the run cannot "
                    "paginate" % mode)
        ok &= check(query.count("{") == query.count("}"),
                    "the %s query has unbalanced braces" % mode)
    return ok


def test_graphql_errors_are_loud():
    """The endpoint answers 200 with an `errors` array. Fail loudly (§8)."""
    group("graphql errors")
    ok = True
    payload = {"errors": [{"message": "Something went wrong"}], "data": None}
    ok &= check(P.graphql_errors(payload) == ["Something went wrong"],
                "an errors array must be readable")
    ok &= check(P.rows_from_graphql(payload, mode="items") == [],
                "an error response yields no rows")
    ok &= check(P.graphql_errors({"data": {"collectionItems": {}}}) == [],
                "a good response has no errors")
    return ok


def test_graphql_and_state_agree():
    """One normaliser, two routes — the claim this repo is built on (§21).

    Feeds the SAME objects through both entry points and asserts the rows are
    identical. The live version of this check measured 49 of 50 rows
    identical across 18 stable columns against the real endpoint; the one
    difference was a listing whose price had changed between the capture and
    the query, which is the site moving rather than the parser disagreeing.
    """
    group("one parser, two routes")
    ok = True
    for name in ("collection", "collection_solana", "ranking", "activity"):
        data = fixture(name)
        mode = data["mode"]
        field, value = P.ssr_field(data["html"], mode)
        ok &= check(field is not None, "%s: no inlined operation found" % name)
        if not isinstance(value, dict):
            continue
        payload = {"data": {field: value}}
        from_state = P.parse_rows(data["html"], data["url"], mode=mode,
                                  first_rank=1)
        from_graphql = P.rows_from_graphql(payload, data["url"], mode=mode,
                                           first_rank=1)
        ok &= check(len(from_state) == len(from_graphql),
                    "%s: the two routes produced different row counts" % name)
        for left, right in zip(from_state, from_graphql):
            a = {k: v for k, v in left.__dict__.items()
                 if k not in ("scraped_at", "data_source")}
            b = {k: v for k, v in right.__dict__.items()
                 if k not in ("scraped_at", "data_source")}
            ok &= check(a == b,
                        "%s: the two routes disagree on %s — %s" % (
                            name, left.sku,
                            sorted(k for k in a if a[k] != b[k])))
        ok &= check(all(r.data_source == "ssr" for r in from_state),
                    "%s: rows read from the page should say so" % name)
        ok &= check(all(r.data_source == "graphql" for r in from_graphql),
                    "%s: rows read from the endpoint should say so" % name)
    return ok


def test_dom_fallback():
    """The fallback emits what the URL proves and leaves prices null."""
    group("dom fallback")
    ok = True
    html = html_of("collection")
    # Remove the inlined state, leaving the rendered anchors.
    stripped = re.sub(r'<script>\(window\[Symbol\.for\("urql_transport"\)\].*?'
                      r'</script>', "", html, flags=re.S)
    ok &= check(not P.payload_answered(stripped, "items"),
                "the state should be gone from the stripped fixture")
    rows = P.parse_rows(stripped, fixture("collection")["url"], mode="items")
    ok &= check(bool(rows),
                "the DOM fallback found nothing on a page full of item links")
    for row in rows:
        ok &= check(row.data_source == "dom",
                    "a fallback row must say it came from the DOM")
        ok &= check(row.sku is not None,
                    "a fallback row should still carry an id from its URL")
        ok &= check(row.price is None,
                    "a fallback row must not invent a price — the rendered "
                    "markup carries only formatted display text")
    # And the §20 signal: a served page that links to items and parses to
    # nothing is THIS PARSER'S bug, not an empty collection.
    ok &= check(page_flow.parsed_nothing_from_a_served_page(html, 0),
                "a served page with links and no rows must be reported as a "
                "parser regression")
    ok &= check(not page_flow.parsed_nothing_from_a_served_page(html, 5),
                "a page that produced rows is not a parser regression")
    return ok


# ===========================================================================
# The output contract
# ===========================================================================
def test_output_contract():
    group("output contract")
    ok = True
    ok &= check((output_writer.EXIT_NO_PRODUCTS, output_writer.EXIT_BLOCKED,
                 output_writer.EXIT_API_ERROR, output_writer.EXIT_PARTIAL)
                == (4, 3, 5, 6),
                "the family's exit codes changed")

    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        # A run that finds nothing writes NOTHING, so last night's good
        # output survives (§8).
        with open(prefix + ".json", "w") as f:
            f.write('[{"sku": "yesterday"}]')
        rc = output_writer.save([], prefix, "both")
        ok &= check(rc == output_writer.EXIT_NO_PRODUCTS,
                    "an empty result must exit 4")
        with open(prefix + ".json") as f:
            ok &= check("yesterday" in f.read(),
                        "an empty run overwrote a previous good result")

        # --allow-empty is the opt-out, and an empty CSV still has a header.
        rc = output_writer.save([], prefix, "both", allow_empty=True,
                                row_cls=output_writer.Activity)
        with open(prefix + ".csv") as f:
            header = f.readline().strip().split(",")
        ok &= check(header[0] == "source" and "event_type" in header,
                    "an empty CSV must carry the header of the MODE that "
                    "produced it, got %r" % header[:4])

        # A failed run writes no sidecar beside the previous good output.
        rc = output_writer.finish_run(
            [], prefix, "both", False, blocked=True,
            stop_reason="blocked_cloudflare", pages_requested=3,
            pages_completed=0, start_url="u", final_url="u", mode="items")
        ok &= check(rc == output_writer.EXIT_BLOCKED,
                    "a blocked run must exit 3")
        ok &= check(not os.path.exists(prefix + ".meta.json"),
                    "a failed run wrote a sidecar next to output it did not "
                    "write — the two would contradict each other")

        rows = P.parse_rows(html_of("collection"),
                            fixture("collection")["url"], mode="items")
        rc = output_writer.finish_run(
            rows, prefix, "both", False, blocked=False,
            stop_reason="cursor_exhausted", pages_requested=1,
            pages_completed=1, start_url="u", final_url="u", mode="items",
            extra={"sort": "price"})
        ok &= check(rc == 0, "a good run must exit 0")
        with open(prefix + ".meta.json") as f:
            meta = json.load(f)
        ok &= check(meta["status"] == "complete",
                    "a finished walk is complete")
        ok &= check(meta["mode"] == "items" and meta["sort"] == "price",
                    "the sidecar must record the mode AND the ordering — two "
                    "runs under different sorts are different samples")
        ok &= check(meta["source"] == "opensea.io",
                    "the sidecar's source should be this site")

        partial = output_writer.finish_run(
            rows, prefix, "both", False, blocked=False,
            stop_reason="endpoint_error", pages_requested=5,
            pages_completed=2, start_url="u", final_url="u", mode="items")
        ok &= check(partial == output_writer.EXIT_PARTIAL,
                    "a run that stopped early must exit 6")
    return ok


def test_page_and_position_are_unique():
    """§18's arithmetic bug, pinned: `position` restarts on every page."""
    group("page and position")
    ok = True
    groups = []
    for page_num in (1, 2, 3):
        rows = P.parse_rows(html_of("collection"),
                            fixture("collection")["url"], page=page_num,
                            mode="items")
        # Give each page distinct skus so the merge keeps them all.
        for index, row in enumerate(rows):
            row.sku = "%s#p%d" % (row.sku, page_num)
        groups.append((page_num, rows))
    merged, _, _ = output_writer.merge_pages(groups)
    pairs = {(r.page, r.position) for r in merged}
    ok &= check(len(pairs) == len(merged),
                "page+position is not unique across a multi-page run — the "
                "position column is worthless without the page beside it")
    ok &= check(all(r.page is not None and r.position is not None
                    for r in merged),
                "every row must carry both")
    return ok


def test_merge_is_page_ordered():
    group("merge order")
    ok = True
    rows_a = P.parse_rows(html_of("collection"), fixture("collection")["url"],
                          page=1, mode="items")[:3]
    rows_b = P.parse_rows(html_of("collection_solana"),
                          fixture("collection_solana")["url"], page=2,
                          mode="items")[:3]
    forward, _, _ = output_writer.merge_pages([(1, rows_a), (2, rows_b)])
    backward, _, _ = output_writer.merge_pages([(2, rows_b), (1, rows_a)])
    ok &= check([r.sku for r in forward] == [r.sku for r in backward],
                "the merged order depends on which page arrived first (§8)")
    ok &= check([r.sku for r in forward][:3] == [r.sku for r in rows_a],
                "page 1's rows must come first")
    # is_deeper is inert here, and that is a measured property of the modes
    # rather than a stub — but it must still be CALLED, or merge_pages would
    # be carrying a hook nothing reaches.
    ok &= check(output_writer.is_deeper(rows_a[0], rows_b[0]) is False,
                "no mode here reads one key twice at two depths")
    return ok


def test_dedupe():
    group("dedupe")
    ok = True
    rows = P.parse_rows(html_of("collection"), fixture("collection")["url"],
                        mode="items")
    seen = set()
    first = output_writer.dedupe_by_key(rows, seen)
    second = output_writer.dedupe_by_key(rows, seen)
    ok &= check(len(first) == len(rows), "the first pass should keep every row")
    ok &= check(second == [], "a replayed cursor page must be deduped away")
    # A row with no key is always kept: dropping it would be a silent data
    # loss rather than a duplicate removal.
    keyless = output_writer.Item(sku=None, title="no id")
    ok &= check(output_writer.dedupe_by_key([keyless, keyless], set())
                == [keyless, keyless],
                "rows with no key must not be deduped against each other")
    return ok


# ===========================================================================
# The engines
# ===========================================================================
ENGINES = ("playwright_scraper", "puppeteer_scraper", "selenium_scraper")

# Flags one engine has and its twins do not, with the reason. The list IS the
# documentation, so closing a difference fails this check too — §20's rule
# that a parity check must assert in BOTH directions.
FLAG_EXCEPTIONS = {
    "--chromium-path": ("puppeteer_scraper",),   # pyppeteer downloads its own
                                                 # browser; the other two do
                                                 # not have the concept
}


def _engine_source(name):
    path = os.path.join(REPO_ROOT, name + ".py")
    with open(path, encoding="utf-8") as f:
        return f.read()


def _engine_flags(name):
    """The CLI flags an engine DECLARES, read from its add_argument calls.

    Not every quoted `"--something"` in the file: Chromium's own launch
    arguments are spelled the same way (`--no-sandbox`,
    `--disable-dev-shm-usage`) and a text scan reported them as a parity
    failure between engines that simply launch different browsers.
    """
    tree = ast.parse(_engine_source(name))
    parse_args = next((n for n in tree.body
                       if isinstance(n, ast.FunctionDef)
                       and n.name == "parse_args"), None)
    if parse_args is None:
        return set()
    # Scoped to `parse_args` for a reason that bit once: Selenium's
    # `options.add_argument("--no-sandbox")` is the same method NAME as
    # argparse's, so a walk over the whole module reported Chromium's launch
    # arguments as CLI flags this engine had and its twins did not.
    flags = set()
    for node in ast.walk(parse_args):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "add_argument":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and arg.value.startswith("--"):
                    flags.add(arg.value)
    return flags


def test_engine_flag_parity():
    group("engine flag parity")
    ok = True
    flags = {name: _engine_flags(name) for name in ENGINES}
    union = set().union(*flags.values())
    for flag in sorted(union):
        carriers = tuple(sorted(n for n in ENGINES if flag in flags[n]))
        if len(carriers) == len(ENGINES):
            continue
        expected = FLAG_EXCEPTIONS.get(flag)
        ok &= check(expected is not None and carriers == tuple(sorted(expected)),
                    "%s is on %s and not on the others, and that difference "
                    "is not in FLAG_EXCEPTIONS. Either add it to every "
                    "engine, or document why it cannot be." % (flag, carriers))
    # The other direction: an exception that is no longer a difference is
    # stale documentation, and stale documentation is what this check exists
    # to prevent.
    for flag, expected in FLAG_EXCEPTIONS.items():
        carriers = tuple(sorted(n for n in ENGINES if flag in flags[n]))
        ok &= check(carriers == tuple(sorted(expected)),
                    "FLAG_EXCEPTIONS says %s is only on %s, but it is on %s"
                    % (flag, tuple(sorted(expected)), carriers))

    # The family contract (§9), asserted against the primary engine.
    contract = ("--url --pages --category --format --out --delay --retries "
                "--retry-delay --concurrency --proxy --proxy-file "
                "--proxy-rotate --proxy-shuffle --proxy-block-retries "
                "--twocaptcha-key --captcha-api --solve-captcha --min-score "
                "--cdp-endpoint --allow-empty --dump-html --headless "
                "--headful --fingerprint --fp-country --fp-tags --locale "
                "--mode").split()
    for flag in contract:
        ok &= check(flag in flags["playwright_scraper"],
                    "the family contract's %s is missing from the primary "
                    "engine" % flag)

    # A flag this repo BANS, scoped to the engines: a --country on a scraper
    # could disagree with the URL it was given. It is legitimate on
    # fingerprint_client.py, which is why the check is scoped (§10).
    for name in ENGINES:
        ok &= check("--country" not in flags[name],
                    "%s has a --country flag; on this site the locale is a "
                    "path in the URL and a flag could only disagree with it"
                    % name)
    return ok


def test_engine_constants_agree():
    group("engine constants")
    ok = True
    values = {}
    for name in ENGINES:
        source = _engine_source(name)
        tree = ast.parse(source)
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
                if target in ("FIELD_FLOOR", "DEFAULT_BROWSER_CHANNEL",
                              "CDP_CONNECT_TIMEOUT_MS"):
                    with contextlib.suppress(ValueError):
                        found[target] = ast.literal_eval(node.value)
        values[name] = found
    for constant in ("FIELD_FLOOR", "DEFAULT_BROWSER_CHANNEL",
                     "CDP_CONNECT_TIMEOUT_MS"):
        seen = {name: values[name].get(constant) for name in ENGINES}
        ok &= check(len(set(seen.values())) == 1,
                    "the engines disagree about %s: %s — one engine "
                    "reporting a different threshold than its twins on the "
                    "identical run is exactly the drift the shared modules "
                    "exist to prevent (§6)" % (constant, seen))
    return ok


def test_engines_import_their_driver_at_module_level():
    """Or the offline suite's "works without an engine" claim means nothing.

    A sibling repo imported `launch`/`connect` inside the launch path, so the
    module imported cleanly with no pyppeteer installed: the group never
    skipped, and the CI job that exists to fail on unexpected skips could not
    have caught a broken import (§10).
    """
    group("module-level driver imports")
    ok = True
    wanted = {"playwright_scraper": "playwright",
              "puppeteer_scraper": "pyppeteer",
              "selenium_scraper": "selenium"}
    for name, library in wanted.items():
        tree = ast.parse(_engine_source(name))
        at_module_level = False
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", "") or ""
                names = [a.name for a in node.names]
                if module.startswith(library) or any(
                        n.startswith(library) for n in names):
                    at_module_level = True
        ok &= check(at_module_level,
                    "%s does not import %s at MODULE level, so the offline "
                    "suite would not skip when the library is absent — and a "
                    "skip that never happens cannot be failed on"
                    % (name, library))
    return ok


def test_engine_parity(skips):
    """Bind every shared-module call in every engine against the real signature.

    Two engines in a sibling repo called `classify(html, url=...)` where the
    parameter is positional. Both crashed on their FIRST fetch, and it was
    invisible to import, `--help`, `compileall`, the undefined-name walk and
    400+ green assertions — because none of those calls a function the way a
    live run does (§17).
    """
    group("shared-module call signatures")
    ok = True
    shared = {"page_flow": page_flow, "product_parser": P,
              "output_writer": output_writer}
    aliases = {"P": "product_parser"}
    for name in ENGINES:
        tree = ast.parse(_engine_source(name))
        # Which shared names this engine imported, and from where.
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in shared:
                for alias in node.names:
                    imported[alias.asname or alias.name] = (node.module,
                                                            alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = None
            if isinstance(node.func, ast.Attribute) and \
                    isinstance(node.func.value, ast.Name):
                module_name = aliases.get(node.func.value.id,
                                          node.func.value.id)
                if module_name in shared:
                    target = (module_name, node.func.attr)
            elif isinstance(node.func, ast.Name) and node.func.id in imported:
                target = imported[node.func.id]
            if target is None:
                continue
            module, attribute = target
            function = getattr(shared[module], attribute, None)
            if not callable(function):
                ok &= check(function is not None,
                            "%s calls %s.%s, which does not exist"
                            % (name, module, attribute))
                continue
            try:
                signature = inspect.signature(function)
            except (TypeError, ValueError):
                continue
            positional = [ast.literal_eval(a) if isinstance(a, ast.Constant)
                          else object() for a in node.args]
            keywords = {}
            for keyword in node.keywords:
                if keyword.arg is None:
                    keywords = None
                    break
                keywords[keyword.arg] = object()
            if keywords is None:
                continue
            try:
                signature.bind(*positional, **keywords)
            except TypeError as exc:
                ok = check(False,
                           "%s line %d: %s.%s(%s) does not match its real "
                           "signature %s — %s"
                           % (name, node.lineno, module, attribute,
                              ", ".join(list(map(repr, positional))
                                        + sorted(keywords)),
                              signature, exc))
    return ok


def test_no_undefined_names():
    """`compileall` proves a file PARSES, not that its names RESOLVE.

    A sibling repo's engine died with `NameError` on a line reached only
    while fetching, after an import had been removed. The module imported
    cleanly, `--help` worked, `compileall` passed and CI was green (§10).

    Deliberately COARSE — one pool of bindings per module, no scope
    tracking — so it under-reports rather than inventing problems.
    """
    group("undefined names")
    ok = True
    modules = [f for f in os.listdir(REPO_ROOT) if f.endswith(".py")]
    for filename in sorted(modules):
        with open(os.path.join(REPO_ROOT, filename), encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename)
        import builtins
        bound = set(dir(builtins))
        bound |= {"__file__", "__name__", "__doc__", "__builtins__",
                  "self", "cls"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.Lambda):
                # A lambda's parameters are bindings too, and every engine
                # hands page_flow its driver primitives as lambdas.
                args = node.args
                for arg in (args.posonlyargs + args.args + args.kwonlyargs):
                    bound.add(arg.arg)
                if args.vararg:
                    bound.add(args.vararg.arg)
                if args.kwarg:
                    bound.add(args.kwarg.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                bound.add(node.name)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = node.args
                    for arg in (args.posonlyargs + args.args + args.kwonlyargs):
                        bound.add(arg.arg)
                    if args.vararg:
                        bound.add(args.vararg.arg)
                    if args.kwarg:
                        bound.add(args.kwarg.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bound.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                bound.update(node.names)
            elif isinstance(node, ast.comprehension):
                for sub in ast.walk(node.target):
                    if isinstance(sub, ast.Name):
                        bound.add(sub.id)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                ok &= check(node.id in bound,
                            "%s line %d: %r is used and never imported, "
                            "defined or assigned"
                            % (filename, node.lineno, node.id))
    return ok


def test_dockerfile_matches_its_entrypoint():
    """The COPY list against the real import graph.

    All three repos in this family shipped an image that died with
    ModuleNotFoundError on every invocation, `--help` included, because
    `proxy_pool.py` was missing from the COPY list. CI never builds the
    image, so nothing noticed (§10).
    """
    group("dockerfile")
    ok = True
    with open(os.path.join(REPO_ROOT, "Dockerfile"), encoding="utf-8") as f:
        dockerfile = f.read()
    # The COPY lines only. The comments above them NAME other files — this
    # check's own file among them — and a text scan of the whole Dockerfile
    # reported smoke_test.py as being copied into the image because the
    # comment explaining this check mentions it.
    copy_lines = []
    for block in re.findall(r"^COPY\s+(.*?)(?=^\S|\Z)", dockerfile,
                            re.M | re.S):
        copy_lines.append(block.replace("\\\n", " "))
    copied = set(re.findall(r"([a-z_]+\.py)", " ".join(copy_lines)))
    entry = re.search(r'ENTRYPOINT \["python3", "([a-z_]+\.py)"', dockerfile)
    ok &= check(entry is not None, "the Dockerfile has no ENTRYPOINT script")
    if not entry:
        return ok

    local = {f[:-3] for f in os.listdir(REPO_ROOT) if f.endswith(".py")}
    needed, queue = set(), [entry.group(1)[:-3]]
    while queue:
        module = queue.pop()
        if module in needed or module not in local:
            continue
        needed.add(module)
        with open(os.path.join(REPO_ROOT, module + ".py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                queue += [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                queue.append(node.module.split(".")[0])
    for module in sorted(needed):
        ok &= check(module + ".py" in copied,
                    "the Dockerfile does not COPY %s.py, which its "
                    "entrypoint imports — the image would die with "
                    "ModuleNotFoundError on every invocation, `--help` "
                    "included" % module)
    # And the other direction: nothing that does not belong in an image.
    for unwanted in ("smoke_test.py", "make_fixtures.py"):
        ok &= check(unwanted not in copied,
                    "the Dockerfile copies %s into the image" % unwanted)
    with open(os.path.join(REPO_ROOT, ".dockerignore"), encoding="utf-8") as f:
        ignored = f.read()
    ok &= check(".env" in ignored,
                "a .env baked into an image is a credential published to "
                "everyone who can pull it")
    return ok


# ===========================================================================
# Wording, configuration and the repo itself
# ===========================================================================
BANNED_PHRASES = (
    "cloud browser",
    "antidetect browser",
    "2scraper Antidetect Browser",
    "gate.2prx.com",
    "--antidetect",
    "ANTIDETECT_LOCAL_API",
)


def test_wording():
    """§12's table, enforced. It reached two public repo DESCRIPTIONS while
    the suite scanned only files, so `.github/repo-metadata.yml` is scanned
    here too — a banned-wording check should cover the surfaces the repo
    publishes, not only the files it contains (§21)."""
    group("wording")
    ok = True
    scanned = 0
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "__pycache__", ".venv", ".venv-pup", "live",
                    "node_modules")]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt",
                                      ".toml", ".example")):
                continue
            path = os.path.join(root, filename)
            # The two files that DEFINE the banned list necessarily contain
            # it. Excluded by name rather than by a cleverer match, so that
            # adding a third place the list lives is a decision.
            if filename in ("smoke_test.py", "ci_checks.py"):
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            scanned += 1
            for phrase in BANNED_PHRASES:
                if phrase == "cloud browser" and "Scraping Browser" in text \
                        and phrase not in text.lower():
                    continue
                ok &= check(phrase.lower() not in text.lower(),
                            "%s contains the banned phrase %r — write "
                            "'Scraping Browser API' instead (§12)"
                            % (os.path.relpath(path, REPO_ROOT), phrase))
    ok &= check(scanned > 10, "the wording scan looked at almost nothing")
    return ok


def test_env_example_matches_the_code():
    group("env")
    ok = True
    with open(os.path.join(REPO_ROOT, ".env.example"), encoding="utf-8") as f:
        example = f.read()
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", example, re.M))
    read = set(env_config.ENV_KEYS)
    ok &= check(documented == read,
                "the .env.example and ENV_KEYS disagree: only in the example "
                "%s; only in the code %s"
                % (sorted(documented - read), sorted(read - documented)))
    for name in read:
        ok &= check(name == "TWOCAPTCHA_KEY" or name.startswith("OPENSEA_"),
                    "%s does not follow the family's naming (TWOCAPTCHA_KEY "
                    "or OPENSEA_*)" % name)

    # A copied example must read as UNSET for every credential, and stay
    # usable for the one value that is not one (§17).
    import argparse as argparse_module
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, ".env")
        with open(path, "w", encoding="utf-8") as f:
            f.write(example)
        saved = {k: os.environ.pop(k, None) for k in read}
        try:
            env_config.load_env(path)
            namespace = argparse_module.Namespace(
                **{dest: None for dest in env_config.ENV_KEYS.values()})
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                env_config.apply(namespace, quiet=True)
            ok &= check(namespace.twocaptcha_key is None,
                        "a copied .env.example was read as a configured key")
            ok &= check(namespace.cdp_endpoint is None,
                        "a copied .env.example's CDP endpoint was read as "
                        "configured — it would connect with the literal "
                        "'{login}-zone-…' as its username and get a 401 a "
                        "long way from its cause (§17)")
            ok &= check(namespace.proxy is None,
                        "a copied .env.example's proxy was read as configured")
            ok &= check(namespace.url and namespace.url.startswith("https://"),
                        "the example's non-credential default should stay "
                        "usable")
        finally:
            for k in read:
                os.environ.pop(k, None)
                if saved.get(k) is not None:
                    os.environ[k] = saved[k]
    return ok


def test_credentials_never_reach_logs():
    group("credential masking")
    ok = True
    # Built by concatenation on purpose: no LINE of this file then holds a
    # string shaped like a credentialled URL, so the committed-credential
    # check needs no allowlist entry for it — and an allowlist entry is the
    # thing that would also let a real one through (§17).
    secret = "hunter" + "2"
    url = "http://user:" + secret + "@exit.example.com:2334"
    masked = proxy_pool.mask(url)
    ok &= check("hunter2" not in masked, "mask() leaked a password")
    ok &= check("exit.example.com" in masked and "2334" in masked,
                "mask() should KEEP host and port — which exit a run used is "
                "the point of the log and is not the secret (§8)")
    # Globally, not once: a Playwright connection error repeats an endpoint
    # five times, and a masker that handles the first prints the password the
    # other four times while looking like it works.
    repeated = " ".join([url] * 5)
    for name in ENGINES:
        source = _engine_source(name)
        ok &= check("_CREDENTIALS_IN_URL_RE" in source,
                    "%s has no credential masker" % name)
        ok &= check(".sub(" in source,
                    "%s's masker does not look like a global substitution"
                    % name)
    import importlib
    for name in ENGINES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        masked = module._mask_credentials(repeated)
        ok &= check("hunter2" not in masked,
                    "%s's masker left a password in a repeated endpoint"
                    % name)
    return ok


def test_ci_checks_is_wired_up():
    """ONE implementation, invoked from both CI and this suite (§17).

    Three repos in this family shipped `.github/ci_checks.py` that NOTHING
    ran, while the workflow carried an inline grep doing a narrower version
    of the same job with its own allowlist — two sources of truth, one dead
    and one holed.
    """
    group("ci checks")
    ok = True
    script = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check(os.path.exists(script), "ci_checks.py is missing")
    workflow = os.path.join(REPO_ROOT, ".github", "workflows", "tests.yml")
    with open(workflow, encoding="utf-8") as f:
        text = f.read()
    ok &= check("ci_checks.py" in text,
                "tests.yml does not invoke ci_checks.py, so the check either "
                "runs nowhere or is reimplemented inline")
    result = subprocess.run([sys.executable, script, "--secret-check"],
                            cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check(result.returncode == 0,
                "the committed-credential check FAILS on this repository: "
                "%s" % (result.stdout + result.stderr)[-400:])
    return ok


def test_sample_output():
    group("sample output")
    ok = True
    json_path = os.path.join(REPO_ROOT, "sample_output.json")
    csv_path = os.path.join(REPO_ROOT, "sample_output.csv")
    ok &= check(os.path.exists(json_path) and os.path.exists(csv_path),
                "sample_output.{json,csv} must be cut from a real run")
    if not os.path.exists(json_path):
        return ok
    with open(json_path, encoding="utf-8") as f:
        rows = json.load(f)
    ok &= check(bool(rows), "sample_output.json is empty")
    expected = [f.name for f in dataclass_fields(output_writer.Item)]
    ok &= check(list(rows[0]) == expected,
                "sample_output.json's columns do not match the Item "
                "dataclass")
    with open(csv_path, encoding="utf-8") as f:
        header = next(csv_module.reader(f))
    ok &= check(header == expected,
                "sample_output.csv's header does not match the Item "
                "dataclass")
    for marker in ("example.com", "lorem", "ipsum", "FIXME", "TODO",
                   "0x0000000000000000000000000000000000000000"):
        ok &= check(not any(marker.lower() in json.dumps(r).lower()
                            for r in rows),
                    "sample_output.json looks fabricated: it contains %r"
                    % marker)
    return ok


def test_no_dead_policy_constants():
    """A policy constant nothing reads is the same defect as dead code (§17)."""
    group("policy constants")
    ok = True
    sources = {name: _engine_source(name) for name in ENGINES}
    for constant in ("RETRY_ON_BLOCKED", "RETRY_NEEDS_FRESH_CONTEXT",
                     "BLOCK_RETRIES_WITHOUT_POOL", "SOLVES_PER_PAGE",
                     "MIN_CARD_MATCHES"):
        consumers = [name for name, text in sources.items()
                     if constant in text]
        ok &= check(len(consumers) == len(ENGINES),
                    "page_flow.%s is read by %s and not by the others — a "
                    "policy that looks enforced and is not is worse than no "
                    "policy (§17)" % (constant, consumers or "nothing"))
    return ok


def test_engine_modules_import(skips):
    """Every engine must import, or say which library it is missing."""
    group("engine imports")
    ok = True
    import importlib
    for name in ENGINES:
        try:
            importlib.import_module(name)
            print("  ok    %s imports" % name)
        except ImportError as exc:
            # The wording matters: CI's engine-smoke job greps for exactly
            # this sentence to tell "absent by design" from "installed and
            # broken". A skip reads like a pass, so the job that installs the
            # engine has to be able to fail on it.
            message = "%s could not be imported (%s)" % (name, exc)
            skips.append(message)
            print("  SKIP  %s" % message)
    return ok


def main():
    print("opensea-scraper offline smoke tests")
    skips = []
    ok = True
    ok &= test_fixtures_are_present()
    ok &= test_values_on_real_fixtures()
    ok &= test_row_shape_per_mode()
    ok &= test_solana_addresses()
    ok &= test_empty_symbol_is_null()
    ok &= test_prices_are_tokens_not_dollars()
    ok &= test_traits_and_rarity()
    ok &= test_activity_rows()
    ok &= test_collection_rows()
    ok &= test_collection_totals()
    ok &= test_markers_do_not_match_a_good_page()
    ok &= test_cf_turnstile_is_not_a_marker()
    ok &= test_extension_injection_does_not_read_as_a_challenge()
    ok &= test_the_not_found_sentence_is_not_a_marker()
    ok &= test_classification()
    ok &= test_state_policy_is_complete()
    ok &= test_cursors()
    ok &= test_page_url_is_none()
    ok &= test_ssr_request_match()
    ok &= test_url_knowledge()
    ok &= test_graphql_bodies()
    ok &= test_graphql_errors_are_loud()
    ok &= test_graphql_and_state_agree()
    ok &= test_dom_fallback()
    ok &= test_output_contract()
    ok &= test_page_and_position_are_unique()
    ok &= test_merge_is_page_ordered()
    ok &= test_dedupe()
    ok &= test_engine_flag_parity()
    ok &= test_engine_constants_agree()
    ok &= test_engines_import_their_driver_at_module_level()
    ok &= test_engine_parity(skips)
    ok &= test_no_undefined_names()
    ok &= test_dockerfile_matches_its_entrypoint()
    ok &= test_wording()
    ok &= test_env_example_matches_the_code()
    ok &= test_credentials_never_reach_logs()
    ok &= test_ci_checks_is_wired_up()
    ok &= test_sample_output()
    ok &= test_no_dead_policy_constants()
    ok &= test_engine_modules_import(skips)

    passed = _total_checks - len(_failures)
    if passed < CLAIMED_CHECK_FLOOR:
        ok = False
        _failures.append(
            "this suite is supposed to run over %d checks and only %d ran — "
            "either checks were removed or the claim needs lowering"
            % (CLAIMED_CHECK_FLOOR, passed))

    print()
    print("%d check(s) run, %d passed" % (_total_checks, passed))
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for failure in _failures:
            print("  - %s" % failure)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs each engine in its own "
              "venv and fails if this list is non-empty, because a skip reads "
              "exactly like a passing run:" % len(skips))
        for skip in skips:
            print("  - %s" % skip)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
