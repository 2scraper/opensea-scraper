# opensea-scraper

[![release](https://img.shields.io/github/v/release/2scraper/opensea-scraper?sort=semver)](https://github.com/2scraper/opensea-scraper/releases)
[![tests](https://github.com/2scraper/opensea-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/opensea-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/opensea-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/opensea-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-playwright%20%7C%20selenium%20%7C%20pyppeteer-lightgrey)](#engines)
[![runs without an account](https://img.shields.io/badge/account-not%20required-brightgreen)](#you-do-not-need-an-account-a-key-or-a-proxy)

Scrapes [opensea.io](https://opensea.io) into JSON or CSV: **a collection's
items** with their listing prices, best offers, last sales, traits, rarity
ranks and owners; **OpenSea's own collection ranking** with floors, volume
and supply; and **a collection's activity feed** — sales, listings, offers,
transfers and mints.

Playwright, Selenium and pyppeteer engines, a one-request
[2Captcha Scraping Browser API](https://2captcha.com) path, proxy rotation,
fingerprints and captcha solving. All four produce the same rows.

---

## You do not need an account, a key or a proxy

Measured on **2026-09-17** from one Hetzner datacentre address in Helsinki
(AS24940), with no key, no proxy and no account:

| what was asked for | answer |
|---|---|
| every page kind and every mode | HTTP 200 |
| a Japanese-locale collection page | HTTP 200 |
| `curl/8.x`, no User-Agent at all, `HeadlessChrome/140`, a made-up `opensea-scraper/0.1` | HTTP 200 to all four |
| OpenSea's own GraphQL endpoint, anonymous POST | HTTP 200, `x-ratelimit-remaining: 400` |
| headless vs headful, 2 runs each | identical — 150 rows, the same 150 ids, 4 of 4 |
| vendor captcha markers across 7 captures | 0 |

**One User-Agent IS refused, and it is the one that will find you first.**
`Python-urllib/3.13` gets HTTP **403** — on the HTML route and on the
endpoint alike — while `python-requests/2.32` from the same address in the
same minute is served normally. That is a Cloudflare rule against one
signature, not a site that blocks scrapers, and it is named here because the
failure it produces is the most misleading one OpenSea can give you.

**Confirmed from a second network on the same day.** The canary's first
dispatch ran the whole three-mode scrape from a bare GitHub runner, with no
secrets, and came back green with the same numbers: 350 items over 4 pages
stopping at `listed_items_exhausted`, 202 ranking rows with 201 floors, and a
sales feed of only sales. Two unrelated datacentre networks, one afternoon.

So the paid products here buy **volume from many addresses, a specific
country, browser infrastructure you do not run, and a solver for the day
Cloudflare does issue its managed challenge** — not access. The
[canary](#the-canary) re-runs that scrape every morning, precisely so that
this claim is retested without anyone remembering to.

---

## Quick start

```bash
git clone https://github.com/2scraper/opensea-scraper
cd opensea-scraper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium

# a collection's cheapest items, three pages
python3 playwright_scraper.py \
    --collection boredapeyachtclub --pages 3
```

```
[INFO] Page 1 shipped its rows in the page's own state — parsing it directly.
[INFO] Parsed 50 row(s) from page 1.
[INFO] Key/title coverage on page 1: 50 and 50 of 50 (100% at worst).
[INFO] This run holds 250 of the 9998 item(s) OpenSea says this collection
       has (2.5%). `complete` in the sidecar means the walk finished, not
       that it read the whole collection.
[INFO] Rows by route: graphql=200, ssr=50.
[+] Saved 250 products -> opensea_items.json
[+] Wrote run metadata -> opensea_items.meta.json (status=complete)
```

Twelve seconds, exit 0. [`sample_output.json`](sample_output.json) and
[`sample_output.csv`](sample_output.csv) are cut from a real run of exactly
that command.

---

## The three modes

Each one reads a different page and yields a **different row class** — they
share only the family prefix (`source`, `scraped_at`, `url`, `sku`, `title`),
and `diff_runs.py` refuses to compare two of them.

```bash
# 1. items — one row per NFT (the default)
python3 playwright_scraper.py --collection pudgypenguins --pages 5

# 2. collections — OpenSea's own ranking, one row per collection
python3 playwright_scraper.py --mode collections --timeframe 1d --pages 3

# 3. activity — one row per event
python3 playwright_scraper.py --collection pudgypenguins \
    --mode activity --activity sales --pages 3
```

Measured 2026-09-17, one run each:

| mode | pages | rows | notes |
|---|---|---|---|
| `items` | 4 | 350 | stopped at the end of the listed items, exit 0 |
| `collections` | 3 | 202 | 201 of 202 carried a floor price |
| `activity` | 3 | 232 | all `SALE`, all priced, all with a timestamp |

A single item URL works too and yields one row:

```bash
python3 playwright_scraper.py \
    --url "https://opensea.io/item/ethereum/0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d/1"
```

---

## What a row looks like

```json
{
  "source": "opensea.io",
  "scraped_at": "2026-09-17T08:22:50.208548+00:00",
  "url": "https://opensea.io/item/ethereum/0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d/1774",
  "sku": "ethereum/0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d/1774",
  "title": "#1774",
  "price": 6.8497666,
  "currency": "ETH",
  "price_usd": 16707.950690719998,
  "best_offer": 6.51,
  "best_offer_currency": "WETH",
  "last_sale": 6.75,
  "last_sale_currency": "WETH",
  "last_sale_at": "2026-09-06T21:56:59.000Z",
  "listing_marketplace": "blur",
  "chain": "ethereum",
  "token_id": "1774",
  "rarity_rank": 2412,
  "traits": [
    "Background: Orange",
    "Clothes: Toga",
    "Earring: Silver Hoop"
  ],
  "owner_address": "0xff53e1da7b67ae676d7742f858aab5bd4bc937f6",
  "data_source": "ssr",
  "page": 1,
  "position": 1
}
```

That is the first row of [`sample_output.json`](sample_output.json), verbatim.

`price` is a **token amount** and `currency` names the token; `price_usd` is
OpenSea's own conversion, which moves with the market even when the listing
does not. `listing_marketplace` is `blur` here and that is a real answer
rather than a bad read: OpenSea aggregates other marketplaces' listings.

`last_sale_currency` can be **null** on a perfectly good row — OpenSea does
not always name the token a sale settled in, measured on **11 of 50 rows** of
one capture against 25 in ETH and 14 in WETH. It is null rather than an empty
string, because an empty string in a currency column reads as a currency.

---

## The one thing to know before you scrape a collection

**`--sort` decides WHICH rows you get, not just their order.**

OpenSea pages every feed with an opaque cursor, and under its own ordering —
by price, which is what a collection page shows and what "scrape the floor"
means — that cursor carries the last row's price. At the boundary between
listed and unlisted items the key goes null, and the walk stops:

```
[INFO] Stopped at the end of the LISTED items: under --sort price the cursor
       carries the last row's price, and it goes null where the listed items
       end. The run holds 350 row(s) and is COMPLETE — every item that has a
       price is in it, and the rest have none. OpenSea's own count for this
       collection is 282 listed item(s) out of 9998.
```

That run is **complete and it is 3.5% of the collection**, and both are true.
Three independent numbers agreed when this was measured on
`boredapeyachtclub`: 282 rows carried a price, the priced rows were exactly a
prefix of the file with their prices ascending, and OpenSea's own
`listedItemCount` for the same collection in the same minute was **282**.

To walk the collection itself rather than its order book:

```bash
python3 playwright_scraper.py --collection boredapeyachtclub \
    --sort created --pages 20
```

`--sort created` was measured going **800 items deep** on the same collection
without a stumble. Two runs under different sorts are two different
**samples**, so `diff_runs.py` refuses to compare them — the same way it
refuses two different modes.

| `--sort` | what it walks |
|---|---|
| `price` (default) | cheapest first, ends at the last listed item — the floor and the order book |
| `price-desc` | most expensive first |
| `created` / `newest` | the whole collection, by mint date |
| `rarity` | rarest first |
| `last-sale` | most recently sold first |

---

## How pagination works here, and why `--concurrency` is refused

Page 1 is a real navigation: the browser opens the human URL and the run
reads the state OpenSea inlined into it — fifty complete rows, before a pixel
paints. That is what proves the site served the request, and it is what
carries the collection's own totals.

Pages 2..N are **not** navigations, because OpenSea publishes no address for
them. Every feed is cursor-paginated, so the engines call the site's own
GraphQL endpoint **from inside the open page** — same origin, same cookies
(including Cloudflare's `__cf_bm`), same TLS session, same exit — and parse
the response with the same code that read page 1.

Two consequences, both deliberate:

* **`--concurrency` above 1 is refused, in every mode, with the reason.**
  Page 5's request does not exist until page 4 has been read. For throughput,
  run several collections at once, one process each.
* **`--dump-html` writes `<path>.pageN.json` for the cursor pages**, because
  what the parser saw there was a JSON body and not a document.

The two routes are not assumed to agree — they were checked. Feeding the same
page through both, **49 of 50 rows were identical across 18 stable columns**;
the one difference was a listing whose price had changed in the forty minutes
between the capture and the query.

---

## The traps that look like bugs

* **A Solana item has no token id.** An EVM address is
  `/item/ethereum/{contract}/{token_id}`; a Solana one is
  `/item/solana/{mint}` — two segments, because the mint *is* the token.
  `token_id` is null on those rows and `contract_address` carries the mint.
  OpenSea's own payload sets `tokenId` to the mint address, so a parser that
  writes it through produces an address the site does not serve, on every row,
  at 100% coverage.

* **Most of a collection has no price, and that is correct.** 282 of 9,998
  Bored Apes were listed when this was written. Under `--sort created` you
  will see mostly nulls in `price`; under `--sort price` you will see none
  until the walk crosses the boundary.

* **`page 1` of a `--sort created` run is dropped, on purpose.** The rows the
  page renders are the ones the SITE chose, under the site's ordering — so a
  run asking for another ordering takes page 1 as proof-of-service and totals
  only, and starts the feed from the endpoint. Keeping them would put two
  samples in one file, and page 1's cursor is not even valid for the other
  ordering: the endpoint answers `Invalid cursor`. The run says so in the log
  and records `rows_dropped_from_page_1` in the sidecar.

* **`0` is a real number and `null` is a real absence.** A ranking's
  `floorPriceChange: 0` means nothing moved in that window and is kept; a
  collection with nothing listed has `floorPrice: null` and stays null.

* **The ranking's `rank` belongs to the ranking, not the collection.** The
  same collection has a different rank under a different `--timeframe`, which
  is why the timeframe is in the sidecar and `volume_window` is on every row.

* **`Python-urllib` gets a 403.** See the top of this file.

---

## When the paid products actually help

Nothing above needed a key. These do:

| you want | what to use |
|---|---|
| many addresses, or one specific country | `--proxy` / `--proxy-file`, a [2Captcha proxy](https://2captcha.com/proxy) |
| no browser on your machine at all | `scraper_api_client.py` — one request, `$0.0005`, measured 200/50 rows in 5.0s |
| a browser you do not run, with persistent cookies | `--cdp-endpoint`, the Scraping Browser API |
| a consistent device identity | `--fingerprint` |
| the day Cloudflare issues its managed challenge | `--solve-captcha` (the default already solves when blocked) |

```bash
# the one-request path, no browser anywhere
python3 scraper_api_client.py \
    --url "https://opensea.io/collection/boredapeyachtclub"
```

**On captchas, what is and is not known here.** No challenge was met on
opensea.io while this repo was built: 0 occurrences of
`challenges.cloudflare.com`, `cdn-cgi/challenge-platform`, `recaptcha`,
`hcaptcha`, `turnstile`, `datadome`, `perimeterx` or `data-sitekey` across
seven captures. So the solver path here is **implemented and unexercised** —
which is a fact about this repo's testing, not a claim about what a solver
can do. What this repo implements: reCAPTCHA v2, v2-invisible, v3 and
enterprise, and Cloudflare Turnstile including the Challenge-page form, whose
parameters are captured by an init script installed before any page script
runs because a Challenge page publishes no sitekey in its markup. A page
carrying no widget is reported unsolved rather than charged for.

Credentials go in `.env`, never on a command line
([`.env.example`](.env.example)):

```bash
cp .env.example .env
python3 env_config.py     # prints what was picked up, without printing secrets
```

---

## Engines

| engine | notes |
|---|---|
| `playwright_scraper.py` | **primary.** The one the Dockerfile builds and the canary runs. |
| `selenium_scraper.py` | Cannot use an authenticated `--cdp-endpoint` (chromedriver's `debuggerAddress` is a bare `host:port`), and `--proxy` credentials cannot be sent — they are stripped with a warning. |
| `puppeteer_scraper.py` | pyppeteer is effectively unmaintained and its own README points at Playwright. Kept for parity, and an authenticated CDP endpoint does work from it. |
| `scraper_api_client.py` | One HTTP request, no browser. One page only — it has no open document to issue a cursor request from. |

All three browser engines produced **150 of 150 identical rows** on the same
URL, across 15 stable columns including `page`, `position` and `data_source`.

**Install exactly one.** The three pin mutually unsatisfiable versions
(playwright wants `pyee>=13`, pyppeteer `pyee<12`; pyppeteer wants
`urllib3<2`, selenium `>=2.6`). A virtualenv each is what CI does and what
this README recommends.

```bash
pip install -r requirements.txt -r requirements-selenium.txt   # or -puppeteer
```

---

## Output contract

Same across this family of scrapers.

* **A run that finds nothing writes nothing** — last night's good output
  survives. `--allow-empty` is the opt-out.
* **Blocked ≠ empty ≠ partial**: exit `0` ok · `1` crash · `2` bad usage ·
  `3` blocked · `4` zero rows · `5` remote API error · `6` partial.
* **`<out>.meta.json` beside every successful run**, with `status`,
  `stop_reason`, which pages failed by number, the ordering the run used, and
  `collection_totals` — what OpenSea says the collection holds, beside what
  the run read.
* **An empty CSV still carries its header**, of the mode that produced it.
* Rows are merged in **page order**, never arrival order.

`stop_reason` is worth reading. `cursor_exhausted` and
`listed_items_exhausted` both mean COMPLETE; `endpoint_error` and
`parser_found_nothing` do not.

### Diffing two runs

```bash
python3 diff_runs.py --old monday.json --new tuesday.json --fail-on-change
```

It refuses to compare runs that are not both `complete`, that used different
modes, or that used **different orderings** — on this site the ordering
decides which rows are in the file, so two sorts are two samples and every
row one of them lacks would read as `removed`.

---

## The canary

`.github/workflows/canary.yml` runs a real three-mode scrape against
opensea.io every morning **with no secrets**, and is expected green. It
asserts pagination reached at least three pages, that the price ladder
ascends and its priced rows match OpenSea's own `listedItemCount`, that ranks
are contiguous across pages, that `--activity sales` returns only sales, and
that `page`+`position` is unique across the run. A block warns (and says in
the step summary that nothing was tested today); zero rows from a served page
fails.

---

## Testing

```bash
python3 smoke_test.py     # no network, no key, no engine library needed
pytest -q                 # the same run, through the pytest entry point
```

The offline suite runs **over ten thousand assertions** — most of them one
per row, marker or name across eight fixtures — with no engine library
installed at all. The fixtures are cut from real captures by
`make_fixtures.py`, which proves each one parses identically to its untrimmed
original column for column, classifies the same way, and replaces every real
profile handle with a placeholder before writing it.

CI additionally installs each engine in its own venv and fails on an
unexpected skip, builds the Docker image, runs its entrypoint, launches a
browser inside it and checks the image carries no `.env`, no test suite and
no fixtures.

## Docker

```bash
docker build -t opensea-scraper .
docker run --rm -v "$PWD/out:/out" opensea-scraper \
    --collection boredapeyachtclub --pages 3 --out /out/bayc
```

---

## Contributing, security, licence

[CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) ·
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) · [MIT](LICENSE)

Scrape responsibly: this reads public pages at a polite rate, and `--delay`
exists to keep it that way.
