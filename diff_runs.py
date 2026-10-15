#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku`:
an NFT's chain/contract/token id in an items run, a collection's slug in a
collections run, an event's id in an activity run.

    python3 diff_runs.py --old bayc.2026-09-01.json \\
                          --new bayc.2026-09-07.json

Typical use is a scheduled re-run of one of the engines, kept under a dated
filename, diffed against the previous one:

    python3 playwright_scraper.py --collection boredapeyachtclub \\
        --out "bayc_$(date +%F)"
    python3 diff_runs.py --old "$(ls -t bayc_*.json | sed -n 2p)" \\
                          --new "bayc_$(date +%F).json" --out diff.json

WHICH COLUMNS are compared depends on the run's MODE, because the three modes
write three different row classes (`Item`, `Collection`, `Activity`) that
share only the family prefix. The mode is read from the `<out>.meta.json`
sidecar, or from the rows' own columns when there is no sidecar.

Buckets, each keyed on sku:

  added            — sku present in --new, absent from --old
  removed          — sku present in --old, absent from --new (sold out of
                     the listed walk, delisted, out of this ranking window,
                     or simply beyond the pages this run fetched)
  changed          — sku present in both, with a different tracked value
  source_changed   — sku present in both with a different value, but also a
                     different `data_source`: one run read the page's own
                     state or the endpoint, the other fell back to the DOM,
                     which carries no prices. Reported separately because it
                     says something about our own two snapshots rather than
                     about the site, and --fail-on-change ignores it.
  within_tolerance — only numeric columns moved, and by less than
                     --price-tolerance-pct.

A row this project's parser could not recover a sku for (None) cannot be
matched across runs at all, so it is counted and reported separately rather
than silently folded into "added"/"removed".
"""

import argparse
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple

from output_writer import UNIQUE_BY_SKU_MODES

# WHAT COUNTS AS A CHANGE ON THIS SITE, per mode.
#
# These names are CHECKED against each mode's row class by the offline suite.
# Nothing fails at runtime when they are wrong: comparing a column that is
# absent from every row of both runs simply reports "no changes" for ever.
#
# items:
#   price / currency         what the cheapest standing listing costs, in the
#                            token it is denominated in (6.8 ETH and 6.8 WETH
#                            are not the same price).
#   best_offer               the other side of the book.
#   last_sale / last_sale_at a sale happened.
#   owner_address            it changed hands. The address and not the
#                            username, because a handle is display text.
#   listing_marketplace      OpenSea aggregates, so a listing moving from
#                            `opensea` to `blur` is a real change.
#   is_delisted              OpenSea hiding an item (usually a reported theft).
#   rarity_rank / title      what the row MEANS rather than what it costs.
#
# collections:
#   price / top_offer        the floor and the best collection offer.
#   volume / sales / owners  activity in the run's window (`volume_window`
#                            is the same for both runs: two runs under
#                            different timeframes are refused below).
#   floor_change / rank      how the collection moved and where it now sits.
#   total_supply / verified  / title: what the collection IS.
#
# activity:
#   An event is a past fact and should not change once recorded; every
#   column that describes it is tracked anyway, so a difference (a re-indexed
#   event, a parser change) is reported rather than hidden.
#
# Deliberately NOT tracked in any mode: the USD columns, which move with the
# token every minute; `scraped_at` (it differs by construction); and
# `page`/`position`, which are properties of the ORDERING — and two runs
# under different orderings are refused outright, below.
TRACKED_FIELDS_BY_MODE = {
    "items": ("price", "currency", "best_offer", "best_offer_currency",
              "last_sale", "last_sale_at", "owner_address",
              "listing_marketplace", "is_delisted", "rarity_rank", "title"),
    "collections": ("price", "currency", "top_offer", "top_offer_currency",
                    "floor_change", "volume", "sales", "owners",
                    "total_supply", "rank", "verified", "title"),
    "activity": ("event_type", "event_time", "price", "currency", "quantity",
                 "from_address", "to_address", "token_id", "title"),
}

# The numeric subset per mode: what --price-tolerance-pct may absorb, and
# what a DOM-fallback row lacks (it carries no prices), so a `data_source`
# difference turns a difference here into `source_changed`.
COUNT_FIELDS_BY_MODE = {
    "items": ("price", "best_offer", "last_sale", "rarity_rank"),
    "collections": ("price", "top_offer", "volume", "sales", "owners",
                    "floor_change"),
    "activity": ("price", "quantity"),
}

# The items lists under their historical names, for anything importing them.
TRACKED_FIELDS = TRACKED_FIELDS_BY_MODE["items"]
COUNT_FIELDS = COUNT_FIELDS_BY_MODE["items"]


def mode_of_rows(rows: List[dict]) -> Optional[str]:
    """The mode a run's rows were written in, read from their own columns.

    Used only when there is no sidecar to say so: `event_type` exists only on
    an activity row and `volume` only on a collections row.
    """
    for r in rows:
        if "event_type" in r:
            return "activity"
        if "volume" in r or "top_offer" in r:
            return "collections"
        if "best_offer" in r or "rarity_rank" in r:
            return "items"
    return None


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
                      tolerance_pct: float,
                      count_fields: Tuple[str, ...] = COUNT_FIELDS) -> bool:
    """True if every differing numeric field moved by less than `tolerance_pct`.

    A floor price, a collection's volume and its owner count move all the
    time, so a monitor alerted on every small move is a monitor nobody reads.
    A monitor watching for an item changing hands wants `owner_address`,
    which is not numeric and is never absorbed by this.

    It DEFAULTS TO ZERO, because the default should report what happened
    rather than decide for the reader what was interesting. Every numeric
    field must be inside the tolerance, so a genuine collapse in one is not
    hidden by small moves in the others.
    """
    if tolerance_pct <= 0:
        return False
    for field in count_fields:
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
                  price_tolerance_pct: float = 0.0,
                  mode: Optional[str] = None) -> dict:
    """The buckets. Named `diff_products` for the family's call shape.

    `mode` picks the columns compared; when it is not given it is read from
    the rows themselves (see `mode_of_rows`), falling back to items.
    """
    mode = mode or mode_of_rows(new) or mode_of_rows(old) or "items"
    if mode not in TRACKED_FIELDS_BY_MODE:
        raise ValueError(f"diff_runs cannot compare a {mode!r} run")
    tracked = TRACKED_FIELDS_BY_MODE[mode]
    count_fields = COUNT_FIELDS_BY_MODE[mode]
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed, source_changed, within_tolerance, lifecycle = [], [], [], []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in tracked
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue

        # THE TWO RUNS READ DIFFERENT ROUTES. A row built from the page's
        # own state and one built from the endpoint carry the same objects;
        # a row from the DOM fallback carries no prices at all. So when
        # `data_source` differs, a difference in a numeric column is about
        # our two snapshots rather than the site, and goes to its own bucket.
        sources = (before.get("data_source"), after.get("data_source"))
        if sources[0] != sources[1] and any(
                f in field_changes for f in count_fields):
            view_part = {f: v for f, v in field_changes.items()
                         if f in count_fields}
            other_part = {f: v for f, v in field_changes.items()
                          if f not in count_fields}
            source_changed.append({
                "sku": sku, "title": after.get("title"),
                "data_source": {"old": sources[0], "new": sources[1]},
                "changes": view_part,
            })
            field_changes = other_part
            if not field_changes:
                continue

        # There is no lifecycle bucket on this site; the `lifecycle` key is
        # still emitted, always empty, so a consumer written against the
        # family diff shape does not have to branch.

        # A small numeric move rather than an event -- see
        # `_within_tolerance`. Only when the ONLY differences are numeric:
        # a title or an owner changing alongside is a real change whatever
        # the size of the move.
        if (all(f in count_fields for f in field_changes)
                and _within_tolerance(before, after, field_changes,
                                      price_tolerance_pct, count_fields)):
            within_tolerance.append({"sku": sku, "title": after.get("title"),
                                     "changes": field_changes})
            continue

        changed.append({"sku": sku, "title": after.get("title"),
                        "changes": field_changes})

    return {
        "mode": mode,
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
          f"different routes), "
          f"{len(result.get('within_tolerance', []))} within the "
          f"tolerance ({result.get('mode')} run).")
    for p in result["added"]:
        print(f"  + {p.get('sku')}  {p.get('title')}  "
              f"{p.get('price')} {p.get('currency') or ''}".rstrip())
    for p in result["removed"]:
        print(f"  - {p.get('sku')}  {p.get('title')}  "
              f"{p.get('price')} {p.get('currency') or ''}".rstrip())
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result.get("within_tolerance", []):
        moves = ", ".join(
            f"{f}: {v['old']} -> {v['new']}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {moves}  [within --price-"
              f"tolerance-pct: a small move, not an event]")
    for c in result["source_changed"]:
        src = c["data_source"]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[data_source {src['old']!r} -> {src['new']!r}: the two runs "
              f"read different routes, and a DOM-fallback row carries no "
              f"prices, so this is not a site-side change]")
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
    # `removed` (the ordering is a column, not a sidecar footnote).
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
            # This tool's whole premise is one row per `sku`. A mode that
            # produces many rows per sku would give a diff whose every line
            # is an artefact of two rows sharing an id, so it is refused
            # outright rather than answered. All three current modes
            # qualify; the check is here so that adding one that does not is
            # caught rather than discovered.
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

    # WHICH ADDRESS ANSWERED. `source` is the host that served a row; on
    # this site it is `opensea.io` on every row, because the locales are
    # PATHS under one host. Two runs that each landed on a DIFFERENT single
    # host would make `added` and `removed` describe the address rather than
    # the catalogue, so that pair is refused.
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
            f"the two runs landed on different hosts ({sources}), so added "
            f"and removed would describe the address change rather than "
            f"anything on OpenSea.")

    if not problems:
        return True

    # A generic headline, because the reasons below are not only about
    # completeness: a mode or ordering mismatch is refused too, and a
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
                   help="Treat a move smaller than PCT%% in the numeric "
                        "columns (a floor price, an offer, a collection's "
                        "volume, sales or owners) as noise rather than an "
                        "event: reported separately and ignored by "
                        "--fail-on-change. Default 0 — report every move. "
                        "A change in a non-numeric column (an owner, a "
                        "marketplace, a title) is never absorbed.")
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

    mode = None
    for path in (args.new, args.old):
        _, meta = _run_status(path)
        if meta and meta.get("mode"):
            mode = meta["mode"]
            break
    if mode is not None and mode not in TRACKED_FIELDS_BY_MODE:
        print(f"[!] This tool cannot compare a {mode!r} run.")
        return 2
    result = diff_products(old, new, price_tolerance_pct=args.price_tolerance_pct,
                           mode=mode)
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    # Neither `source_changed` nor `within_tolerance` is a reason to fail.
    # The first means our two runs read different routes; the second means a
    # number moved by less than the reader asked to hear about. Alerting on
    # either would train whoever reads the alert to ignore it.
    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
