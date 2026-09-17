# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

OpenSea changing its markup is the normal way this stops working, and it
has its own issue template. The detail that saves the most time is WHICH of
the three read paths broke, because this repo has three and they fail
differently.

**Path 1 — the inlined Next.js payload.** Every page ships its own data in
React Server Components chunks:

```
self.__next_f.push([1, "…\"outcomePrices\":[\"0.12\",\"0.88\"]…"])
```

Decoded and concatenated, those chunks contain the site's own market objects
verbatim — slug, numeric id, `outcomePrices`, volumes, and on an event page
the on-chain `conditionId` and both CLOB token ids. Every healthy run reads
this and nothing else.

If the push pattern moves, a run does NOT fail. It drops to the tiles, and
then every row is EVENT-level: keyed by the event slug, priced from the
tile's rounded percentage, with a rounded volume and no market id at all.
The `data_source` column is what shows it — every row reads `dom` where it
used to read `flight` or `flight+jsonld`.

Two things about that payload are worth knowing before you touch the
scanner. It is **not JSON**: Next.js inlines the site's own bootstrap script
into the same stream, and that script contains a brace inside a
single-quoted string. A scanner that tracks double-quoted strings only —
which is what a JSON scanner tracks — desyncs there and loses every object
after it. That is a real regression this repo shipped for an afternoon, and
`test_payload_decoding` pins the capture that caught it. And an event page
ships the markets of everything in its **rails** as well as its own, so the
parser scopes rows to the event the URL asked for; without that, a run for a
single-market event returned twenty-one rows.

**Path 2 — JSON-LD.** A listing publishes `CollectionPage → ItemList` with
one `Event` node per tile, each carrying ONE price and the currency. It is
used to confirm the payload's price (recorded as `data_source="flight+jsonld"`)
and to read the currency, which the payload never states. An EVENT page
publishes a different `@type` whose `offers.price` is `"0"` for markets
trading at 0.07 and 0.91 — a placeholder, and deliberately not read.

**Path 3 — the rendered tiles.** Anchored on the `/event/{slug}` href and
never on a class name: the classes here are Tailwind utilities. The tile
scope widens to the outermost ancestor still covering exactly ONE event,
counting distinct event slugs rather than links — a tile links its event two
or three times.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once —
   including its WARNING branch, which is what runs when a bare GitHub
   runner's datacentre address is refused and no `OPENSEA_PROXY` secret is set.
   This canary needs no secret to do real work: eight of fourteen fetches
   were served in full with no key and no proxy, from a DATACENTRE address
   at that. What it has NOT been measured doing is getting past
   Cloudflare from a shared datacentre address, and since the challenge here
   tracks the address's recent request rate, a runner is the worst case for
   it. That is exactly why a block there is a warning rather than a failure —
   until you set `OPENSEA_PROXY`, after which it is a failure, because then it
   means something.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

Twelve properties in this repo exist because they were once absent, or
because they cost a sibling repo real time. Tests pin all twelve, so a PR
that breaks one will fail rather than silently regress:

- **`sku` is the item's ADDRESS, not the API's id.**
  `{chain}/{contract}/{token_id}`, which is what OpenSea's own URL carries,
  what survives in a `--dump-html` capture and what a reader can paste into a
  browser. The API's `id` is a 32-hex hash that appears nowhere a human can
  see.
- **A Solana item has no token id, and its address has two segments.** The
  payload sets `tokenId` to the MINT ADDRESS — byte-identical to
  `contractAddress` — so writing it through produces `/item/solana/{mint}/
  {mint}`, an address the site does not serve, on every row at 100% coverage.
- **A price is a TOKEN amount and the token is named.** `ETH`, `WETH`, `SOL`,
  `USDC`. Never a defaulted "USD", and never an empty string: OpenSea
  publishes `"symbol": ""` for a token it has not named — 11 of 50 rows on
  one capture — and an empty string in a currency column reads as a currency.
- **`price_usd` is the site's conversion and lives in its own column.** It
  moves with the token every minute, which is why `diff_runs.py` does not
  track it.
- **The ORDERING decides which rows are in the file.** Under `--sort price`
  the cursor carries the last row's price and the walk ends where the listed
  items do (282 of 9,998 on one collection, matching OpenSea's own
  `listedItemCount` exactly). `listed_items_exhausted` is therefore a
  COMPLETE stop reason, and `diff_runs.py` refuses two runs whose orderings
  differ — they are two samples, not two snapshots.
- **Page 1's rows are only kept when the run asks the page's own question.**
  The site renders items by price, the ranking as TRENDING over one day, and
  the activity feed as sales. A run asking for anything else drops those rows
  and starts the feed from the endpoint: keeping them would merge two
  samples, and page 1's cursor is answered `Invalid cursor` under another
  ordering.
- **One normaliser reads both routes.** The inlined urql state and the
  GraphQL endpoint return the same objects — 49 of 50 rows identical across
  18 stable columns when it was checked — so `parse_rows` and
  `rows_from_graphql` share every row builder and cannot drift.
- **`page_url()` returns None and `--concurrency` above 1 is refused.** There
  is no address for page 2, and refusing the flag with the reason is better
  than running one worker and letting it look like the flag did something.
- **A marker that matches every good page is not a marker.** Every marker in
  `BOT_CHALLENGE_MARKERS` is asserted ABSENT from all six good-page fixtures.
  `cf-turnstile` is deliberately not among them (2Captcha's own Scraping
  Browser extension injects a `cf-turnstile-response` hunter into every page
  it loads), and neither is OpenSea's own "404: This page could not be
  found." — Next.js ships that sentence inside every RSC payload, twice, on
  every page the site serves.
- **A page that was SERVED, links to items and parses to zero rows is OUR
  bug.** It gets its own stop reason (`parser_found_nothing`) so a reader
  goes to `product_parser.py` rather than checking their URL for a typo.
- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows, `5` remote API error, `6` partial. A
  pipeline branches on these.

Two more that are about the fixtures rather than the code:

- **A fixture is CUT from a real capture and proven to parse identically**,
  column for column, AND to classify the same way after trimming — that
  second check is what catches a trim that took the site's own asset
  references with it and turned a good page into a `blocked` one. Never
  hand-written.
- **Profile handles and avatar URLs are replaced; wallet addresses are
  not.** An OpenSea handle is a real person's chosen public name and a busy
  capture carries dozens; an address is the row's id and public on-chain
  data, and a fixture with fake addresses could not check that `sku` is built
  correctly. The leak scan runs over every fixture anyway, against PATTERNS
  rather than the literals of one capture.

There is also a naming check: certain phrases are banned repo-wide and the suite
fails naming them. If it trips, read the message — the phrase is wrong for a
reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha
classifier and the CLI contract against committed fixtures. If yours
genuinely needs opensea.io, say in the PR what you ran, which URL and
page kind, from which exit, and what you got — including the row count, the
`data_source` breakdown the run prints, and the sidecar's `total_events`.
A datacentre address is fine here: every measurement in this repo was taken
from one, and the site served `curl/8.0` the full page. Market counts differ
by listing and change through the day, so a bare "worked for me" is not
reproducible.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: the first live run of the pyppeteer engine crashed on its
FIRST fetch on a signature mismatch that four separate offline checks and 400
green assertions had not caught.

Do not add anything that signs in, connects a wallet or places an order.
This project reads public pages as an anonymous visitor and nothing else; a
token proved valid by trading with real money is not a result worth having.

## Scope

This repo scrapes **public pages** on OpenSea: listing pages, search
listings and event pages, exactly as an anonymous visitor is served them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
