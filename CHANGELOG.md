# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the versions follow [SemVer](https://semver.org/) as closely as a CLI
toolkit can. A PATCH release means *fixes*, not that every flag is frozen: a
default can change in one when leaving it alone would cost a reader money or
data, and when that happens the release notes lead with it in a blockquote.

A released section is HISTORY. If a later release contradicts something
stated here, the correction goes in the new section — the old one stays as it
was written, because anyone can read it with `git show v0.1.0:CHANGELOG.md`.

## [Unreleased]

### Added

- **A Scraping Browser fixture, and the check that needed it.** The
  `--cdp-endpoint` path was run for the first time: three modes over a
  `country-us` profile, 250 rows identical to a local browser's on the same
  pages, pyppeteer matching Playwright 150 of 150 over the same
  authenticated endpoint, Selenium refusing it with the documented reason and
  exit 2, and a second concurrent run exiting 5 with `profile_locked` and the
  credential masked to `ws://***:***@cb.2captcha.com:9222`.

  The capture from that run is now a fixture, because the auto-solve
  extension injects a Turnstile hunter into every page it loads and this is
  the only fixture that can spring that trap. Measured on one collection page
  fetched two ways in the same hour: `cf-turnstile` 1 over CDP against 0
  locally, `chrome-extension://…hbpbo` 16 against 0, and
  `challenges.cloudflare.com` — the marker this repo actually uses — 0 on
  both. Putting `cf-turnstile` back into the marker set now turns six checks
  red, one of them on that fixture. A sibling repo shipped the same check and
  it passed for the wrong reason, having only ever run against pages fetched
  with a plain client.

- **The proxy path, run for the first time**, which was the last credentialled
  path §16 asks for and the one that had never been exercised. Through a
  2Captcha residential exit (the global mix, which came out in RU rather than
  the EU its hostname suggests): Playwright 250 rows over 3 pages with one
  timeout retried and recovered; pyppeteer 50 rows through
  `page.authenticate`; Selenium warning that it cannot send proxy
  credentials, stripping them and failing — the documented limitation
  verified rather than asserted; and pool rotation from a dead exit to a live
  one, with `ERR_PROXY_CONNECTION_FAILED` recognised as a proxy fault rather
  than a timeout.

  The check worth having a real credential for: **the password appears in no
  process command line**, verified with `ps` mid-run. The browser is given
  `--proxy-server=http://eu.proxy.2captcha.com:2334` — host and port only.
  That is §3's central promise, checked against a live secret instead of a
  fixture.

  The data did not move: 250 of 250 rows identical to the Finnish datacentre
  and US Scraping Browser runs, on 12 stable columns each. Four unrelated
  networks now return the same rows for the same URL.

### Changed

- The challenge-marker comment in `product_parser.py` and the README now
  carry THIS site's measurement rather than a sibling's.
- `page_flow.block_advice` names the four networks this site has actually
  been measured serving, instead of "a plain datacentre address".

- `SECURITY.md` said this project has no releases or version tags; it has
  both. "Supported versions" now names the latest release and `main`.

## [0.1.0] - 2026-09-17

First release. Scrapes opensea.io into JSON or CSV from three views, with
four ways to fetch a page.

### Added

- **Three modes, three row classes.** `--mode items` (the default) reads a
  collection's NFTs — listing price, best offer, last sale, traits, rarity
  rank, owner; `--mode collections` reads OpenSea's own ranking — floor,
  volume, sales, owners, supply; `--mode activity` reads a collection's feed
  — sales, listings, offers, transfers and mints. The three share the
  family's row prefix (`source`, `scraped_at`, `url`, `sku`, `title`) and
  nothing else, so `diff_runs.py` refuses to compare two of them.
- **Four engines that produce the same rows.** Playwright (primary),
  Selenium, pyppeteer, and a one-request 2Captcha Scraper API client. The
  three browser engines were measured producing **150 of 150 identical rows**
  on the same URL, across 15 stable columns including `page`, `position` and
  `data_source`.
- **Cursor pagination through the site's own endpoint.** Page 1 is a real
  navigation and yields the fifty rows OpenSea inlines into the page; pages
  2..N are the site's own GraphQL call, issued from inside the open document
  so it carries the same cookies, connection and exit. One normaliser reads
  both routes — measured 49 of 50 rows identical across 18 stable columns,
  the one difference being a listing whose price moved between the capture
  and the query.
- **`--sort`, and the honesty around it.** The ordering decides *which* rows
  end up in the file. Under the site's own price ordering the cursor carries
  the last row's price and goes null where the listed items end, so the walk
  stops there and reports COMPLETE: measured on `boredapeyachtclub`, 282
  priced rows of 350, against OpenSea's own `listedItemCount` of **282** out
  of a 9,998-item collection. `--sort created` walks the collection itself
  and was measured going 800 items deep.
- **`collection_totals` in the run sidecar** — what OpenSea says the
  collection holds, beside what the run read, so `complete` cannot be
  mistaken for `exhaustive`.
- **Chain-aware addresses.** An EVM item is
  `/item/{chain}/{contract}/{token_id}`; a Solana item is
  `/item/solana/{mint}` with `token_id` null, because the mint is the token.
- **Captcha, proxy and fingerprint support** carried from the family:
  reCAPTCHA v2/v2-invisible/v3/enterprise and Cloudflare Turnstile including
  the Challenge-page form, proxy pools with rotation and credential masking,
  and 2Captcha fingerprints. `--fingerprint` was run end to end and verified
  to apply a user agent, locale, timezone and viewport.
- **An offline suite of over ten thousand assertions** that passes with no
  engine library installed, over eight fixtures cut from real captures by
  `make_fixtures.py` — which proves each one parses identically to its
  untrimmed original, classifies the same way after trimming, and carries no
  profile handle or credential-shaped string.
- **A canary that needs no secrets.** A real three-mode scrape runs daily
  from a bare GitHub runner and is expected green, because the listing path
  needs no credentials — which is what keeps that claim in the README from
  going stale unnoticed.

### Measured, from one Hetzner datacentre address in Helsinki on 2026-09-17

- Every page kind, every mode and every locale answered HTTP 200 with no key,
  no proxy and no account. So did OpenSea's own GraphQL endpoint to an
  anonymous POST.
- **`Python-urllib/3.13` is refused with HTTP 403** on both the HTML route
  and the endpoint, while `python-requests/2.32`, `curl/8.x`, no User-Agent
  at all and a made-up `opensea-scraper/0.1` were all served. That is a
  Cloudflare rule against one signature and it is named in the README,
  because the failure it produces reads like a site that blocks scrapers.
- Headless and headful were **identical** — 150 rows and the same 150 ids,
  two runs each.
- Zero vendor captcha markers across seven captures, so the solver path here
  is implemented and unexercised. That is a statement about this repo's
  testing and not a claim about what a solver can do.
- The Scraper API path: three requests, one per mode, `$0.0005` each, HTTP
  200, rows identical to the browser engines'.

### Notes

- `--concurrency` above 1 is **refused in every mode**, with the reason:
  OpenSea has no address for page 2, so page 5's request does not exist until
  page 4 has been read. Run several collections at once instead.
- A collection page publishes **no product JSON-LD** — its three
  `application/ld+json` blocks are `BreadcrumbList`, `Brand` and `WebSite`.
  The site's own inlined state is the primary path and the URL pattern is the
  fallback.
- `cf-turnstile` is deliberately absent from the challenge markers: 2Captcha's
  own Scraping Browser extension injects it into every page it loads.
  `challenges.cloudflare.com` is used instead, measured 0 on every served
  page.

[Unreleased]: https://github.com/2scraper/opensea-scraper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/2scraper/opensea-scraper/releases/tag/v0.1.0
