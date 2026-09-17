"""Cut the offline suite's fixtures out of real captures, and PROVE they
parse the same.

Its output is `fixtures_generated.json`, which `smoke_test.py` loads. This
script is shipped because two files point at it — `smoke_test.py`'s own
docstring and TROUBLESHOOTING.md — and an instruction pointing at a file that
does not exist is worse than no instruction.

WHAT YOU NEED TO RUN IT
-----------------------
Your own captures, in `../captures/` relative to the repo, named as `SOURCES`
below expects. They are deliberately NOT in the repository: a collection page
is 1.2 MB and there are eight of them.

Take them with a real browser — `--dump-html` on any engine writes exactly
the bytes the parser was given:

    python3 playwright_scraper.py \\
        --url https://opensea.io/collection/boredapeyachtclub \\
        --dump-html ../captures/collection_bayc.html

Take at least one of each PAGE KIND, because each mode reads a different
inlined operation, and at least one collection on a NON-EVM CHAIN, because
a Solana item's address has two segments where an Ethereum one has three and
a regex written against either shape silently drops the other.

WHAT IT ENFORCES, and why each rule is here
-------------------------------------------
  * every fixture is CUT from a real capture, never hand-written. The one
    thing in a sibling repo that WAS hand-written — a guess at the site's
    "nothing matched" copy — matched none of the real strings, and an empty
    result came back as `shell` and spent a 25-second readiness wait on an
    answer the site had already given;
  * each one is verified to parse IDENTICALLY to the original for the rows it
    keeps — every column, not just a count;
  * the trimmed fixture must still CLASSIFY the same way, which is what
    catches a trim that dropped the site's own asset references and turned a
    good page into a `blocked` one;
  * the expectations `smoke_test.py` asserts are computed HERE, from the real
    capture, and written into the fixture file. A hand-typed expectation is a
    guess about the site; a computed one is a record of it.

WHAT IS NOT VERBATIM, and why (§10)
------------------------------------
Two kinds of field are replaced with obvious placeholders before anything
else happens:

    a profile's display name / username   ->  "opensea-user-{n}"
    a profile's avatar image URL          ->  "https://example.invalid/avatar"

An OpenSea profile handle is a real person's chosen public name, and a
capture of a busy collection carries dozens of them. The site showing them on
its own page is one thing; this repository republishing them is a separate
act, and the checks need the STRUCTURE of an owner rather than the person
(§10). The scrub runs on the ORIGINAL before the trim, so what the tests
compare against is the scrubbed page and the two cannot disagree.

WALLET ADDRESSES ARE KEPT, deliberately. An address is the row's id, it is
public on-chain data that this scraper's whole output is about, and a fixture
with fake addresses could not check that `sku` is built correctly. The
LEAK_PATTERNS scan below still runs over every fixture, because the next
capture may carry something neither of these rules anticipated and the guard
has to be a PATTERN rather than a memory of what was clean last time.

What is otherwise reduced is volume: a feed's `items` array is trimmed to the
first few rows and the page's markup furniture (styles, inline SVG, the other
scripts) is dropped, so a 1.2 MB capture becomes a fixture small enough to
commit. The values that survive are the site's own, byte for byte.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict

import product_parser as P

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CAPTURES = os.path.abspath(os.path.join(REPO_ROOT, "..", "captures"))
OUT_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")

# name -> (capture file, the URL it was taken from, mode, rows to keep)
# `None` for the count means "keep the feed whole": an item page is one row
# already, and the two negative pages carry no rows at all.
SOURCES = {
    "collection": ("collection_bayc.html",
                   "https://opensea.io/collection/boredapeyachtclub",
                   "items", 6),
    "collection_solana": ("collection_madlads_solana.html",
                          "https://opensea.io/collection/mad-lads",
                          "items", 6),
    "collection_ja": ("collection_bayc_ja.html",
                      "https://opensea.io/ja/collection/boredapeyachtclub",
                      "items", 4),
    "ranking": ("ranking.html", "https://opensea.io/collections",
                "collections", 6),
    "activity": ("activity_bayc.html",
                 "https://opensea.io/collection/boredapeyachtclub/activity",
                 "activity", 6),
    "item": ("item_bayc.html",
             "https://opensea.io/item/ethereum/"
             "0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d/1", "items", None),
    "notfound": ("notfound.html",
                 "https://opensea.io/collection/this-collection-does-not-exist-xyz",
                 "items", None),
    "chromium_proxy_error": ("chromium_proxy_error.html",
                             "https://opensea.io/collection/boredapeyachtclub",
                             "items", None),
    # THE SAME collection page, fetched over the 2Captcha Scraping Browser.
    # It is here because a guard is only as good as the fixture it runs
    # against (§21): the check that `cf-turnstile` must never be a marker
    # would otherwise run only against pages this repo fetched with a local
    # browser, which carry no extension injection at all — and would pass for
    # the wrong reason. This one carries the injection, measured.
    "cdp_scraping_browser": ("cdp_scraping_browser.html",
                             "https://opensea.io/collection/boredapeyachtclub",
                             "items", 6),
}

# Fixtures the suite treats as pages the site really served.
GOOD_PAGES = ("collection", "collection_solana", "collection_ja", "ranking",
              "activity", "item", "cdp_scraping_browser")


# ---------------------------------------------------------------------------
# The scrub (§10)
# ---------------------------------------------------------------------------
# Applied to the ORIGINAL capture, before anything is trimmed or measured, so
# the expectations written into the fixture file describe the scrubbed page
# and cannot disagree with it.
_USERNAME_KEYS = ("username", "displayName")
_AVATAR_RE = re.compile(r'https://i2c\.seadn\.io/profiles/[^"\\]*')
_AVATAR_PLACEHOLDER = "https://example.invalid/avatar"


def scrub(text: str) -> str:
    """Replace profile handles and avatar URLs with obvious placeholders."""
    seen: dict = {}

    def _name(match):
        key, value = match.group(1), match.group(2)
        if not value:
            return match.group(0)
        if value not in seen:
            seen[value] = f"opensea-user-{len(seen) + 1}"
        return f'"{key}":"{seen[value]}"'

    text = re.sub(r'"(%s)":"((?:[^"\\]|\\.)*)"' % "|".join(_USERNAME_KEYS),
                  _name, text)
    return _AVATAR_RE.sub(_AVATAR_PLACEHOLDER, text)


# ---------------------------------------------------------------------------
# The trim
# ---------------------------------------------------------------------------
_PUSH_RE = re.compile(
    r'(urql_transport"\)\]\s*\?\?=\s*\[\]\)\.push\()(\{.*?\})(\)</script>)',
    re.S)

_DROPPABLE = (
    # The React Server Components flight payload. It is the single biggest
    # thing on an OpenSea page — about a third of a 1.2 MB capture — and this
    # parser reads none of it: the rows come out of the urql transport push
    # and the anchors come out of the rendered DOM, both of which are
    # separate scripts and separate elements. Dropped here so a fixture is
    # small enough to commit; `build` re-parses and re-classifies afterwards,
    # which is what proves the drop cost nothing.
    (re.compile(r"<script>self\.__next_f\.push\(.*?\)</script>", re.S), ""),
    (re.compile(r"<style\b[^>]*>.*?</style>", re.S | re.I), ""),
    (re.compile(r"<svg\b[^>]*>.*?</svg>", re.S | re.I), ""),
    (re.compile(r"<noscript\b[^>]*>.*?</noscript>", re.S | re.I), ""),
    (re.compile(r"<!--.*?-->", re.S), ""),
    (re.compile(r'\s(?:srcSet|srcset|sizes|imagesrcset)="[^"]*"'), ""),
)


def _trim_feed(html: str, mode: str, keep: int) -> str:
    """Cut every inlined feed down to `keep` rows, leaving everything else."""
    if keep is None:
        return html

    def _replace(match):
        try:
            payload = json.loads(match.group(2))
        except ValueError:
            return match.group(0)
        changed = False
        for entry in (payload.get("rehydrate") or {}).values():
            data = (entry or {}).get("data")
            if not isinstance(data, dict):
                continue
            for field in P.PAYLOAD_FIELDS.get(mode, ()):
                value = data.get(field)
                if isinstance(value, dict) and isinstance(value.get("items"), list):
                    if len(value["items"]) > keep:
                        value["items"] = value["items"][:keep]
                        changed = True
        if not changed:
            return match.group(0)
        return (match.group(1)
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + match.group(3))

    return _PUSH_RE.sub(_replace, html)


def _strip_furniture(html: str) -> str:
    """Drop markup that carries no data this parser reads.

    NOT the `<script>` tags: the inlined state lives in one, and this site's
    rows come out of it. Dropping the site's own asset references is the one
    thing that would break a fixture silently, so `build` re-classifies every
    trimmed fixture and fails if the answer moved.
    """
    for pattern, replacement in _DROPPABLE:
        html = pattern.sub(replacement, html)
    return re.sub(r"\n{3,}", "\n\n", html)


# ---------------------------------------------------------------------------
# The leak scan (§10)
# ---------------------------------------------------------------------------
# PATTERNS rather than the literals of a capture that happened to be clean:
# the next capture is the one that will carry something.
LEAK_PATTERNS = (
    # A 2Captcha key is 32 hex characters, and so is half the identifier
    # space on this site — item ids, collection ids and every seadn.io image
    # path segment. Matching the SHAPE alone produced three false positives
    # on the first capture tried, including Solana's system program address
    # (`1111…1111`) and a CDN path, so this matches the shape IN A
    # CREDENTIAL'S POSITION instead: named as a key, or carried as one in a
    # query string. That is the only place a real key would appear.
    (re.compile(r"(?i)(?:clientkey|api[_-]?key|\bkey)\s*[:=]\s*[\"']?"
                r"([0-9a-f]{32})\b"),
     "a 32-hex string in a key's position, which is the shape of a "
     "2Captcha API key"),
    (re.compile(r"(?i)[?&](?:key|clientkey|token)=[0-9a-zA-Z]{16,}"),
     "a credential in a query string"),
    (re.compile(r"(?i)\b(?:api[_-]?key|clientkey|access[_-]?token|"
                r"secret|password)\b\s*[:=]\s*[\"'][^\"']{8,}"),
     "something named like a credential with a value on it"),
    (re.compile(r"(?i)\bauthorization\b\s*[:=]\s*[\"']?(?:bearer|basic)\s"),
     "an Authorization header"),
    (re.compile(r"https://i2c\.seadn\.io/profiles/"),
     "a profile avatar URL the scrub should have replaced"),
    (re.compile(r"[a-z][a-z0-9+.\-]*://[^\s/@\"']+:[^\s/@\"']+@"),
     "a URL with credentials in it"),
)

# OpenSea's own ids ARE 32-hex, and they are data rather than secrets: an
# item's `id`, a collection's `id`, an order's `id`, and every on-chain
# address in the payload. The first pattern above would fire on every one of
# them, so those keys are excused BY NAME — which is narrower than excusing
# the shape, and keeps the check able to catch a loose 32-hex string that is
# not one of them.
_ID_KEYS_RE = re.compile(
    r'"(?:id|collectionId|orderId|accountId|address|contractAddress|'
    r'tokenId|transactionHash|mintAddress)":\s*"[0-9a-zA-Z]{20,}"')

# And one more exclusion that is a fact about blockchains rather than about
# this site: Solana's system program is `1111…1111`, 32 characters of a
# single digit, and it appears as a currency address on every Solana
# collection. A key with two bits of entropy is not a key, so the scan
# ignores any candidate built from fewer than four distinct characters —
# which no real credential is.
def _looks_random(value: str) -> bool:
    return len(set(value)) >= 4


def scan_for_leaks(name: str, text: str) -> list:
    """Anything in `text` that should not be committed. Empty when clean."""
    stripped = _ID_KEYS_RE.sub('"id":"<id>"', text)
    found = []
    for pattern, description in LEAK_PATTERNS:
        for match in pattern.finditer(stripped):
            if not _looks_random(match.group(0)):
                continue
            found.append(f"{name}: {description} — {match.group(0)[:60]!r}")
            break
    return found


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def _rows(html: str, url: str, mode: str) -> list:
    return P.parse_rows(html, url, page=1, mode=mode)


def _expectations(rows: list, html: str, url: str, mode: str) -> dict:
    """The VALUES `smoke_test.py` pins, computed from the real capture.

    Values, not coverage: a column can be 100% populated and entirely wrong,
    and a sibling repo shipped a `review_count` of 445279961 on every row
    with a coverage check reading 100% (§10).
    """
    first = asdict(rows[0]) if rows else {}
    first.pop("scraped_at", None)
    return {
        "rows": len(rows),
        "state": P.detect_page_state(html, None, url, mode),
        "cards": P.count_cards(html),
        "served": P.served_by_opensea(html),
        "next_cursor_present": bool(P.next_cursor(html, mode)),
        "totals": P.collection_totals(html),
        "first_row": first,
        "skus": [r.sku for r in rows],
        "with_price": sum(1 for r in rows if r.price is not None),
        "data_sources": sorted({r.data_source for r in rows}),
    }


def build(name: str, filename: str, url: str, mode: str, keep) -> dict:
    path = os.path.join(CAPTURES, filename)
    if not os.path.exists(path):
        raise SystemExit(
            f"missing capture {path}\nTake it with:\n"
            f"  python3 playwright_scraper.py --url {url} "
            f"--dump-html ../captures/{filename}")
    with open(path, encoding="utf-8", errors="replace") as f:
        original = scrub(f.read())

    trimmed = _strip_furniture(_trim_feed(original, mode, keep))

    # 1. The trimmed fixture must parse IDENTICALLY to the original for the
    #    rows it keeps — every column, not just a count.
    before = _rows(original, url, mode)
    after = _rows(trimmed, url, mode)
    kept = len(after)
    if name in GOOD_PAGES and not after:
        raise SystemExit(f"{name}: the trimmed fixture parses to no rows")
    for index, (a, b) in enumerate(zip(before[:kept], after)):
        left, right = asdict(a), asdict(b)
        left.pop("scraped_at"), right.pop("scraped_at")
        if left != right:
            differing = {k for k in left if left[k] != right[k]}
            raise SystemExit(
                f"{name}: row {index} parses differently after trimming "
                f"({sorted(differing)}). The trim dropped something the "
                f"parser reads.")

    # 2. It must still CLASSIFY the same way. This is what catches a trim
    #    that took the site's own asset references with it and turned a good
    #    page into a `blocked` one.
    before_state = P.detect_page_state(original, None, url, mode)
    after_state = P.detect_page_state(trimmed, None, url, mode)
    if before_state != after_state:
        raise SystemExit(
            f"{name}: classified {before_state!r} before trimming and "
            f"{after_state!r} after. The trim changed what the page IS.")

    leaks = scan_for_leaks(name, trimmed)
    if leaks:
        raise SystemExit("\n".join(["refusing to write fixtures:"] + leaks))

    print(f"  {name:22} {len(original):>9,} -> {len(trimmed):>8,} bytes  "
          f"{kept} row(s), state={after_state}")
    return {"url": url, "mode": mode, "html": trimmed,
            "expect": _expectations(after, trimmed, url, mode)}


def main() -> int:
    if not os.path.isdir(CAPTURES):
        raise SystemExit(f"no captures directory at {CAPTURES} — see this "
                         f"file's docstring for how to take one")
    print(f"Reading captures from {CAPTURES}")
    fixtures = {name: build(name, *source) for name, source in SOURCES.items()}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(fixtures, f, ensure_ascii=False, indent=1, sort_keys=True)
    size = os.path.getsize(OUT_PATH)
    print(f"Wrote {OUT_PATH} ({size:,} bytes, {len(fixtures)} fixtures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
