#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku` —
the identifier the README tells people to diff on for tracking an answer's
reception over time.

    python3 diff_runs.py --old ml.2026-09-01.json \\
                          --new ml.2026-09-07.json

Typical use is a scheduled re-run of one of the engines, kept under a dated
filename, diffed against the previous one:

    python3 playwright_scraper.py --url "$URL" --out "ml_$(date +%F)"
    python3 diff_runs.py --old "ml_$(ls -t ml_*.json | sed -n 2p)" \\
                          --new "ml_$(date +%F).json" --out diff.json

Four buckets, each keyed on sku — here the market's slug:

  added          — sku present in --new, absent from --old
  removed        — sku present in --old, absent from --new (deleted or
                   collapsed, or just off this particular feed run)
  changed        — sku present in both, with a different price, order book,
                   volume, liquidity, state or question
  source_changed — sku present in both with a different value, but also a
                   different `data_source`. That is the bucket this site
                   needs most: `spread`, the volume windows and the on-chain
                   ids are published by an EVENT page and by nothing else, so
                   a `markets` run diffed against an `events` run would
                   report every one of them as having appeared from nowhere.
                   Reported separately because it says something about our
                   own two snapshots rather than about the site — and
                   --fail-on-change deliberately ignores it.

A market this project's parser could not recover a sku for (None) cannot be
matched across runs at all, so it is counted and reported separately rather
than silently folded into "added"/"removed", which would be wrong on its face.
"""

import argparse
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple

from output_writer import UNIQUE_BY_SKU_MODES

# WHAT COUNTS AS A CHANGE ON THIS SITE, and why each of these and not more.
#
# These names are CHECKED against the row class by the offline suite, and
# that check exists because this file arrives from a sibling repo tracking
# ITS columns. Nothing fails when they are wrong: `diff_runs.py` simply
# compares eight fields that are absent from every row of both runs and
# reports "no changes" for ever. That is this family's most common bug shape
# — a tool doing less than it says while reporting success (§16) — and it
# survives a green suite because the suite tests the DIFF's mechanics rather
# than its subject.
#
#   price / currency         the whole point of an items run: what the
#                            cheapest standing listing costs, in the token it
#                            is denominated in. `currency` rides along
#                            because 6.8 ETH and 6.8 WETH are not the same
#                            price, and a listing can be re-made in another
#                            token without the number moving.
#   best_offer               the other side of the book. An offer moving
#                            while the ask holds still is an event a monitor
#                            wants and no `price` column shows.
#   last_sale / last_sale_at a SALE happened — the most consequential thing
#                            that can occur to an NFT, and it moves neither
#                            the listing nor the offer by itself.
#   owner_address            it changed hands. Tracked as the address and not
#                            the username, because a handle is display text
#                            and a person can rename themselves without
#                            anything having happened (§20: join on the id,
#                            never on a label).
#   listing_marketplace      OpenSea aggregates, so a listing moving from
#                            `opensea` to `blur` is a real change in where
#                            the item is for sale.
#   is_delisted              OpenSea hiding an item — a reported theft,
#                            usually — which removes it from the market
#                            without changing any price.
#   rarity_rank / title      both change what the row MEANS rather than what
#                            it costs. A rank moves when a collection is
#                            re-ranked; a name moves when its metadata is
#                            updated.
#
# Deliberately NOT tracked: `price_usd` and the other USD columns, which move
# with the token every minute and would report a change on every row of every
# run; `scraped_at` (it differs by construction); and `page`/`position`,
# which are properties of the ORDERING rather than of the item — and two runs
# under different orderings are refused outright, below.
TRACKED_FIELDS = ("price", "currency", "best_offer", "best_offer_currency",
                  "last_sale", "last_sale_at", "owner_address",
                  "listing_marketplace", "is_delisted", "rarity_rank",
                  "title")

# The subset whose presence depends on WHICH ROUTE built the row. Nothing on
# this site actually qualifies — the inlined state and the endpoint return
# the same objects, measured 49 of 50 rows identical across 18 stable columns
# — except the DOM fallback, which carries no prices at all. A run that fell
# back to the anchors would otherwise report every price as having vanished,
# so `data_source` differing turns a difference into `source_changed` rather
# than into a change (§8).
COUNT_FIELDS = ("price", "best_offer", "last_sale", "rarity_rank")


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(products: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for p in products:
        sku = p.get("sku")
        if sku is None:
            unmatchable += 1
            continue
        # A run's own output can already hold a duplicate sku (two rows in the
        # same category, or a rerun of dedupe_by_sku's job on older output
        # written before it existed) — keep the first and count the rest as
        # unmatchable rather than letting one clobber the other silently.
        if sku in indexed:
            unmatchable += 1
            continue
        indexed[sku] = p
    return indexed, unmatchable


def _within_tolerance(before: dict, after: dict, changes: dict,
                      tolerance_pct: float) -> bool:
    """True if every differing count field moved by less than `tolerance_pct`.

Unlike in most of this family, this flag has a real use here and the
    reason is worth stating. A prediction market's price moves CONTINUOUSLY:
    two runs of the same command minutes apart will differ on most rows by a
    tick, and a monitor alerted on every tick is a monitor nobody reads. A
    monitor watching for a real repricing wants a threshold; a monitor
    watching for a market CLOSING wants `closed`, which is not a count field
    and is never absorbed by this.

    It still DEFAULTS TO ZERO, because the default should report what
    happened rather than decide for the reader what was interesting.

    A move is judged on the LARGEST relative change among the count fields,
    so a genuine collapse in one price is not hidden by a tolerance applied
    field-by-field.
    """
    if tolerance_pct <= 0:
        return False
    for field in COUNT_FIELDS:
        if field not in changes:
            continue
        was, now = before.get(field), after.get(field)
        if not isinstance(was, (int, float)) or not isinstance(now, (int, float)):
            return False  # a None appearing or disappearing is a real change
        if was == 0:
            return False
        if abs(now - was) / abs(was) * 100.0 > tolerance_pct:
            return False
    return True


def diff_products(old: List[dict], new: List[dict],
                  price_tolerance_pct: float = 0.0) -> dict:
    """The four buckets. Named `diff_products` for the family's call shape.

    `price_tolerance_pct` keeps the family's parameter name; on this site it
    is a COUNT tolerance — see `_within_tolerance`.
    """
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed, source_changed, within_tolerance, lifecycle = [], [], [], []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in TRACKED_FIELDS
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue

        # THE TWO RUNS READ DIFFERENT VIEWS, which is not a change in the
        # answer — and on this site this is the bucket that matters most.
        #
        # Upvotes, views, shares, comments and the question answer count come
        # from the payload the view carried, which an archive or author page
        # carries and a topic page does not. So a row read off a topic feed
        # has no spread and carries its EVENT's volume, and the same market
        # read off its own event page has a spread and the MARKET's volume.
        # Diffing the two would report every one of them as having appeared
        # from nowhere or collapsed by two orders of magnitude.
        #
        # TWO columns decide "which view", not one, and the second is the
        # subtle one. `data_source` is `flight` on a listing row AND on an
        # event-page row — the payload is the payload — so a `markets` run
        # diffed against an `events` run agrees on it while disagreeing about
        # what `volume` MEANS. `volume_scope` is the column that says which,
        # and a run where it moved is a run that changed its view. Without it
        # the most likely diff anyone will actually run here — yesterday's
        # listing against today's deep walk — reports a 199,000,000 ->
        # 19,000,000 "collapse" on rows where nothing happened at all.
        #
        # `--fail-on-change` ignores this bucket for the same reason it
        # ignores a tolerance move: it says which view we read, not what
        # changed on the site.
        sources = (before.get("data_source"), after.get("data_source"))
        scopes = (before.get("volume_scope"), after.get("volume_scope"))
        view_fields = COUNT_FIELDS + ("volume_scope",)
        if (sources[0] != sources[1] or scopes[0] != scopes[1]) and any(
                f in field_changes for f in view_fields):
            view_part = {f: v for f, v in field_changes.items()
                         if f in view_fields}
            other_part = {f: v for f, v in field_changes.items()
                          if f not in view_fields}
            source_changed.append({
                "sku": sku, "title": after.get("title"),
                "data_source": {"old": sources[0], "new": sources[1]},
                "volume_scope": {"old": scopes[0], "new": scopes[1]},
                "changes": view_part,
            })
            field_changes = other_part
            if not field_changes:
                continue

        # There is no lifecycle bucket on this site, and its absence is a
        # measurement rather than an omission. A sibling repo needs one
        # because an auction closing moves a bid kind and the amount beside
        # it in one event; an answer has no such state machine. What it does
        # have -- being deleted or collapsed -- makes it vanish from the
        # feed, which is the `removed` bucket. The `lifecycle` key is still
        # emitted, always empty, so a consumer written against the family
        # diff shape does not have to branch.

        # A counter ticking rather than a real move -- see
        # `_within_tolerance`. Only when the ONLY differences are count
        # fields: a title or a body length changing alongside is a real
        # change whatever the size of the move.
        if (all(f in COUNT_FIELDS for f in field_changes)
                and _within_tolerance(before, after, field_changes,
                                      price_tolerance_pct)):
            within_tolerance.append({"sku": sku, "title": after.get("title"),
                                     "changes": field_changes})
            continue

        changed.append({"sku": sku, "title": after.get("title"),
                        "changes": field_changes})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "source_changed": source_changed,
        "within_tolerance": within_tolerance,
        "lifecycle": lifecycle,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result['source_changed'])} not comparable (the two runs read "
          f"different views), "
          f"{len(result.get('within_tolerance', []))} within the count "
          f"tolerance.")
    for p in result["added"]:
        print(f"  + {p.get('sku')}  {p.get('title')}  "
              f"{p.get('price')} in {p.get('event_slug')}")
    for p in result["removed"]:
        print(f"  - {p.get('sku')}  {p.get('title')}  "
              f"{p.get('price')} in {p.get('event_slug')}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result.get("within_tolerance", []):
        moves = ", ".join(
            f"{f}: {v['old']} -> {v['new']}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {moves}  [within --price-"
              f"tolerance-pct: a live counter ticking, not an event]")
    for c in result["source_changed"]:
        src = c["data_source"]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        scope = c.get("volume_scope") or {}
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[data_source {src['old']!r} -> {src['new']!r}, volume_scope "
              f"{scope.get('old')!r} -> {scope.get('new')!r}: the two runs "
              f"read different views of the same market, so this is not a "
              f"site-side change. A listing row has no spread and carries "
              f"its EVENT's volume; an event-page row has both of its own]")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str) -> Tuple[Optional[str], Optional[dict]]:
    """Read the `<out>.meta.json` sidecar beside a run's JSON output.

    Returns (status, meta), or (None, None) when there is no sidecar — which
    is the normal case for output written before run metadata existed, or by
    `scraper_api_client.py` (single fetch, no pagination to cut short).
    """
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def _check_comparable(args) -> bool:
    """Refuse an assortment diff between runs that are not both complete.

    This is the failure mode the sidecar exists for: a run cut short on page
    3 of 10 is missing every product on pages 4-10, and diffing it against
    yesterday's full run reports all of them as `removed` — reading as "these
    products were delisted" when in fact they were simply never fetched.
    The counters of the SKUs both runs DID see are still comparable, which is
    why this is a refusal with a --force escape hatch rather than a hard
    error.
    """
    problems = []
    modes = {}
    # The ORDERING each run used. On OpenSea this is not a presentation
    # detail: the ordering decides WHICH rows are in the file. Under the
    # site's own price ordering a walk reaches the LISTED items and stops —
    # 282 of 9,998 on one collection — while `--sort created` walks the
    # collection itself. Two runs under different orderings are two different
    # SAMPLES, so every row the other happens not to hold reads as `added` or
    # `removed` (§21: the ordering is a column, not a sidecar footnote).
    orderings = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if status is None:
            continue  # no sidecar: nothing to check, see _run_status
        mode = (meta or {}).get("mode")
        if mode:
            modes[label] = mode
        ordering = tuple(
            (meta or {}).get(key) for key in
            ("sort", "ranking", "timeframe", "activity_filter"))
        if any(ordering):
            orderings[label] = ordering
        if mode and mode not in UNIQUE_BY_SKU_MODES:
            # This tool's whole premise is one row per `sku`, diffed on
            # A mode that produces many rows per sku would give a diff
            # whose every line is an artefact of two rows sharing an id, so
            # it is refused outright rather than answered. Both of this
            # repo's current modes qualify; the check is here so that adding
            # one that does not is caught rather than discovered.
            problems.append(
                f"{label} ({path}) is a {mode!r} run, which is not one row "
                f"per sku. This tool diffs one row per sku, so there is "
                f"nothing here it can compare.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(set(modes.values())) > 1:
        problems.append(
            f"the two runs are different modes ({modes}). The three modes "
            f"here yield three different ROW CLASSES sharing only the family "
            f"prefix, so a diff between two of them would have almost no "
            f"column in common to compare.")
    if len(set(orderings.values())) > 1:
        readable = {label: "/".join(str(v) for v in value if v)
                    for label, value in orderings.items()}
        problems.append(
            f"the two runs used different orderings ({readable}). On this "
            f"site the ordering decides WHICH rows are in the file and not "
            f"just their order — a price-ordered walk holds the listed items "
            f"and a created-ordered one holds the collection — so every row "
            f"one sample happens not to contain would read as `added` or "
            f"`removed`.")

    # WHICH ADDRESS ANSWERED, and deliberately NOT which language.
    #
    # `source` is the host that served a row. On this site it is
    # `opensea.io` on every row — the seven locales are PATHS under
    # one host only. A run whose rows carry more than one is
    # NOT normal here and is worth saying out loud — so a mixed run is
    # never refused. What IS worth saying is when two runs each landed
    # consistently on a DIFFERENT single host, because then `added` and
    # `removed` would be describing the address rather than the catalogue.
    #
    # There is deliberately no language check, and the reason is worth
    # writing down because the sibling repo this file came from has one.
    # There, `language` meant which of twenty-four language SITES a row came
    # from, and those are different catalogues. Here it is the site's own
    # `detectedLanguage` for the STORY — one measured English tag's day
    # archive carried 123 `en` rows and 5 `id` ones — so refusing a pair
    # whose languages differ would refuse two perfectly comparable runs of
    # the same tag.
    sources = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        try:
            rows = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        hosts = {r.get("source") for r in rows if r.get("source")}
        if len(hosts) == 1:
            sources[label] = hosts.pop()
    if len(set(sources.values())) > 1:
        problems.append(
            f"the two runs landed on different hosts ({sources}). This site "
            f"serves one story from several addresses, so this is usually a "
            f"different URL rather than a different catalogue — but added "
            f"and removed would describe the address change rather than "
            f"anything about the markets.")

    if not problems:
        return True

    # A generic headline, because the reasons below are no longer only about
    # completeness: a mode mismatch and a reviews run are refused too, and a
    # message naming the wrong reason sends the reader looking in the wrong
    # place.
    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the incomplete side, or pass --force to compare anyway "
          "(added/removed will include products that were simply never "
          "fetched).")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two opensea-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--price-tolerance-pct", type=float, default=0.0,
                   metavar="PCT",
                   help="Treat a counter move smaller than PCT%% as a live "
                        "counter ticking rather than an event: reported "
                        "separately and ignored by --fail-on-change. Default 0 "
                        "— report every tick. Unlike in most of this family "
                        "the flag has a real use here: this site's own "
                        "clap counts are live, so a monitor watching for an "
                        "answer taking off wants a threshold, while one "
                        "watching for an edit wants text_chars, which this "
                        "never absorbs.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was partial "
                        "or failed. Products never fetched by the short run will "
                        "appear as added/removed.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    result = diff_products(old, new, price_tolerance_pct=args.price_tolerance_pct)
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    # Neither `source_changed` nor `within_tolerance` is a reason to fail.
    # The first means our two runs read different views of the same answer;
    # the second means a live counter ticked. Neither says anything about the
    # site, and alerting on either would train whoever reads the alert to
    # ignore it.
    # Neither `source_changed` nor `within_tolerance` is a reason to fail —
    # see their comments above.
    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
