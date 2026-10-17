#!/usr/bin/env python3
"""
Appends each trading day's closing prices to prices.json.

Runs nightly on GitHub Actions. Design rule: when in doubt, write nothing and fail loudly.
A failed run leaves the site on yesterday's prices and GitHub emails the repo owner.
A wrong price written silently is the one outcome this script exists to prevent.

Safeguards, in order:
  1. Currency   - every Yahoo listing must report the currency fund.json expects (USD for the six
                  holdings, SGD for FX). Catches the GBp/EUR/CHF lines of the same funds.
  2. Overlap    - Yahoo's closes are compared against every close already stored for the same dates,
                  including the IBKR-seeded history. Any gap over 1% (0.5% for FX) aborts the run.
                  Catches wrong share class, wrong venue, or a mis-mapped symbol.
  3. Range      - FX must sit between fund.json's min and max. Catches an inverted rate.
  4. Daily move - a close moving more than maxMove from the previous stored close aborts the run.
  5. Finality   - a bar dated D is only accepted after D 22:20 UTC (after the NYSE close and the
                  FX day roll), so a still-forming intraday price is never recorded as a close.
  6. Calendar   - the LSE calendar governs. BTC and FX carry the previous close forward only when
                  their own later data proves D was a holiday for them; if their data simply hasn't
                  arrived yet, the date is deferred to the next run instead of being guessed.
Rows are append-only. Nothing already in prices.json is ever rewritten.
"""
import datetime as dt
import json
import math
import sys
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
FUND = json.loads((ROOT / "fund.json").read_text())
PRICES_PATH = ROOT / "prices.json"
PRICES = json.loads(PRICES_PATH.read_text())
CLOSES = PRICES["closes"]

FINAL_AFTER_UTC = dt.time(22, 20)
OVERLAP_TOL = 0.01
OVERLAP_TOL_FX = 0.005
LOOKBACK_DAYS = 35


def fail(msg):
    print(f"\nERROR: {msg}\nNothing was written. The site keeps its previous prices.")
    sys.exit(1)


def fetch(symbol, expect_ccy, start):
    t = yf.Ticker(symbol)
    h = t.history(start=start.isoformat(), interval="1d",
                  auto_adjust=False, actions=False, prepost=False)
    if h is None or h.empty:
        fail(f"{symbol}: Yahoo returned no data")
    meta = t.history_metadata or {}
    ccy = meta.get("currency")
    if ccy != expect_ccy:
        fail(f"{symbol}: Yahoo reports currency {ccy!r}, expected {expect_ccy!r}. "
             f"This is the wrong listing for this holding.")
    out = {}
    for ts, row in h.iterrows():
        c = row.get("Close")
        if c is None or (isinstance(c, float) and math.isnan(c)) or c <= 0:
            continue
        out[ts.date().isoformat()] = round(float(c), 6)
    print(f"  {symbol:<8} {ccy}  {meta.get('fullExchangeName') or meta.get('exchangeName') or '?':<14}"
          f" {len(out)} bars, latest {max(out) if out else '-'}")
    return out


def is_final(date_str, now):
    d = dt.date.fromisoformat(date_str)
    return now >= dt.datetime.combine(d, FINAL_AFTER_UTC, tzinfo=dt.timezone.utc)


def check_overlap(key, fetched, tol):
    worst = 0.0
    for d, v in fetched.items():
        stored = CLOSES.get(d, {}).get(key)
        if stored is None:
            continue
        diff = abs(v / stored - 1)
        worst = max(worst, diff)
        if diff > tol:
            fail(f"{key} on {d}: Yahoo {v} vs stored {stored} ({diff:.2%} apart, limit {tol:.1%}). "
                 f"The Yahoo symbol may not be the same instrument.")
    return worst


def main():
    now = dt.datetime.now(dt.timezone.utc)
    last_date = max(CLOSES)
    start = max(dt.date.fromisoformat(min(CLOSES)),
                dt.date.fromisoformat(last_date) - dt.timedelta(days=LOOKBACK_DAYS))
    print(f"Stored closes run to {last_date}. Fetching from {start}. Now {now:%Y-%m-%d %H:%M} UTC.\n")

    instruments = FUND["instruments"]
    fxspec = FUND["fx"]
    data = {i["ticker"]: fetch(i["yahoo"], i["currency"], start) for i in instruments}
    data["FX"] = fetch(fxspec["yahoo"], fxspec["currency"], start)

    print("\nCross-check against stored closes:")
    for key, series in data.items():
        tol = OVERLAP_TOL_FX if key == "FX" else OVERLAP_TOL
        print(f"  {key:<5} max gap {check_overlap(key, series, tol):.3%}")

    lse = [i["ticker"] for i in instruments if i["calendar"] == "LSE"]
    others = [i["ticker"] for i in instruments if i["calendar"] != "LSE"] + ["FX"]
    move_limit = {i["ticker"]: i["maxMove"] for i in instruments}
    move_limit["FX"] = fxspec["maxMove"]

    candidates = sorted({d for t in lse for d in data[t] if d > last_date and is_final(d, now)})
    prev = dict(CLOSES[last_date])
    added = []
    for d in candidates:
        have = [t for t in lse if d in data[t]]
        if len(have) != len(lse):
            missing = sorted(set(lse) - set(have))
            fail(f"{d}: LSE closes present for {have} but missing for {missing}. Partial day, refusing.")
        row = {t: data[t][d] for t in lse}
        deferred = False
        for k in others:
            if d in data[k]:
                row[k] = data[k][d]
            elif any(x > d for x in data[k]):
                row[k] = prev[k]
                row.setdefault("note", "")
                row["note"] = (row["note"] + f" {k} closed, previous close carried.").strip()
            else:
                print(f"\n{d}: {k} has no data for this date yet. Deferring {d} to the next run.")
                deferred = True
                break
        if deferred:
            break
        for k, v in row.items():
            if k == "note":
                continue
            if k == "FX" and not (fxspec["min"] <= v <= fxspec["max"]):
                fail(f"FX on {d} is {v}, outside {fxspec['min']}-{fxspec['max']}. Rate may be inverted.")
            if prev.get(k):
                move = abs(v / prev[k] - 1)
                if move > move_limit[k]:
                    fail(f"{k} on {d}: {prev[k]} -> {v} is a {move:.1%} move, above the "
                         f"{move_limit[k]:.0%} limit. Check before accepting.")
        row["src"] = "yahoo"
        CLOSES[d] = row
        prev = {**prev, **row}
        added.append(d)

    if not added:
        print("\nNo new final closes. prices.json unchanged.")
        return
    PRICES["updated"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    PRICES["closes"] = dict(sorted(CLOSES.items()))
    PRICES_PATH.write_text(json.dumps(PRICES, indent=1) + "\n")
    print(f"\nAdded {len(added)} day(s): {', '.join(added)}")
    for d in added:
        r = CLOSES[d]
        print("  " + d + "  " + "  ".join(f"{k} {r[k]}" for k in [*lse, *others]))


if __name__ == "__main__":
    main()
