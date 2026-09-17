"""product_parser.py — the OpenSea half of this scraper.

Everything that knows what OpenSea IS lives here. The engines know how to
drive a browser; `page_flow.py` knows what to do with a page; this module
knows what an OpenSea page contains, where the ids are, and how the site
spells a price.

THE ONE THING TO READ FIRST
---------------------------
OpenSea's own front end talks to `https://gql.opensea.io/graphql`, and that
endpoint answers an anonymous POST with no key, no cookie and no signed
header. The page you are looking at and the endpoint it calls hand back the
SAME objects — the server-rendered state inlined in the HTML is literally an
urql cache of those GraphQL responses — so ONE normaliser reads both routes
and they cannot drift (§21, which is where this pattern was learned).

That is why this file has two entry points and one set of row builders:

    parse_rows(html, url, …)          the page's own inlined state
    rows_from_graphql(data, url, …)   a response from the endpoint

Page 1 of a run comes from the first, because it proves the site served the
page. Pages 2..N come from the second, because that is the only thing that
paginates (see PAGE_URL_REASON).

WHAT THIS SITE DOES NOT HAVE
----------------------------
* **No product JSON-LD.** Measured 2026-09-17: a collection page carries
  three `application/ld+json` blocks and they are `BreadcrumbList`, `Brand`
  and `WebSite` — site navigation and a collection description, not items.
  §4 says count the blocks before claiming a JSON-LD path, and counting them
  is what says there isn't one. The site's own inlined state is the primary
  path instead, and the URL pattern is the fallback.

* **No page numbers, no `?page=`, no `link[rel=next]`.** Every feed here is
  cursor-paginated: a response carries `nextPageCursor`, an opaque
  base64 string, and the next request passes it back as `after`. There is no
  address for page 2 of a collection, which is why `page_url()` returns None
  and `--concurrency` above 1 is refused with the reason (§7, §18).

WHAT IT DOES HAVE THAT IS EASY TO GET WRONG
-------------------------------------------
* **An item's address has two shapes, and the chain picks which.** An EVM
  item is `/item/ethereum/{contract}/{token_id}`; a Solana item is
  `/item/solana/{mint_address}`, with no token id at all. Measured on two
  captures: 32 distinct item links on an Ethereum collection page, all
  three-segment; 32 on a Solana one, all two-segment. A regex written
  against one shape silently drops the other.

* **A price is a TOKEN amount, and the token is named.** `ETH`, `WETH`,
  `SOL`, `USDC`. It is never assumed: `_price()` reads the symbol the site
  published and returns None when there is not one, which is §4's rule with
  crypto in place of fiat.

* **An EMPTY symbol is a real state, and it is common.** On the Bored Ape
  capture, 11 of 50 items' last sales were denominated in a token OpenSea
  did not name — `"symbol": ""` with a real contract address behind it
  (25 were ETH and 14 WETH). An empty string written through as a currency
  is worse than a null, because it reads as a currency. It becomes None
  here, and the USD figure beside it is what stays comparable.

* **Zero is not a price.** OpenSea publishes `floorPrice: null` on a
  collection with nothing listed, but a ranking's rolling stats publish
  `floorPriceChange: 0` and `sales: 0` for a window in which nothing
  happened — those zeros are real measurements and stay. The nulls stay
  null. (§21's "zero is not a rating", in the shape this site takes it.)
"""

from __future__ import annotations

import html as html_module
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from bs4 import BeautifulSoup

from output_writer import (SOURCE_DEFAULT, Item, Collection, Activity,
                           ROW_CLASS_BY_MODE)


# ---------------------------------------------------------------------------
# The site
# ---------------------------------------------------------------------------
# One host. OpenSea publishes an hreflang set (read from the `link` header of
# a collection page on 2026-09-17: en-US, es, de-DE, fr, ja, zh-CN, zh-TW),
# and every entry is a PATH under the same host rather than a host of its
# own — the alternates point at an internal preview domain, which is the
# site's own deployment plumbing and not somewhere a scraper should follow.
HOSTS = ("opensea.io",)

# Locale prefixes the site serves, as PATH segments: /ja/collection/{slug}.
# `en-US` has no prefix. Used to strip the locale before reading a route, so
# `/ja/collection/x` and `/collection/x` are the same page kind.
LOCALES = ("es", "de-DE", "fr", "ja", "zh-CN", "zh-TW")

GRAPHQL_ENDPOINT = "https://gql.opensea.io/graphql"

# The most pages one run will fetch. 100 pages x 100 rows is 10,000 rows,
# which is one whole Bored Ape Yacht Club — past that a caller wants several
# runs and a `--sort` they chose on purpose rather than one long walk.
PAGE_CAP = 100

# How many rows one GraphQL request may ask for. The endpoint states its own
# limit in an error rather than truncating silently
# ("/collectionItems/limit range must be between 1 and 100", measured
# 2026-09-17), so this is the site's number and not a guess.
MAX_LIMIT = 100

MODES = ("items", "collections", "activity")

# What a rendered page is made of, for the readiness wait and the DOM
# fallback. Anchored on the URL PATTERN and never on a class: OpenSea's
# classes are Tailwind utilities and build hashes that change on every
# deploy, while `/item/{chain}/…` is the address it publishes to search
# engines (§4).
SELECTORS = {
    "item_link": 'a[href*="/item/"]',
    "item_card": 'a[href*="/item/"]',
    "collection_link": 'a[href^="/collection/"], a[href*="/collection/"]',
}

# The states `detect_page_state` can return. `page_flow.STATE_POLICY` asserts
# it covers exactly these at import time.
PAGE_STATES = ("content", "empty", "shell", "challenge", "blocked")

PAGE_URL_REASON = (
    "OpenSea paginates every feed with an opaque cursor rather than with a "
    "page number: a response carries `nextPageCursor` and the next request "
    "passes it back as `after`, so page 5's request is unknowable until "
    "page 4 has been read"
)

CONCURRENCY_REASON = (
    "there is no address to hand a second worker — the cursor chain is "
    "strictly sequential by construction"
)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
_TRACKING_PREFIXES = ("utm_", "ref_")
_TRACKING_KEYS = {"ref", "referrer", "source", "fromSearch"}


def site_host(url: str) -> str:
    """The bare host of `url`, lowercased and without `www.`."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_supported_host(url: str) -> bool:
    return site_host(url) in HOSTS


def source_of(url: str) -> str:
    """The `source` column's value for a row read from `url`."""
    return site_host(url) or SOURCE_DEFAULT


def strip_tracking(url: str) -> str:
    """`url` with the site's own click/attribution parameters removed.

    Said out loud in the engines when it changes the address, because
    silently fetching something other than what was typed is how a run's rows
    stop matching its command line.
    """
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in _TRACKING_KEYS
            and not any(k.startswith(p) for p in _TRACKING_PREFIXES)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), ""))


def normalize_url(url: str) -> str:
    """The address this scraper will actually fetch.

    Tracking parameters go, the fragment goes, and a trailing slash on a
    route goes — `/collection/x/` and `/collection/x` are one page and must
    not produce two different `url` values for the same row.
    """
    cleaned = strip_tracking(url)
    parts = urlsplit(cleaned)
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def locale_of(url: str) -> Optional[str]:
    """The locale prefix in `url`'s path, or None for the default English."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if segments and segments[0] in LOCALES:
        return segments[0]
    return None


def _route_segments(url: str) -> List[str]:
    """`url`'s path segments with any locale prefix removed."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if segments and segments[0] in LOCALES:
        segments = segments[1:]
    return segments


def page_kind(url: str) -> str:
    """Which kind of OpenSea page `url` addresses.

        collection   /collection/{slug}          -> --mode items
        activity     /collection/{slug}/activity -> --mode activity
        collections  /collections, /rankings     -> --mode collections
        item         /item/{chain}/…             -> --mode items, one row
        other        anything else
    """
    segments = _route_segments(url)
    if not segments:
        return "other"
    head = segments[0]
    if head == "collection" and len(segments) >= 2:
        if len(segments) >= 3 and segments[2] == "activity":
            return "activity"
        return "collection"
    if head in ("collections", "rankings"):
        return "collections"
    if head == "item" and len(segments) >= 3:
        return "item"
    return "other"


def mode_for_url(url: str) -> Optional[str]:
    """The mode `url` implies, or None where the URL does not settle it."""
    kind = page_kind(url)
    if kind in ("collection", "item"):
        return "items"
    if kind == "activity":
        return "activity"
    if kind == "collections":
        return "collections"
    return None


def unsupported_reason(url: str) -> Optional[str]:
    """Why this scraper refuses `url`, or None if it will take it.

    Refused rather than attempted, and WITH the reason (§5). Every path
    pattern, every payload field name and the whole GraphQL vocabulary in
    this file are OpenSea's, so pointing this at another marketplace would
    not fail loudly — it would return zero rows and read as an empty
    collection.
    """
    if not urlsplit(url).scheme.startswith("http"):
        return (f"{url!r} is not an http(s) URL. Pass a full address, e.g. "
                f"https://opensea.io/collection/boredapeyachtclub")
    host = site_host(url)
    if not host:
        return f"{url!r} has no host in it."
    if host not in HOSTS:
        return (f"{host} is not OpenSea. This scraper reads opensea.io — its "
                f"routes, its inlined state and its GraphQL vocabulary — so "
                f"on another marketplace it would return zero rows rather "
                f"than fail, which is worse.")
    kind = page_kind(url)
    if kind == "other":
        segments = _route_segments(url)
        route = "/" + "/".join(segments[:2]) if segments else "/"
        return (f"{route} is an OpenSea page this scraper does not read. It "
                f"reads three: /collection/{{slug}} (items), "
                f"/collection/{{slug}}/activity (events) and /collections "
                f"(the ranking). Token trading, swaps, drops, profiles and "
                f"the studio are all real OpenSea routes and none of them "
                f"holds the rows these modes describe.")
    return None


def collection_url_for(slug: str, locale: Optional[str] = None) -> str:
    prefix = f"/{locale}" if locale else ""
    return f"https://opensea.io{prefix}/collection/{slug}"


def activity_url_for(slug: str, locale: Optional[str] = None) -> str:
    return collection_url_for(slug, locale) + "/activity"


def collections_url(locale: Optional[str] = None) -> str:
    prefix = f"/{locale}" if locale else ""
    return f"https://opensea.io{prefix}/collections"


def url_for_mode(mode: str, target: str = "", locale: Optional[str] = None) -> str:
    """The page `--mode {mode}` starts from, given a collection slug."""
    if mode == "collections":
        return collections_url(locale)
    if not target:
        raise ValueError(f"--mode {mode} needs a collection slug")
    if mode == "activity":
        return activity_url_for(target, locale)
    return collection_url_for(target, locale)


def collection_slug_from_url(url: str) -> Optional[str]:
    """The collection slug in `url`, for a collection or activity page."""
    segments = _route_segments(url)
    if len(segments) >= 2 and segments[0] == "collection":
        return segments[1]
    return None


# An item address, in both of the shapes the site publishes:
#
#   /item/ethereum/0xbc4ca0…f13d/1     EVM: chain, contract, token id
#   /item/solana/3bF2maQ97hX8…6BxT     Solana: chain, mint address
#
# One regex with the third group optional rather than two patterns, so a
# caller cannot use the one that happens to match the chain it tested on.
_ITEM_IN_URL_RE = re.compile(
    r"/item/(?P<chain>[a-z0-9_]+)/(?P<contract>[A-Za-z0-9]+)(?:/(?P<token>[^/?#]+))?")


def item_parts_from_url(url: str) -> Optional[Tuple[str, str, Optional[str]]]:
    """(chain, contract, token_id) out of an item address, or None."""
    match = _ITEM_IN_URL_RE.search(url or "")
    if not match:
        return None
    return (match.group("chain"), match.group("contract"), match.group("token"))


def sku_for(chain: Optional[str], contract: Optional[str],
            token_id: Optional[str]) -> Optional[str]:
    """The `sku` column: "{chain}/{contract}/{token_id}", or two parts on Solana."""
    if not chain or not contract:
        return None
    return f"{chain}/{contract}/{token_id}" if token_id else f"{chain}/{contract}"


def sku_from_url(url: str) -> Optional[str]:
    parts = item_parts_from_url(url)
    return sku_for(*parts) if parts else None


def item_url(chain: Optional[str], contract: Optional[str],
             token_id: Optional[str], locale: Optional[str] = None) -> str:
    """An item's own page. Empty string when the parts are not there."""
    if not chain or not contract:
        return ""
    prefix = f"/{locale}" if locale else ""
    tail = f"/{token_id}" if token_id else ""
    return f"https://opensea.io{prefix}/item/{chain}/{contract}{tail}"


def page_url(url: str, page_num: int) -> None:
    """None, on every page, because this site has no page addresses.

    Kept — with this name and this signature — because every repo in the
    family has it and the smoke suite checks that the engines agree about
    what it returns. Returning None is the honest answer and the engines
    read it as "ask the site for the next cursor instead"; returning a made-
    up `?page=N` would be the §7 failure in its purest form, since OpenSea
    answers an unknown query parameter with page 1 and a run built on it
    would report COMPLETE holding fifty rows over and over.
    """
    return None


def paginates_by_url(url: str = "") -> bool:
    """False, always. See `page_url`."""
    return False


def redirected_away(asked: str, got: str) -> Optional[str]:
    """Why the site answered a different address than the one asked for.

    None when it did not. Two redirects on this site change what was asked
    for: `/rankings` resolves to `/collections` (harmless — the same ranking
    under its current name) and a locale prefix can be dropped or added,
    which changes the wording and nothing else. A collection slug resolving
    to a DIFFERENT slug is the one that matters, because the rows then
    belong to another collection.
    """
    if not asked or not got:
        return None
    if strip_tracking(asked) == strip_tracking(got):
        return None
    asked_slug, got_slug = collection_slug_from_url(asked), collection_slug_from_url(got)
    if asked_slug and got_slug and asked_slug != got_slug:
        return (f"asked for the collection {asked_slug!r} and the site "
                f"served {got_slug!r}")
    if page_kind(asked) != page_kind(got):
        return (f"asked for a {page_kind(asked)} page and the site served a "
                f"{page_kind(got)} one ({got})")
    return None


# ---------------------------------------------------------------------------
# The page's own inlined state
# ---------------------------------------------------------------------------
# OpenSea is a Next.js app whose pages ship their GraphQL cache inline:
#
#   <script>(window[Symbol.for("urql_transport")] ??= []).push(
#       {"rehydrate":{"16774269391":{"hasNext":false,"data":{…}}}})</script>
#
# There are several of those per page — four on a collection page, one on the
# ranking — and each holds one operation's result. The one this scraper wants
# is whichever carries `collectionItems`, `collectionRankings`,
# `collectionActivity` or `itemByIdentifier`.
#
# Bounded and non-greedy so a page that never closes the call cannot make the
# regex walk two megabytes of unrelated markup.
_URQL_PUSH_RE = re.compile(
    r'urql_transport"\)\]\s*\?\?=\s*\[\]\)\.push\((\{.*?\})\)</script>', re.S)

# The GraphQL fields a mode reads, in preference order. A collection page
# carries several operations and only one of them is the feed.
PAYLOAD_FIELDS = {
    "items": ("collectionItems", "itemByIdentifier"),
    "collections": ("collectionRankings",),
    "activity": ("collectionActivity",),
}

_ALL_PAYLOAD_FIELDS = tuple(
    field for fields in PAYLOAD_FIELDS.values() for field in fields)


def ssr_operations(html: Optional[str]) -> List[dict]:
    """Every inlined operation result on the page, as `data` dicts.

    Returns [] rather than raising on anything malformed: a page that ships
    a payload this cannot read is a page the DOM fallback should get a turn
    at, not a crash (§8 — but a failure that returns [] must be VISIBLE,
    which is what `parsed_nothing_from_a_served_page` in page_flow is for).
    """
    if not html:
        return []
    operations: List[dict] = []
    for raw in _URQL_PUSH_RE.findall(html):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        rehydrate = parsed.get("rehydrate")
        if not isinstance(rehydrate, dict):
            continue
        for entry in rehydrate.values():
            data = (entry or {}).get("data")
            if isinstance(data, dict):
                operations.append(data)
    return operations


def ssr_field(html: Optional[str], mode: str = "items") -> Tuple[Optional[str], Any]:
    """(field_name, value) for the operation this mode reads, or (None, None)."""
    wanted = PAYLOAD_FIELDS.get(mode, PAYLOAD_FIELDS["items"])
    for data in ssr_operations(html):
        for field in wanted:
            value = data.get(field)
            if value:
                return field, value
    return None, None


def payload_answered(html: Optional[str], mode: str = "items") -> bool:
    """Whether the page's own state already names rows for this mode.

    The fast path, and the reason page 1 of a run costs one fetch: when this
    is True there is nothing to wait for and nothing to scroll, because
    everything page 1 will write is already in the bytes the server sent.
    """
    field, value = ssr_field(html, mode)
    if not field:
        return False
    if field == "itemByIdentifier":
        return True
    return bool((value or {}).get("items"))


def next_cursor(html: Optional[str], mode: str = "items") -> Optional[str]:
    """The cursor for the page AFTER the one in this HTML, where there is one."""
    _, value = ssr_field(html, mode)
    if isinstance(value, dict):
        return value.get("nextPageCursor")
    return None


def collection_totals(html: Optional[str]) -> Dict[str, Any]:
    """What the collection page says about the WHOLE collection.

    A collection page ships its own `collectionBySlug.stats` beside the fifty
    items it renders — `totalSupply`, `listedItemCount`, its floor and its
    volume. That is the arithmetic §21 calls for: a run of three pages is
    genuinely `complete` under the ordering it used AND is 300 rows of a
    9,998-item collection, and a sidecar that says only "complete" is lying
    by omission. The engines put this in `extra` and log the percentage.

    Returns {} where the page does not carry it (the ranking, an item page),
    which is a real answer rather than a failed read: there is no such total
    for a feed of every collection on the site.
    """
    for data in ssr_operations(html):
        collection = data.get("collectionBySlug")
        if not isinstance(collection, dict):
            continue
        stats = collection.get("stats")
        if not isinstance(stats, dict):
            continue
        floor, floor_currency, floor_usd = _price(collection.get("floorPrice"))
        totals = {
            "collection_slug": _clean(collection.get("slug")),
            "collection_name": _clean(collection.get("name")),
            "total_supply": _int(stats.get("totalSupply")),
            "unique_items": _int(stats.get("uniqueItemCount")),
            "listed_items": _int(stats.get("listedItemCount")),
            "owners": _int(stats.get("ownerCount")),
            "floor_price": floor,
            "floor_currency": floor_currency,
            "floor_price_usd": floor_usd,
        }
        return {k: v for k, v in totals.items() if v is not None}
    return {}


def count_cards(html: Optional[str]) -> int:
    """How many distinct things this page links to.

    Counts DISTINCT addresses, not anchors: a rendered tile links to its item
    twice (the image and the name), so 192 `/item/` anchors on a collection
    page are 32 items. The family learned that counting links instead of ids
    is what lets a tile-scoping walk steal its neighbour's data (§4), and the
    same arithmetic error would make a readiness threshold meaningless here.
    """
    if not html:
        return 0
    items = set(re.findall(r'href="(/(?:[a-zA-Z-]+/)?item/[^"]+)"', html))
    if items:
        return len(items)
    return len(set(re.findall(r'href="(/(?:[a-zA-Z-]+/)?collection/[^"]+)"', html)))


# ---------------------------------------------------------------------------
# Was this page served by OpenSea at all?
# ---------------------------------------------------------------------------
# The structural test §8 and §18 both arrive at: a page the site really
# served is BUILT OUT OF ITS OWN ASSETS, and an interstitial — or Chromium's
# own network-error page, which carries `<title>opensea.io</title>` and would
# fool any title check — is not.
#
# Measured 2026-09-17 across five captures (a collection, the ranking, an
# item, a Solana collection and the site's own 404):
#
#   /_next/static/     1,963 to 2,329 on every one of them
#   seadn.io             309 to   634
#
# Note the 404 is in that list and passes, which is correct: OpenSea's "not
# found" IS an OpenSea page, and it is `empty` rather than `blocked` (§8 —
# blocked is not empty).
_SITE_ASSET_MARKERS = ("/_next/static/", "seadn.io", "i2c.seadn.io")
_SITE_ASSET_FLOOR = 2


def served_by_opensea(html: Optional[str]) -> bool:
    """Whether this document was built out of OpenSea's own assets."""
    if not html:
        return False
    hits = sum(html.count(marker) for marker in _SITE_ASSET_MARKERS)
    return hits >= _SITE_ASSET_FLOOR


# Vendor markers for a challenge. EVERY ONE OF THESE WAS COUNTED ON PAGES
# KNOWN TO BE GOOD FIRST (§18), on 2026-09-17, across the homepage, a
# collection page, an item page, a Solana collection page, a Japanese-locale
# collection page, the ranking and the site's own 404:
#
#   challenges.cloudflare.com     0 on all seven
#   cdn-cgi/challenge-platform    0 on all seven
#   __cf_chl                      0 on all seven
#   "Just a moment"               0 on all seven
#
# `cf-turnstile` is deliberately NOT here. It is the obvious marker for a
# Turnstile and it is measured useless in any repo that can reach the
# Scraping Browser, whose auto-solve extension injects
# `data-ts-input="cf-turnstile-response"` into every page it loads — 1
# occurrence on a SERVED page against 0 on a real challenge, in two sibling
# repos (§8, §19). With it gone, nothing in this set matches anything that
# extension injects, so this repo needs no extension-tag strip either; adding
# one would be dead code.
BOT_CHALLENGE_MARKERS = {
    "challenges.cloudflare.com": "cloudflare",
    "cdn-cgi/challenge-platform": "cloudflare",
    "__cf_chl": "cloudflare",
    "Just a moment": "cloudflare",
    "Attention Required!": "cloudflare",
    "/_Incapsula_Resource": "incapsula",
    "px-captcha": "perimeterx",
    "_pxhd": "perimeterx",
    "geo.captcha-delivery.com": "datadome",
    "awswaf.com": "aws-waf",
}

# How much of a document to unescape before matching. An edge can
# entity-escape the punctuation in its own markers — `errors&#46;edgesuite&#46;net`
# reaching a raw HTTP client and `errors.edgesuite.net` reaching a browser
# DOM, which is one marker with two spellings and a check that silently
# covers only one of them (§20). Unescaping a bounded PREFIX handles both
# without spending the cost on two megabytes of grid, and without a product
# name deep in a collection description reading as a marker.
_MARKER_SCAN_BYTES = 200_000


def _scannable(html: str) -> str:
    return html_module.unescape(html[:_MARKER_SCAN_BYTES])


def detect_block_marker(html: Optional[str]) -> Optional[str]:
    """The vendor whose interstitial this is, or None."""
    if not html:
        return None
    text = _scannable(html)
    for marker, vendor in BOT_CHALLENGE_MARKERS.items():
        if marker in text:
            return vendor
    return None


# Kept under the family's name: every engine imports `detect_bot_challenge`.
detect_bot_challenge = detect_block_marker


def is_challenge_page(html: Optional[str]) -> bool:
    """Whether this page is an interstitial rather than the site's own."""
    return detect_block_marker(html) is not None


# Chromium's own network-error page. Not an interstitial and not the site: it
# carries `<title>opensea.io</title>`, so a title check calls it a real page,
# and the only thing that reads it correctly is asking whether the document
# was built out of the site's assets (§18, proven on a third site here).
_BROWSER_ERROR_MARKERS = ("ERR_PROXY_CONNECTION_FAILED",
                          "ERR_TUNNEL_CONNECTION_FAILED",
                          "ERR_NAME_NOT_RESOLVED",
                          "ERR_CONNECTION_REFUSED",
                          "ERR_CONNECTION_TIMED_OUT",
                          "ERR_EMPTY_RESPONSE")


def is_browser_error_page(html: Optional[str]) -> bool:
    if not html:
        return False
    return any(marker in html for marker in _BROWSER_ERROR_MARKERS)


# The site's own way of saying a feed has nothing in it. An UNAMBIGUOUS
# positive signal — no interstitial carries it — so it is checked before any
# threshold (§17's classification-order trap).
#
# WHAT IS DELIBERATELY NOT IN THIS SET, and it is the §18 trap caught in the
# act. OpenSea's "not found" page reads `404: This page could not be found.`,
# which looks like the obvious marker for an unknown collection slug. Counted
# first on pages known to be good, as §18 requires: that sentence appears
# TWICE on every served page measured — the collection page, the ranking, an
# item page, a Solana collection and a Japanese-locale page alike — because
# Next.js ships its bundled not-found component inside every RSC payload.
# Adding it would have classified every page in the site as empty.
#
# So an unknown slug is recognised by its HTTP STATUS, which really is 404
# and which every engine has. The one case that leaves is a `--dump-html`
# capture of a 404 read back with no status beside it: that classifies as
# `shell`, waits, parses nothing and exits 4 — the same exit code as `empty`,
# which is why the gap is worth naming rather than closing with a marker that
# fires everywhere.
_EMPTY_STATE_MARKERS = ("No items found", "No activity yet",
                        "No results found", "Nothing here yet")


def looks_empty(html: Optional[str]) -> bool:
    if not html:
        return False
    text = _scannable(html)
    return any(marker in text for marker in _EMPTY_STATE_MARKERS)


def detect_page_state(html: Optional[str], status: Optional[int] = None,
                      url: str = "", mode: str = "items") -> str:
    """Which of the five states this response is.

    Ordered by how much each signal PROVES rather than by how cheap it is to
    check (§17): an inlined payload naming rows is proof the site served
    this page, and it is checked before any status code or threshold. The
    heuristic — "was this built out of OpenSea's assets?" — is last among the
    positives, because a minimal real page can carry fewer asset references
    than a fat one and a threshold must never be what turns a correct answer
    into exit 3.
    """
    if html is None or not html.strip():
        return "blocked"
    if is_challenge_page(html):
        return "challenge"
    if is_browser_error_page(html):
        return "blocked"
    if payload_answered(html, mode):
        return "content"
    if looks_empty(html):
        return "empty"
    if status == 404:
        # A real 404 from the site — a collection slug that does not exist.
        # The request was served exactly as asked and there is nothing on the
        # page: exit 4, not exit 3.
        return "empty"
    if status is not None and status >= 400:
        return "blocked"
    if count_cards(html) >= 2:
        # Rendered tiles but no readable payload. There is something to
        # parse, and the DOM fallback is what parses it.
        return "content"
    if served_by_opensea(html):
        # Served, and not painted yet. Wants a WAIT rather than a refetch.
        return "shell"
    return "blocked"


# ---------------------------------------------------------------------------
# The GraphQL route
# ---------------------------------------------------------------------------
# These are this scraper's own documents, written against the shapes the
# site's own bundle publishes and trimmed to the fields these rows need.
# Introspection is disabled on the endpoint (`INTROSPECTION_DISABLED`,
# measured 2026-09-17) but execution is open, and an unknown field comes back
# as a named validation error rather than as silence — which is what made
# writing these safe.
_PRICE_FRAGMENT = """
  usd
  token { unit symbol contractAddress chain { identifier } }
"""

ITEMS_QUERY = """
query OpenSeaScraperItems($slug: String!, $limit: Int!, $after: Cursor,
                          $sort: CollectionItemsSort!) {
  collectionItems(collectionSlug: $slug, limit: $limit, after: $after,
                  sort: $sort) {
    nextPageCursor
    items {
      id tokenId name imageUrl standard totalSupply
      contractAddress chain { identifier }
      collection { slug name }
      rarity { rank }
      attributes { traitType value }
      owner { address username }
      enforcement { isDelisted isCompromised }
      lastSale { %(price)s } lastSaleAt
      bestListing {
        pricePerItem { %(price)s }
        marketplace { identifier } endTime quantityRemaining
      }
      bestOffer { pricePerItem { %(price)s } }
    }
  }
}
""" % {"price": _PRICE_FRAGMENT}

COLLECTIONS_QUERY = """
query OpenSeaScraperRankings($slug: CollectionRankingSlug!, $timeframe: Timeframe!,
                             $limit: Int!, $after: Cursor) {
  collectionRankings(slug: $slug, timeframe: $timeframe, limit: $limit,
                     after: $after) {
    nextPageCursor
    items {
      score
      collection {
        slug name imageUrl isVerified createdAt
        chain { identifier }
        floorPrice { pricePerItem { %(price)s } }
        topOffer { pricePerItem { %(price)s } }
        stats {
          ownerCount sales totalSupply listedItemCount
          volume { usd native { unit symbol } }
          oneHour { sales floorPriceChange volume { usd native { unit symbol } } }
          oneDay { sales floorPriceChange volume { usd native { unit symbol } } }
          sevenDays { sales floorPriceChange volume { usd native { unit symbol } } }
          thirtyDays { sales floorPriceChange volume { usd native { unit symbol } } }
        }
      }
    }
  }
}
""" % {"price": _PRICE_FRAGMENT}

ACTIVITY_QUERY = """
query OpenSeaScraperActivity($slug: String!, $limit: Int!, $after: Cursor,
                             $filter: CollectionActivityFilterInput) {
  collectionActivity(collectionSlug: $slug, limit: $limit, after: $after,
                     filter: $filter) {
    nextPageCursor
    items {
      id type eventTime quantity
      from { address username }
      to { address username }
      chain { identifier }
      collection { slug }
      price { %(price)s }
      item {
        tokenId name imageUrl contractAddress
        chain { identifier }
      }
    }
  }
}
""" % {"price": _PRICE_FRAGMENT}

QUERIES = {"items": ITEMS_QUERY, "collections": COLLECTIONS_QUERY,
           "activity": ACTIVITY_QUERY}

# What `--sort` accepts, and what the endpoint calls it. The four valid
# values were established by sending each candidate and reading which came
# back as a validation error (2026-09-17): PRICE, CREATED_DATE, RARITY and
# LAST_SALE are accepted; LISTING_DATE, TOKEN_ID, RARITY_RANK, BEST_OFFER,
# SALE_DATE, LAST_TRANSFER_DATE and OFFER_PRICE are not.
SORTS = {
    "price": ("PRICE", "ASC"),
    "price-desc": ("PRICE", "DESC"),
    "created": ("CREATED_DATE", "ASC"),
    "newest": ("CREATED_DATE", "DESC"),
    "rarity": ("RARITY", "ASC"),
    "last-sale": ("LAST_SALE", "DESC"),
}
DEFAULT_SORT = "price"

# The ranking's own axes. `slug` picks WHICH ranking and `timeframe` the
# window its volume and floor-change figures cover.
RANKING_SLUGS = ("TRENDING", "TOP")
TIMEFRAMES = {"1h": "ONE_HOUR", "1d": "ONE_DAY", "7d": "SEVEN_DAYS",
              "30d": "THIRTY_DAYS", "all": "ALL_TIME"}
DEFAULT_TIMEFRAME = "1d"

# Which rolling-stats block a timeframe reads, for the volume columns.
_STATS_WINDOW = {"ONE_HOUR": "oneHour", "ONE_DAY": "oneDay",
                 "SEVEN_DAYS": "sevenDays", "THIRTY_DAYS": "thirtyDays"}

ACTIVITY_FILTERS = {
    "all": None,
    "sales": ["SALE"],
    "listings": ["LISTING"],
    "offers": ["OFFER", "COLLECTION_OFFER", "TRAIT_OFFER"],
    "transfers": ["TRANSFER"],
    "mints": ["MINT"],
}
DEFAULT_ACTIVITY_FILTER = "all"


# WHAT EACH PAGE RENDERS BEFORE ANYONE TOUCHES A CONTROL, and it is the one
# thing about this site that cost a working run to find.
#
# Page 1 of a run is a NAVIGATION, and the rows it yields are the ones the
# SITE chose to render — under the SITE's ordering and the SITE's filter.
# Page 2 onwards are this scraper's own query, under whatever `--sort`,
# `--timeframe` or `--activity` was asked for. Where those two disagree, the
# output would be two different samples in one file, and the cursor page 1
# handed over is not even valid for the other ordering: a `--sort created`
# run passing the page's price cursor was answered `Invalid cursor` and
# reported partial (measured on mad-lads, 2026-09-17, which is exactly the
# second-collection run §15 asks for).
#
# So the engines compare, and where the request does not match the page they
# take page 1 as proof-of-service and totals only, then start the feed from
# the endpoint. Each of these was MEASURED rather than read off the UI:
#
#   items        the bundle's own prefetch calls
#                `{limit: PAGE_SIZE, sort: {by: "PRICE", direction: "ASC"}}`
#   collections  the ranking page's inlined rows matched TRENDING + ONE_DAY
#                on all six leading slugs, and no other combination of the
#                two ranking slugs and five timeframes matched any of them
#   activity     the feed page inlines 32 SALE rows; the same query with no
#                filter came back 32 OFFER rows in the same minute, so the
#                page is filtered to sales and `--activity all` is a
#                different question
SSR_REQUEST = {
    "items": {"sort": "price"},
    "collections": {"ranking": "TRENDING", "timeframe": "1d"},
    "activity": {"activity_filter": "sales"},
}


def ssr_matches_request(mode: str, *, sort: str = DEFAULT_SORT,
                        ranking: str = "TRENDING",
                        timeframe: str = DEFAULT_TIMEFRAME,
                        activity_filter: str = DEFAULT_ACTIVITY_FILTER
                        ) -> bool:
    """Whether page 1's own rows answer the question this run is asking."""
    wanted = SSR_REQUEST.get(mode)
    if not wanted:
        return False
    given = {"sort": sort, "ranking": ranking, "timeframe": timeframe,
             "activity_filter": activity_filter}
    return all(given.get(key) == value for key, value in wanted.items())


def ssr_mismatch_note(mode: str, **given) -> str:
    """Why page 1's rows are being left out of this run."""
    wanted = SSR_REQUEST.get(mode, {})
    asked = ", ".join(f"{k}={given.get(k)!r}" for k in wanted)
    renders = ", ".join(f"{k}={v!r}" for k, v in wanted.items())
    return (f"Page 1 renders {renders} and this run asked for {asked}, so its "
            f"fifty rows are a different sample and its cursor belongs to the "
            f"other ordering — passing that cursor back is answered `Invalid "
            f"cursor`. The navigation still happens (it is what proves the "
            f"site served us, sets the cookies the feed rides on and carries "
            f"the collection's totals); the rows come from the endpoint under "
            f"the ordering that was asked for, and every row in the file is "
            f"then one sample.")


def graphql_body(mode: str, *, slug: str = "", limit: int = MAX_LIMIT,
                 after: Optional[str] = None, sort: str = DEFAULT_SORT,
                 ranking: str = "TRENDING", timeframe: str = DEFAULT_TIMEFRAME,
                 activity_filter: str = DEFAULT_ACTIVITY_FILTER) -> dict:
    """The JSON body for one page of `mode`.

    Built here rather than in the engines so all three send byte-identical
    requests — a query that differed between engines would mean one of them
    reporting columns its twins cannot (§6).
    """
    if mode not in QUERIES:
        raise ValueError(f"no GraphQL query for mode {mode!r}")
    limit = max(1, min(int(limit), MAX_LIMIT))
    variables: Dict[str, Any] = {"limit": limit}
    if after:
        variables["after"] = after
    if mode == "items":
        by, direction = SORTS.get(sort, SORTS[DEFAULT_SORT])
        variables["slug"] = slug
        variables["sort"] = {"by": by, "direction": direction}
    elif mode == "collections":
        variables["slug"] = ranking
        variables["timeframe"] = TIMEFRAMES.get(timeframe,
                                                TIMEFRAMES[DEFAULT_TIMEFRAME])
    else:
        variables["slug"] = slug
        types = ACTIVITY_FILTERS.get(activity_filter)
        variables["filter"] = {"activityTypes": types} if types else None
    return {"query": QUERIES[mode], "variables": variables}


def graphql_errors(payload: Any) -> List[str]:
    """The error messages in a GraphQL response, as strings.

    The endpoint answers HTTP 200 with an `errors` array rather than a 4xx,
    so a caller that only checks the status code reads a failure as an empty
    page. That is exactly the "fail loudly" case (§8).
    """
    if not isinstance(payload, dict):
        return []
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return []
    return [str((e or {}).get("message", e))[:300] for e in errors]


def graphql_data(payload: Any, mode: str = "items") -> Optional[dict]:
    """The `data.{field}` object out of a GraphQL response, or None."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    for field in PAYLOAD_FIELDS.get(mode, ()):
        value = data.get(field)
        if value:
            return value
    return None


# The cursor OpenSea hands back is base64 of a small JSON array whose first
# element is the sort key of the last row on the page:
#
#   items, sorted by price   [6.89, "35f3f2a7-7c78-37eb-8a12-0af57eba4eb1"]
#   items, sorted by created ["2021-04-24T10:21:17Z", 99, "99b822d6-…"]
#   the collections ranking  [50]
#
# When a price-ordered walk crosses from listed items into unlisted ones that
# first element becomes `null`, and the request that passes such a cursor
# back answers `{"errors":[{"message":"Something went wrong"}]}`. Measured on
# boredapeyachtclub, 2026-09-17: three pages of 100, 282 of the 300 listed,
# then the null key and the error.
#
# Recognising it HERE, before spending the request, is what turns that from
# a run-ending error into a clean stop: the run has every item that has a
# price, which is what a price-ordered walk is for.
def cursor_key_is_null(cursor: Optional[str]) -> bool:
    """Whether this cursor's sort key is null — the end of the listed items."""
    if not cursor:
        return False
    import base64
    try:
        decoded = base64.b64decode(cursor + "=" * (-len(cursor) % 4))
        parsed = json.loads(decoded)
    except Exception:  # noqa: BLE001 — an unreadable cursor is not a null one
        return False
    return isinstance(parsed, list) and bool(parsed) and parsed[0] is None


# ---------------------------------------------------------------------------
# Normalising — one set of builders, both routes
# ---------------------------------------------------------------------------
def _clean(value: Any) -> Any:
    """An empty string becomes None; everything else passes through.

    OpenSea publishes `"symbol": ""` for a token it has not named — 11 of 50
    last sales on one capture — and an empty string written into a currency
    column reads as a currency (§8: never present a guess as a fact, and an
    empty string is a guess that something was there).
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _number(value: Any) -> Optional[float]:
    """A float, or None. Never a string and never a silent zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    number = _number(value)
    return int(number) if number is not None else None


def _price(node: Any) -> Tuple[Optional[float], Optional[str], Optional[float]]:
    """(amount, currency, usd) out of any of the site's price shapes.

    Handles `{token:{unit,symbol},usd}` and `{pricePerItem:{…}}` alike,
    because the site uses both and which one it uses depends on whether the
    price belongs to an ORDER (a listing has a price per item) or to an EVENT
    (a sale has a price).
    """
    if not isinstance(node, dict):
        return None, None, None
    if isinstance(node.get("pricePerItem"), dict):
        node = node["pricePerItem"]
    token = node.get("token")
    token = token if isinstance(token, dict) else {}
    amount = _number(token.get("unit"))
    if amount is None:
        native = node.get("native")
        if isinstance(native, dict):
            amount = _number(native.get("unit"))
    return amount, _clean(token.get("symbol")), _number(node.get("usd"))


def _chain_of(node: Any) -> Optional[str]:
    if isinstance(node, dict):
        chain = node.get("chain")
        if isinstance(chain, dict):
            return _clean(chain.get("identifier"))
    return None


def _profile(node: Any) -> Tuple[Optional[str], Optional[str]]:
    """(address, username) out of a profile object."""
    if not isinstance(node, dict):
        return None, None
    return (_clean(node.get("address")),
            _clean(node.get("username")) or _clean(node.get("displayName")))


def _traits(node: Any) -> Optional[List[str]]:
    if not isinstance(node, list):
        return None
    out = []
    for attribute in node:
        if not isinstance(attribute, dict):
            continue
        trait, value = attribute.get("traitType"), attribute.get("value")
        if trait is None and value is None:
            continue
        out.append(f"{trait}: {value}")
    return out or None


def item_row(node: Any, *, url_locale: Optional[str] = None,
             data_source: str = "graphql", page: int = 1,
             position: int = 1, source: str = SOURCE_DEFAULT) -> Optional[Item]:
    """One `Item` out of the site's own item object.

    The same object shape comes from `collectionItems.items[]`, from
    `itemByIdentifier` on a single item's page, and from the inlined state of
    either — which is why there is one builder rather than three (§21).
    """
    if not isinstance(node, dict):
        return None
    chain = _chain_of(node)
    contract = _clean(node.get("contractAddress"))
    token_id = _clean(node.get("tokenId"))
    # On Solana the payload sets `tokenId` to the MINT ADDRESS — the same
    # string as `contractAddress`, measured byte-identical on every item of
    # the mad-lads capture (2026-09-17). Written through, that produces
    # `/item/solana/{mint}/{mint}`, an address the site does not serve, and a
    # `sku` claiming a token id that does not exist. A Solana NFT has no
    # token id within a contract: the mint IS the token, so the column is
    # null and the address is the two-segment form the site publishes.
    if token_id and contract and token_id == contract:
        token_id = None
    collection = node.get("collection") if isinstance(node.get("collection"), dict) else {}

    listing = node.get("bestListing") if isinstance(node.get("bestListing"), dict) else {}
    price, currency, price_usd = _price(listing)
    offer_amount, offer_currency, offer_usd = _price(node.get("bestOffer"))
    sale_amount, sale_currency, sale_usd = _price(node.get("lastSale"))
    marketplace = listing.get("marketplace") if isinstance(listing.get("marketplace"), dict) else {}
    rarity = node.get("rarity") if isinstance(node.get("rarity"), dict) else {}
    enforcement = node.get("enforcement") if isinstance(node.get("enforcement"), dict) else {}
    owner_address, owner_username = _profile(node.get("owner"))

    return Item(
        source=source,
        url=item_url(chain, contract, token_id, url_locale),
        sku=sku_for(chain, contract, token_id),
        title=_clean(node.get("name")),
        price=price, currency=currency, price_usd=price_usd,
        best_offer=offer_amount, best_offer_currency=offer_currency,
        best_offer_usd=offer_usd,
        last_sale=sale_amount, last_sale_currency=sale_currency,
        last_sale_usd=sale_usd, last_sale_at=_clean(node.get("lastSaleAt")),
        listing_marketplace=_clean(marketplace.get("identifier")),
        listing_expires_at=_clean(listing.get("endTime")),
        listing_quantity=_clean(listing.get("quantityRemaining")),
        chain=chain, contract_address=contract, token_id=token_id,
        token_standard=_clean(node.get("standard")),
        collection_slug=_clean(collection.get("slug")),
        collection_name=_clean(collection.get("name")),
        rarity_rank=_int(rarity.get("rank")),
        traits=_traits(node.get("attributes")),
        image_url=_clean(node.get("imageUrl")),
        owner_address=owner_address, owner_username=owner_username,
        is_delisted=enforcement.get("isDelisted"),
        is_compromised=enforcement.get("isCompromised"),
        data_source=data_source, page=page, position=position,
    )


def collection_row(node: Any, *, timeframe: str = "ONE_DAY",
                   url_locale: Optional[str] = None,
                   data_source: str = "graphql", page: int = 1,
                   position: int = 1, rank: Optional[int] = None,
                   source: str = SOURCE_DEFAULT) -> Optional[Collection]:
    """One `Collection` out of a `collectionRankings.items[]` entry."""
    if not isinstance(node, dict):
        return None
    score = _number(node.get("score"))
    collection = node.get("collection")
    if not isinstance(collection, dict):
        # A ranking entry with no collection under it: skipped rather than
        # written as a row of nulls with a rank.
        return None
    slug = _clean(collection.get("slug"))
    stats = collection.get("stats") if isinstance(collection.get("stats"), dict) else {}
    window = stats.get(_STATS_WINDOW.get(timeframe, "oneDay"))
    window = window if isinstance(window, dict) else {}

    floor, floor_currency, floor_usd = _price(collection.get("floorPrice"))
    offer, offer_currency, offer_usd = _price(collection.get("topOffer"))

    # ALL_TIME reads the collection's lifetime volume; every other timeframe
    # reads its own rolling window. Mixing the two is how a "24h volume"
    # column ends up holding an all-time figure.
    if timeframe == "ALL_TIME" or not window:
        volume_node = stats.get("volume")
        sales = _int(stats.get("sales"))
        floor_change = None
    else:
        volume_node = window.get("volume")
        sales = _int(window.get("sales"))
        floor_change = _number(window.get("floorPriceChange"))
    volume_node = volume_node if isinstance(volume_node, dict) else {}
    native = volume_node.get("native") if isinstance(volume_node.get("native"), dict) else {}

    return Collection(
        source=source,
        url=collection_url_for(slug, url_locale) if slug else "",
        sku=slug, title=_clean(collection.get("name")),
        price=floor, currency=floor_currency, price_usd=floor_usd,
        top_offer=offer, top_offer_currency=offer_currency,
        top_offer_usd=offer_usd,
        floor_change=floor_change,
        volume=_number(native.get("unit")),
        volume_usd=_number(volume_node.get("usd")),
        volume_window=timeframe,
        sales=sales,
        owners=_int(stats.get("ownerCount")),
        total_supply=_int(stats.get("totalSupply")),
        rank=rank, score=score,
        chain=_chain_of(collection),
        contract_address=_clean(collection.get("contractAddress")),
        category=_clean(collection.get("category")),
        verified=collection.get("isVerified"),
        image_url=_clean(collection.get("imageUrl")),
        data_source=data_source, page=page, position=position,
    )


def activity_row(node: Any, *, url_locale: Optional[str] = None,
                 data_source: str = "graphql", page: int = 1,
                 position: int = 1,
                 source: str = SOURCE_DEFAULT) -> Optional[Activity]:
    """One `Activity` out of a `collectionActivity.items[]` entry."""
    if not isinstance(node, dict):
        return None
    item = node.get("item") if isinstance(node.get("item"), dict) else {}
    chain = _chain_of(node) or _chain_of(item)
    contract = _clean(item.get("contractAddress"))
    token_id = _clean(item.get("tokenId"))
    collection = node.get("collection") if isinstance(node.get("collection"), dict) else {}
    amount, currency, usd = _price(node.get("price"))
    from_address, _ = _profile(node.get("from"))
    to_address, _ = _profile(node.get("to"))

    # `type` where the endpoint gives one, `__typename` where only the
    # inlined state does: a server-rendered activity row arrives as
    # `"__typename": "Sale"` and a queried one as `"type": "SALE"`. Reading
    # only the first leaves the column null on every SSR row.
    event_type = _clean(node.get("type"))
    if not event_type:
        typename = _clean(node.get("__typename"))
        if typename:
            event_type = re.sub(r"(?<!^)(?=[A-Z])", "_", typename).upper()

    return Activity(
        source=source,
        url=item_url(chain, contract, token_id, url_locale),
        sku=_clean(node.get("id")),
        title=_clean(item.get("name")),
        event_type=event_type,
        event_time=_clean(node.get("eventTime")),
        price=amount, currency=currency, price_usd=usd,
        quantity=_clean(node.get("quantity")),
        from_address=from_address, to_address=to_address,
        chain=chain, contract_address=contract, token_id=token_id,
        collection_slug=_clean(collection.get("slug")),
        image_url=_clean(item.get("imageUrl")),
        data_source=data_source, page=page, position=position,
    )


_ROW_BUILDER = {"items": item_row, "collections": collection_row,
                "activity": activity_row}


def rows_from_nodes(nodes: Sequence[Any], mode: str, *, url: str = "",
                    data_source: str = "graphql", page: int = 1,
                    first_position: int = 1, timeframe: str = "ONE_DAY",
                    first_rank: Optional[int] = None) -> List[Any]:
    """Rows for a list of the site's own objects.

    `page` and `first_position` are threaded rather than defaulted, because
    `position` restarts at 1 on every page: without the page number beside it
    a row from page 2 claims the same position as one from page 1 and the two
    are indistinguishable in the output (§18).
    """
    builder = _ROW_BUILDER.get(mode)
    if builder is None or not isinstance(nodes, (list, tuple)):
        return []
    locale = locale_of(url)
    source = source_of(url) if url else SOURCE_DEFAULT
    rows = []
    for offset, node in enumerate(nodes):
        kwargs = {"url_locale": locale, "data_source": data_source,
                  "page": page, "position": first_position + offset,
                  "source": source}
        if mode == "collections":
            kwargs["timeframe"] = timeframe
            kwargs["rank"] = (first_rank + offset) if first_rank else None
        row = builder(node, **kwargs)
        if row is not None:
            rows.append(row)
    return rows


def rows_from_graphql(payload: Any, url: str = "", *, mode: str = "items",
                      page: int = 1, first_position: int = 1,
                      timeframe: str = "ONE_DAY",
                      first_rank: Optional[int] = None) -> List[Any]:
    """Rows out of a GraphQL response body.

    Raises nothing on an error response — the caller reads `graphql_errors`
    for that and decides. Returning [] here and letting the caller log the
    messages keeps "the endpoint said no" distinguishable from "the feed is
    empty", which is the whole of §8's blocked/empty/partial distinction in
    one function.
    """
    data = graphql_data(payload, mode)
    if data is None:
        return []
    nodes = data.get("items") if isinstance(data, dict) else None
    if nodes is None and isinstance(data, dict):
        nodes = [data]          # itemByIdentifier: one object, not a feed
    return rows_from_nodes(nodes or [], mode, url=url, data_source="graphql",
                           page=page, first_position=first_position,
                           timeframe=timeframe, first_rank=first_rank)


# ---------------------------------------------------------------------------
# The DOM fallback
# ---------------------------------------------------------------------------
def rows_from_dom(html: str, url: str, *, mode: str = "items", page: int = 1,
                  first_position: int = 1) -> List[Any]:
    """Rows built from the rendered anchors alone.

    Runs ONLY when the inlined state did not parse, and it is deliberately
    thin: a rendered OpenSea tile carries an address and a name and no
    machine-readable price — the prices in the markup are formatted display
    text inside build-hashed elements, which is exactly what §4 says never to
    anchor on. So these rows carry what the URL proves and leave every price
    null, with `data_source="dom"` saying so.

    That is a worse row than the payload gives, and it is a much better
    outcome than zero rows with a green exit: a run that drops to this path
    reports it, and `page_flow.parsed_nothing_from_a_served_page` catches the
    case where even this finds nothing.
    """
    if mode == "collections":
        pattern = r'href="(/(?:[a-zA-Z-]+/)?collection/[^"/?#]+)"'
    else:
        pattern = r'href="(/(?:[a-zA-Z-]+/)?item/[^"?#]+)"'
    seen: List[str] = []
    for path in re.findall(pattern, html or ""):
        if path not in seen:
            seen.append(path)
    if not seen:
        return []

    soup = BeautifulSoup(html or "", "html.parser")
    names: Dict[str, str] = {}
    for anchor in soup.select('a[href]'):
        href = anchor.get("href") or ""
        text = anchor.get_text(" ", strip=True)
        if href in seen and text and href not in names:
            names[href] = text

    locale = locale_of(url)
    source = source_of(url) if url else SOURCE_DEFAULT
    rows: List[Any] = []
    for offset, path in enumerate(seen):
        absolute = f"https://opensea.io{path}"
        position = first_position + offset
        if mode == "collections":
            slug = collection_slug_from_url(absolute)
            rows.append(Collection(source=source, url=absolute, sku=slug,
                                   title=names.get(path), data_source="dom",
                                   page=page, position=position))
        else:
            parts = item_parts_from_url(absolute)
            chain, contract, token_id = parts if parts else (None, None, None)
            rows.append(Item(source=source, url=absolute,
                             sku=sku_for(chain, contract, token_id),
                             title=names.get(path), chain=chain,
                             contract_address=contract, token_id=token_id,
                             collection_slug=collection_slug_from_url(url),
                             data_source="dom", page=page, position=position))
    if locale:
        for row in rows:
            row.url = row.url.replace("https://opensea.io/",
                                      f"https://opensea.io/{locale}/", 1)
    return rows


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------
def parse_rows(html: str, url: str, *, page: int = 1, mode: str = "items",
               first_position: int = 1, timeframe: str = "ONE_DAY",
               first_rank: Optional[int] = None) -> List[Any]:
    """Every row on this page, inlined state first and the DOM as fallback.

    The order is the one §4 prescribes with the site's own data in place of
    JSON-LD: the structured source carries every row regardless of what has
    painted, and the rendered markup is what is left when it does not parse.
    """
    field, value = ssr_field(html, mode)
    if field == "itemByIdentifier":
        rows = rows_from_nodes([value], "items", url=url, data_source="ssr",
                               page=page, first_position=first_position)
        if rows:
            return rows
    elif field:
        nodes = (value or {}).get("items") or []
        rows = rows_from_nodes(nodes, mode, url=url, data_source="ssr",
                               page=page, first_position=first_position,
                               timeframe=timeframe, first_rank=first_rank)
        if rows:
            return rows
    return rows_from_dom(html, url, mode=mode, page=page,
                         first_position=first_position)


# Kept under the family's names so the engines and anything written against
# a sibling repo keep importing successfully.
parse_products = parse_rows
parse_items = parse_rows


def row_class_for(mode: str):
    return ROW_CLASS_BY_MODE.get(mode, Item)
