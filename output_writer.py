"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Three modes, three row classes
------------------------------
    --mode items        /collection/{slug}  — one row per NFT in a
                        collection: its listing price, its best offer, its
                        last sale, its traits and its owner
    --mode collections  /collections — one row per COLLECTION in OpenSea's
                        own ranking: floor price, volume windows, owners,
                        supply
    --mode activity     /collection/{slug}/activity — one row per EVENT:
                        a sale, a listing, an offer, a transfer or a mint

Unlike most of this family, these are three genuinely different KINDS of
thing rather than three views of one, so they get three dataclasses (§9's
allowance, which amazon-scraper spends on `Review`). What they share is the
family prefix — `source`, `scraped_at`, `url`, `sku`, `title` — byte-
identical and in the same order in all three, so one column name works
across the family and across the modes. `diff_runs.py` refuses to compare
two runs whose `mode` differs, because their columns are not the same
columns.

`sku` in each of the three
--------------------------
    items        "{chain}/{contract}/{token_id}" — the item's own address on
                 OpenSea, minus the /item/ prefix. NOT the API's `id` (a
                 32-hex hash), because the address is the one form that
                 survives in a `--dump-html` capture, appears in the rendered
                 markup, and can be pasted into a browser.
    collections  the collection slug (`boredapeyachtclub`), which is what
                 every OpenSea URL and every GraphQL variable keys on.
    activity     the event id the site gives each activity row. Dedupe is on
                 THIS and not on the item: a collection's feed shows the same
                 token being listed, offered and sold, and keying those on
                 the token would drop two of the three.

A note on the chain, because it changes the shape of an address
---------------------------------------------------------------
An EVM item is `/item/ethereum/{contract}/{token_id}` — three segments. A
Solana item is `/item/solana/{mint_address}` — two, because a Solana NFT is
one mint address and has no separate token id within a contract. Measured on
two captures taken 2026-09-17: 32 distinct item links on an Ethereum
collection page, all three-segment; 32 on a Solana one, all two-segment. So
`token_id` is null on a Solana row and `contract_address` carries the mint,
and any consumer joining on the pair must read `chain` first. `sku` is built
from whichever shape the site used, so it stays unique either way.

Columns the family has and these rows do not, with the measurement:

    brand           an NFT has a creator and a collection, both of which have
                    their own columns; neither is a brand in the retail sense
                    and calling one that would invite a join across repos
                    that does not mean anything.
    original_price  no discount chain. A listing is a price the owner set,
                    not a price marked down from another one. What a reader
                    of this data actually compares against is `last_sale`
                    and `best_offer`, which are columns of their own.
    discount_pct    same reason — and a computed one here would be a claim
                    about a market rather than about a price tag.
    rating          OpenSea publishes no rating on an item. It publishes a
                    RARITY RANK, which is a different thing and has its own
                    column on `Item`.
    in_stock        an NFT is not stock. `listing_marketplace` being non-null
                    is what "you can buy this right now" means here, and
                    `is_delisted` says when OpenSea has hidden it.

The currency is a TOKEN SYMBOL, not ISO 4217
--------------------------------------------
`currency` on these rows is `ETH`, `WETH`, `SOL`, `USDC` — what the price is
actually denominated in — and never a defaulted "USD". The dollar figure the
site computes beside it is `price_usd`, in its own column, so a consumer can
tell an exact on-chain amount from a conversion that moves with the market.
§4's rule reads the same here as it does with fiat: the structured source
names the currency, so it is a fact and is never overwritten from the DOM.
"""
import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The host a row came from. OpenSea serves one host and seven locale PATHS
# under it (`/ja/collection/…`), not seven hosts, so this genuinely does not
# vary — it is here because the family's first column is `source` and a
# consumer reading six of these repos reads it in every one.
SOURCE_DEFAULT = "opensea.io"


@dataclass
class Item:
    """One NFT, as `--mode items` reads it."""
    # --- the family prefix, byte-identical and in order across the family ---
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # The item's own page: /item/{chain}/{contract}/{token_id}, or
    # /item/{chain}/{mint} on Solana.
    url: str = ""
    # "{chain}/{contract}/{token_id}". See the module docstring.
    sku: Optional[str] = None
    # The item's name as the collection sets it — "#3428", "Bored Ape #1".
    # Frequently just the token id with a hash in front, which is the
    # collection's choice and not a failed read.
    title: Optional[str] = None

    # --- the price ---------------------------------------------------------
    # What it costs to buy this item RIGHT NOW, in the token the listing is
    # denominated in. Null when the item is not listed, which is the normal
    # state for most of a collection: measured on Bored Ape Yacht Club on
    # 2026-09-17, 282 of the first 300 items under the site's own price
    # ordering carried a listing and the rest did not.
    price: Optional[float] = None
    currency: Optional[str] = None
    # The site's own dollar conversion of `price`, which moves with the token
    # even when the listing does not. A separate column precisely so the two
    # are never confused (§8: never present a guess as a fact — this one is
    # the site's guess, clearly labelled).
    price_usd: Optional[float] = None
    # The highest standing bid, same three-column shape. An offer is what
    # someone else will pay; a listing is what the owner asks.
    best_offer: Optional[float] = None
    best_offer_currency: Optional[str] = None
    best_offer_usd: Optional[float] = None
    # What it last changed hands for, and when (ISO 8601 UTC).
    last_sale: Optional[float] = None
    last_sale_currency: Optional[str] = None
    last_sale_usd: Optional[float] = None
    last_sale_at: Optional[str] = None

    # --- the listing itself ------------------------------------------------
    # Which marketplace the standing listing is on. OpenSea aggregates, so
    # `blur` or `looksrare` here is a real answer rather than a bad read.
    listing_marketplace: Optional[str] = None
    listing_expires_at: Optional[str] = None
    listing_quantity: Optional[str] = None

    # --- the token ---------------------------------------------------------
    chain: Optional[str] = None
    contract_address: Optional[str] = None
    # Null on Solana, where the mint address IS the token. See the docstring.
    token_id: Optional[str] = None
    token_standard: Optional[str] = None
    collection_slug: Optional[str] = None
    collection_name: Optional[str] = None
    # OpenSea's own rarity rank within the collection, where it publishes
    # one: 1 is the rarest. Null on collections it has not ranked.
    rarity_rank: Optional[int] = None
    # "Background: New Punk Blue" per entry, in the site's own order.
    traits: Optional[List[str]] = None
    image_url: Optional[str] = None

    # --- who holds it ------------------------------------------------------
    owner_address: Optional[str] = None
    owner_username: Optional[str] = None

    # --- what OpenSea says about it ----------------------------------------
    # OpenSea's own enforcement flags. `is_delisted` means it has hidden the
    # item from its marketplace (a reported theft, usually); `is_compromised`
    # flags the account. Both are the site's assertion, carried through
    # rather than acted on.
    #
    # NEITHER HAS EVER BEEN SEEN TRUE, and §20 says to write that down rather
    # than let a column look verified because it is always populated.
    # Measured 2026-09-17: 3,400 items — 1,000 under the price ordering
    # across ten collections, and 2,400 under the created ordering across
    # four — came back `False` on both, with not one True.
    #
    # The price-ordered half of that proves nothing on its own and is the
    # §20 mistake made deliberately, then corrected: an item with a live
    # listing is by definition not hidden, so looking for a delisting among
    # listed items is looking where it cannot be. The created-ordered scan is
    # the one that counts, and it found none either.
    #
    # So the open question, unresolved: either OpenSea excludes hidden items
    # from `collectionItems` altogether — which would make this column a
    # constant and, by §9, one that should not exist — or they are simply
    # rarer than 2,400 items of four blue-chip collections. A `False` here is
    # therefore LESS PROVEN than a `True` would be, and a consumer should not
    # read it as "OpenSea has checked and cleared this item".
    is_delisted: Optional[bool] = None
    is_compromised: Optional[bool] = None

    # --- provenance --------------------------------------------------------
    # WHICH route built this row. Never a guess presented as a fact, and
    # `diff_runs.py` reports a difference that comes with a `data_source`
    # difference as `source_changed` rather than as a change (§8).
    #
    #   ssr      the page's own server-rendered state — page 1 of a run
    #   graphql  the site's own GraphQL endpoint, which is what pages 2..N
    #            come from, and what a --transport api run uses throughout
    #   dom      the rendered tile alone, which carries an address and a name
    #            and no prices — the fallback for a page whose state did not
    #            parse
    data_source: Optional[str] = None
    page: Optional[int] = None
    position: Optional[int] = None


@dataclass
class Collection:
    """One collection, as `--mode collections` reads it."""
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # The slug: `boredapeyachtclub`.
    sku: Optional[str] = None
    title: Optional[str] = None

    # The FLOOR: the cheapest item you can buy in this collection, in the
    # collection's own native token. `price` rather than `floor_price` so the
    # family's column means the same thing here as everywhere else — the
    # lowest price one unit costs right now.
    price: Optional[float] = None
    currency: Optional[str] = None
    price_usd: Optional[float] = None
    # The highest standing collection-wide offer: what someone will pay for
    # ANY item in the collection.
    top_offer: Optional[float] = None
    top_offer_currency: Optional[str] = None
    top_offer_usd: Optional[float] = None
    # The site's own floor change over the window this run ranked by, as a
    # fraction (0.05 is +5%). Null rather than 0 when the site does not
    # publish one for that window.
    floor_change: Optional[float] = None

    # --- what the venue counts --------------------------------------------
    # Volume in the collection's native token and in USD, over the window the
    # ranking used; `volume_window` says WHICH, because a one-hour figure and
    # an all-time one are both correct and not comparable.
    volume: Optional[float] = None
    volume_usd: Optional[float] = None
    volume_window: Optional[str] = None
    sales: Optional[int] = None
    owners: Optional[int] = None
    total_supply: Optional[int] = None
    # Where this collection sat in the ranking this run read, 1-based, and
    # the score the ranking sorted on. Both belong to the RANKING rather than
    # to the collection: the same collection has a different rank under a
    # different timeframe, which is why `rank` is a column and the timeframe
    # is in the sidecar.
    rank: Optional[int] = None
    score: Optional[float] = None

    chain: Optional[str] = None
    contract_address: Optional[str] = None
    category: Optional[str] = None
    verified: Optional[bool] = None
    image_url: Optional[str] = None

    data_source: Optional[str] = None
    page: Optional[int] = None
    position: Optional[int] = None


@dataclass
class Activity:
    """One event out of a collection's feed, as `--mode activity` reads it."""
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # The ITEM's page, because that is the only address an event has: OpenSea
    # publishes no per-event URL.
    url: str = ""
    # The event id. See the module docstring for why dedupe keys on this and
    # not on the item.
    sku: Optional[str] = None
    # The item's name, so the family's `title` is readable in a spreadsheet.
    title: Optional[str] = None

    # SALE, LISTING, OFFER, TRAIT_OFFER, COLLECTION_OFFER, TRANSFER, MINT,
    # CANCELLATION — as the site names them, not remapped.
    event_type: Optional[str] = None
    event_time: Optional[str] = None
    # What the event was denominated in. A TRANSFER has no price, which is a
    # fact about transfers and not a failed read.
    price: Optional[float] = None
    currency: Optional[str] = None
    price_usd: Optional[float] = None
    quantity: Optional[str] = None

    # Both sides, where the event has two. A listing has a maker and no
    # taker; a mint has no `from`.
    from_address: Optional[str] = None
    to_address: Optional[str] = None

    chain: Optional[str] = None
    contract_address: Optional[str] = None
    token_id: Optional[str] = None
    collection_slug: Optional[str] = None
    image_url: Optional[str] = None

    data_source: Optional[str] = None
    page: Optional[int] = None
    position: Optional[int] = None


# Which class a mode yields. Read by `finish_run` so an empty CSV still
# carries the header of the mode that produced it, and by diff_runs.py.
ROW_CLASS_BY_MODE = {"items": Item, "collections": Collection,
                     "activity": Activity}

# Kept under the family's name so that code shared with the siblings — and
# anything a user wrote against one of them — keeps importing successfully.
# `Item` is the default row here because `--mode items` is the default mode.
Product = Item

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. All three qualify, but for different reasons:
# an item and a collection appear once each by construction, and an activity
# row is keyed on the EVENT id rather than on the token it concerns, which is
# what makes a feed showing one token three times three distinct rows.
UNIQUE_BY_SKU_MODES = ("items", "collections", "activity")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages.
    What it guards here is the cursor walk: OpenSea pages its feeds with an
    opaque cursor, and a cursor that is replayed — because a page was
    retried from a fresh browser after a challenge, say — hands back rows
    the run already has. First row wins, in page order, merged after every
    page has landed.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")

def is_deeper(new_row: Any, old_row: Any) -> bool:
    """Whether `new_row` is the same thing read from a richer page.

    False on this site, always, and that is a property of the modes rather
    than a stub. Nowhere here does a run read one key twice from two pages of
    different depth: a collection's items arrive once each from one paginated
    feed, a ranking names each collection once, and an activity feed gives
    every event its own id. The upgrade path `merge_pages` offers is real and
    a sibling repo needs it — a listing naming markets shallowly and their
    own pages naming them deeply — and here there is nothing for it to do.

    Kept rather than removed because `merge_pages` is shared family code and
    calls it; what would be wrong is inventing a depth signal to make it look
    used. If a `--mode item` ever lands here, reading one token's own page
    for the columns a grid does not carry, this is where its rule goes.
    """
    return False

def merge_pages(groups: Sequence[Any], key: str = "sku") -> tuple:
    """Merge per-page row groups into one list, in PAGE order.

    `groups` is [(page_num, rows), …] and is sorted here rather than trusted.
    Pages really are sequential on this site — page N+1's cursor comes out of
    page N — so this costs nothing and keeps the family's contract: dedupe
    that mutates a running set inside the loop makes the OUTPUT depend on
    which page finished first (§8).

    The upgrade-in-place rule the family carries is inert here — see
    `is_deeper`, which is False on every mode this repo has, because no run
    reads one key twice at two depths. What matters on this site is the
    ORDER: the site's own ranking (a ranked collections table, a price-
    ordered item grid) is the only ordering information there is, and a
    merge in arrival order would quietly destroy it the first time a page
    was retried.

    Returns (rows, new_per_page, upgraded_per_page).
    """
    rows: List[Any] = []
    position_of: dict = {}
    new_per_page: dict = {}
    upgraded_per_page: dict = {}
    for page_num, group in sorted(groups, key=lambda g: g[0]):
        new_count = upgraded = 0
        for row in group:
            value = getattr(row, key, None)
            if value is None:
                # Nothing to check a duplicate against, and dropping it would
                # be a silent data loss rather than a duplicate removal.
                rows.append(row)
                new_count += 1
                continue
            if value not in position_of:
                position_of[value] = len(rows)
                rows.append(row)
                new_count += 1
            elif is_deeper(row, rows[position_of[value]]):
                rows[position_of[value]] = row
                upgraded += 1
        new_per_page[page_num] = new_count
        upgraded_per_page[page_num] = upgraded
    return rows, new_per_page, upgraded_per_page


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On this site this code specifically does NOT cover the two ways to get a
# page with nothing on it: a collection slug that does not exist, which
# answers a real HTTP 404 carrying the site's own "not found" page; and a
# collection that exists and has no items listed under the filter asked for.
# Both are EXIT_NO_PRODUCTS — the request was served exactly as asked and
# simply has nothing on it. Reporting either as blocked would send a reader
# hunting for a proxy problem that does not exist.
#
# What DOES belong here on opensea.io is Cloudflare's managed challenge.
# Nothing was refused while this repo was built — see the README for the
# addresses and the dates — so this path is implemented and unexercised
# here, which is a fact about this repo's testing rather than a claim about
# the site or about what a solver can do (§19).
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "topic", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` is recorded because on this site it decides the ROW CLASS, not
    just which columns are filled: `items` yields an Item, `collections` a
    Collection and `activity` an Activity, and the three share only the
    family prefix. diff_runs.py refuses a pair whose modes differ, which
    here is not a nicety — a diff across two of them would have no column in
    common to compare. `source` is recorded for the family's shape; on this
    site it is `opensea.io` on every row, because the seven locales are
    PATHS under one host rather than hosts of their own.

    `extra` carries facts about the run that are not about any single row.
    This repo puts the ORDERING there — `sort` and, for a ranking, the
    `timeframe` — because on OpenSea the ordering decides WHICH rows are in
    the file rather than what order they sit in: the site's own item
    ordering is by price and reaches only the listed items (see
    COMPLETE_STOP_REASONS), while `--sort created` walks the whole
    collection. Two runs under different sorts are different samples of the
    same collection, so diff_runs.py refuses that pair too.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the correct output.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected outcome.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# Three of these are the family's and one is this site's own arithmetic.
#
# "cursor_exhausted" is the ordinary end of a feed here: OpenSea pages every
# feed with an opaque cursor and hands back a null one when there is nothing
# after the page just read. That is the site saying "this is all of it", and
# it is the cleanest completion signal any repo in this family has — no
# selector, no convention, no guess (§7's layer 3, with the site doing the
# arithmetic).
#
# "listed_items_exhausted" is the one that is specific to OpenSea and is
# COMPLETE rather than partial, which is the most consequential line in this
# file. Under the site's own item ordering — by price, which is what a
# collection page shows and what "scrape the floor" means — the cursor
# encodes the last row's price, so it reads `[6.89, "35f3f2a7-…"]`. When the
# walk crosses from listed items into unlisted ones that key becomes
# `[null, "005db456-…"]` and the NEXT request answers `Something went wrong`
# (measured on boredapeyachtclub, 2026-09-17: 300 items over three pages,
# 282 of them listed, then that error). The run stops there and reports
# complete, because it has every item that HAS a price and the rest have
# none: there is nothing further to get under this ordering. `--sort created`
# walks the whole collection instead and was measured going 800 items deep
# without a stumble on the same collection.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted",
                         "cursor_exhausted", "listed_items_exhausted",
                         "no_new_products")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "topic", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are genuinely different things and a pipeline branches on
        # them (§8: blocked is not empty is not partial):
        #
        #   blocked          something stood between the run and the content
        #   did not complete we never reached the site — a dead proxy, a
        #                    load timeout, a refused batch
        #   completed        we asked, and the site's reply was nothing
        #
        # The middle one used to fall through to EXIT_NO_PRODUCTS, and that
        # was measured rather than reasoned about in a sibling repo: an
        # unreachable proxy produced exit 4 — "ran fine, found nothing" — on
        # a feed with hundreds of rows, while the sidecar beside it said
        # `status: failed`, `pages_completed: 0`. A consumer branching on the
        # exit code, which is what this family says exit codes are for, would
        # have recorded an empty catalogue.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Failed run: 0 of {pages_requested} page(s) were "
                  f"fetched ({stop_reason}). This is NOT an empty result — "
                  f"nothing was read from the site at all.")
            return EXIT_PARTIAL
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
