# Troubleshooting

Things that look like bugs and are not, and the ones that are. Each entry
says what was MEASURED rather than what is likely, so you can tell which is
which.

If something here does not match what you are seeing, re-run with
`--dump-html out.html` and open an issue with the dump attached — page 1 is
written as HTML and every cursor page after it as `out.html.pageN.json`,
which is exactly what the parser was given.

---

## "It returned 350 rows and stopped, but the collection has ten thousand items"

**That is the ordering, and it is the right answer to the question you
asked.** `--sort price` is the default because it is what a collection page
shows and what "scrape the floor" means. Under it OpenSea's cursor carries
the last row's price, so the walk climbs the price ladder and stops where the
LISTED items end — everything past that point has no price to sort by.

Measured on `boredapeyachtclub`, 2026-09-17, three numbers that all agreed:

| | |
|---|---|
| rows the walk wrote | 350 |
| rows carrying a price | 282, and exactly a prefix of the file |
| OpenSea's own `listedItemCount` | 282 |
| the collection's `totalSupply` | 9,998 |

The run reports `status: complete` and `stop_reason:
listed_items_exhausted`, and the sidecar carries `collection_totals` so the
gap between "complete" and "exhaustive" is visible rather than implied.

To walk the collection itself:

```bash
python3 playwright_scraper.py --collection boredapeyachtclub \
    --sort created --pages 20
```

`--sort created` was measured going 800 items deep on the same collection
without a stumble.

---

## "Most rows have `price: null`"

Expected under any sort but `price`. Most of a collection is not for sale:
282 of 9,998 Bored Apes and 93 of 9,968 Mad Lads were listed when this was
written. `price` is what it costs to buy the item right now, and an unlisted
item has no such number.

What a row still carries when it is unlisted: `last_sale`, `best_offer`,
`rarity_rank`, `traits` and `owner_address`. If you want only the ones you
can buy, use the default sort and take the rows up to the first null.

---

## "`--sort created` threw away my first page"

On purpose, and the run says so:

```
[INFO] Page 1 renders sort='price' and this run asked for sort='created', so
       its fifty rows are a different sample and its cursor belongs to the
       other ordering — passing that cursor back is answered `Invalid
       cursor`.
```

Page 1 is the page OpenSea rendered, under OpenSea's ordering. Keeping its
rows would put two different samples in one file, and its cursor really is
invalid for another ordering — that error is what the first version of this
scraper got, on its first run against a second collection. The navigation
still happens: it proves the site served us, sets the cookies the feed rides
on and carries the collection's totals. `--pages` still means pages of ROWS,
so nothing is lost but the duplicate sample, and `rows_dropped_from_page_1`
in the sidecar records it.

The same applies to `--mode collections` under a `--timeframe` or `--ranking`
other than the page's own (`TRENDING`, one day), and to `--mode activity`
under any `--activity` but `sales`.

---

## "`python3 -c 'import urllib.request…'` gets a 403"

That is the single sharpest thing to know about this site, and it is not
about you.

Measured 2026-09-17 from one address, in one minute:

| User-Agent | HTML route | GraphQL endpoint |
|---|---|---|
| `Python-urllib/3.13` | **403** | **403** |
| `python-requests/2.32` | 200 | 200 |
| `curl/8.x` | 200 | 200 |
| none at all | 200 | 200 |
| `HeadlessChrome/140` | 200 | 200 |
| `opensea-scraper/0.1` (made up) | 200 | 200 |

Cloudflare has a rule against that one signature. Send any other User-Agent —
including one you invent — and the same request is served. None of the
engines in this repo is affected; they all send a real browser's UA.

---

## "Everything came back exit 3 (blocked)"

Read the log line under it: the run says how many bytes came back and whether
the document referenced OpenSea's own asset hosts.

| what you see | what it is |
|---|---|
| `challenges.cloudflare.com` or `Just a moment` in the dump | Cloudflare's managed challenge. `--solve-captcha when-blocked` (the default) hands it to 2Captcha if a widget is actually rendered; a page with no widget is reported unsolved rather than charged for |
| `ERR_PROXY_CONNECTION_FAILED` and `<title>opensea.io</title>` | **Chromium's own error page**, not the site. Your proxy is dead. The title carries the site's hostname, which is exactly why this scraper asks whether the document was built out of the site's own assets instead of trusting a title — a 252 KB error page with zero `/_next/static/` references is the fixture that pins it |
| `with no reference to the site's own asset hosts` and no marker | something between you and the site returned a page that is not OpenSea's |

Nothing refused this scraper while it was built, from a plain datacentre
address. So a refusal is more likely to be your request RATE than your
address: raise `--delay` before reaching for `--proxy-file`.

---

## "0 rows, and the log says `parser_found_nothing`"

That one is **this repo's bug, not an empty collection**, and it is reported
separately for exactly that reason:

```
[ERROR] 0 rows parsed from a page the site SERVED, which links to 32 item(s).
        That is a parser regression rather than an empty collection: the
        inlined state's shape or the anchor pattern has changed.
```

The page was served, it links to items, and neither the inlined state nor the
rendered anchors produced a row. Open an issue with the `_debug.html` it
saved. An empty collection reports 0 rows WITHOUT that line, and a slug that
does not exist answers a real HTTP 404 and reports `empty`.

---

## "A Solana row has no `token_id`"

Correct. A Solana NFT is one mint address and has no token id within a
contract, so its OpenSea address has two segments
(`/item/solana/{mint}`) where an EVM item has three
(`/item/ethereum/{contract}/{token_id}`).

`contract_address` carries the mint, `token_id` is null, and `sku` is
`solana/{mint}`. Join on `sku`, or read `chain` before pairing the other two.

OpenSea's own payload sets `tokenId` to the mint address on those rows — the
same string as `contractAddress` — so a parser that writes it through
produces an address the site does not serve, on every row, at 100% coverage.
This one is worth knowing if you write your own.

---

## "`last_sale_currency` is null but `last_sale` has a number"

OpenSea did not name the token that sale settled in. Measured on one capture:
11 of 50 rows, against 25 in ETH and 14 in WETH — the site publishes
`"symbol": ""` with a real contract address behind it.

It becomes `null` here rather than `""`, because an empty string in a
currency column reads as a currency. Use `last_sale_usd` when you need the
figures to be comparable.

---

## "`--concurrency 4` printed a refusal"

It did, with the reason:

```
[WARNING] --concurrency 4 is refused: OpenSea paginates every feed with an
          opaque cursor rather than with a page number … so there is no
          address to hand a second worker.
```

Page 5's request does not exist until page 4 has been read. This is the one
place OpenSea is strictly worse than a site with page numbers, and refusing
the flag is better than running one worker and letting it look like the flag
did something. For throughput, run several collections at once, one process
each — that is the same parallelism against independent feeds.

---

## "The Japanese page gave me different data"

It should not, beyond the wording. A locale path (`/ja/collection/{slug}`)
changes what the page SAYS, not the ids, the prices or the ordering: a
locale run and an English one produced the same 50 ids with the same prices
on the same collection.

The `url` column follows the locale you asked for, so join two locales on
`sku` rather than on `url`.

---

## "`python3 env_config.py` says my key is a placeholder"

Because it still is. A value carrying a `{…}` placeholder is treated as
UNSET, deliberately:

```
OPENSEA_CDP_ENDPOINT still contains the placeholder {login} from
.env.example — treating it as unset.
```

`cp .env.example .env` gives you the SHAPE of each credential, not a working
one. Without that rule the run would connect to `cb.2captcha.com` with the
literal string `{login}-zone-…` as its username and get a 401 a long way
from its cause.

Scraping Browser profile credentials live about a day, so a `ws://` endpoint
you pasted yesterday is stale today — get a fresh one from the dashboard
rather than debugging the old one.

---

## "Selenium cannot use my proxy / my CDP endpoint"

Both are real Selenium limits, not bugs here, and both are reported rather
than silently swallowed:

* **`--proxy` credentials cannot be sent.** `--proxy-server` accepts an
  address only, and there is no Selenium equivalent of pyppeteer's
  `page.authenticate`. They are stripped with a warning.
* **An authenticated `--cdp-endpoint` cannot be used at all.**
  chromedriver's `debuggerAddress` is a bare `host:port` with nowhere to put
  a password, while Playwright's `connect_over_cdp` and pyppeteer's
  `browserWSEndpoint` take the full `ws://user:pass@host:port` and
  authenticate on the WebSocket upgrade.

Use `playwright_scraper.py` or `puppeteer_scraper.py` for either.

---

## "pyppeteer downloaded a two-year-old Chromium"

It does that by design — it pins its own build. Point it at an installed
browser instead:

```bash
python3 puppeteer_scraper.py --chromium-path /usr/bin/chromium \
    --collection boredapeyachtclub
```

pyppeteer is effectively unmaintained and its own README points at
Playwright. It is kept here for parity.

---

## "The fixtures file is missing"

`fixtures_generated.json` is committed, so this means a partial checkout or a
`.gitignore` rule that caught it. Regenerate it with your own captures:

```bash
python3 playwright_scraper.py --collection boredapeyachtclub \
    --dump-html ../captures/collection_bayc.html
# … one per entry in make_fixtures.py's SOURCES …
python3 make_fixtures.py
```

`make_fixtures.py` refuses to write a fixture that does not parse identically
to its untrimmed original, that classifies differently after trimming, or
that carries anything credential-shaped.

---

## Exit codes

| code | meaning |
|---|---|
| 0 | rows were written |
| 1 | a crash in this code — worth an issue |
| 2 | bad usage: an unsupported URL, a mode that does not match it, a malformed proxy |
| 3 | blocked before parsing |
| 4 | ran fine, found nothing |
| 5 | a remote service failed (the Scraping Browser, the Scraper API) |
| 6 | partial: some rows, then the run stopped early |

A run that writes nothing writes no sidecar either, so the previous good
output and its `.meta.json` stay consistent with each other.
