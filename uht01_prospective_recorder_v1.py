#!/usr/bin/env python3
"""UHT-01 prospective recorder v1.

Frozen purpose: record Card #1 and Card #2 unchanged from 2026-09-15 onward.
No parameter optimization, no rescue filters, no position sizing.

Input: native MNQ 1-minute OHLC CSV containing the target RTH session and enough
same-contract history to identify the previous completed RTH high for Card #1.
Accepted timestamp columns: timestamp, time, datetime, date_time, window_start.
`window_start` may be nanoseconds since Unix epoch (Massive futures aggregates).

Output: deterministic candidate decision rows plus a session audit row.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import math
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
TICK = 0.25
BASE_COST_USD = 4.0
POINT_VALUE_MNQ = 2.0
IDENTITY = "UHT01_PHASE1_IDENTITY_FREEZE_v2_2026-09-14.json"

DECISION_COLUMNS = [
    "record_id","candidate","session_date_et","contract","signal_time_et","side",
    "signal_price_or_close","structural_stop","prescreen_risk_points","entry_time_et",
    "entry_price","actual_risk_points","target_price","exit_time_et","exit_price",
    "exit_reason","net_usd_1mnq_baseline","plus1_tick_side_net_usd",
    "plus2_ticks_side_net_usd","plus4_ticks_side_net_usd","data_source",
    "data_integrity_status","rule_hash_or_identity","notes"
]

SESSION_AUDIT_COLUMNS = [
    "session_date_et","contract","processed_at_utc","target_rth_rows","target_first_et",
    "target_last_et","previous_completed_rth_date","previous_rth_rows","previous_rth_high",
    "duplicate_timestamp_rows","required_window_contiguous","c1_state","c1_decisions",
    "c2_state","c2_decisions","source_fingerprint_sha256","recorder_sha256","identity",
    "notes"
]

@dataclass
class Trade:
    candidate: str
    session_date_et: str
    contract: str
    signal_time_et: str
    side: str
    signal_price_or_close: float
    structural_stop: float
    prescreen_risk_points: float
    entry_time_et: str
    entry_price: float
    actual_risk_points: float
    target_price: float
    exit_time_et: str
    exit_price: float
    exit_reason: str
    net_usd_1mnq_baseline: float
    plus1_tick_side_net_usd: float
    plus2_ticks_side_net_usd: float
    plus4_ticks_side_net_usd: float
    data_source: str
    data_integrity_status: str
    rule_hash_or_identity: str
    notes: str = ""

    def to_row(self) -> dict:
        d = asdict(self)
        basis = f"{self.candidate}|{self.session_date_et}|{self.contract}|{self.signal_time_et}|{self.side}|{self.entry_time_et}|{self.entry_price:.2f}"
        d["record_id"] = hashlib.sha256(basis.encode()).hexdigest()[:20]
        return {k: d.get(k, "") for k in DECISION_COLUMNS}


def second_thursday(year: int, month: int) -> date:
    c = calendar.Calendar(firstweekday=calendar.MONDAY)
    thursdays = [d for d in c.itermonthdates(year, month) if d.month == month and d.weekday() == 3]
    return thursdays[1]


def active_mnq_contract(session_date: date) -> str:
    """Frozen Harvey roll: switch at 18:00 ET on second Thursday of Mar/Jun/Sep/Dec.

    Therefore the RTH session *on* the roll Thursday still uses the old contract;
    the next RTH session uses the new quarterly contract.
    """
    y = session_date.year
    mar, jun, sep, dec = [second_thursday(y, m) for m in (3, 6, 9, 12)]
    if session_date <= mar:
        code, yy = "H", y
    elif session_date <= jun:
        code, yy = "M", y
    elif session_date <= sep:
        code, yy = "U", y
    elif session_date <= dec:
        code, yy = "Z", y
    else:
        code, yy = "H", y + 1
    # The switch happens after the RTH of the second Thursday.
    if session_date > dec:
        code, yy = "H", y + 1
    # At dates after each roll Thursday, advance to next contract.
    if mar < session_date <= jun:
        code, yy = "M", y
    if jun < session_date <= sep:
        code, yy = "U", y
    if sep < session_date <= dec:
        code, yy = "Z", y
    # Jan/Feb (and through Mar roll Thursday) H; after Dec roll Thursday H next year.
    if session_date.month == 12 and session_date > dec:
        code, yy = "H", y + 1
    return f"MNQ{code}{yy % 10}"


def _parse_timestamp_series(df: pd.DataFrame) -> pd.Series:
    candidates = ["timestamp", "time", "datetime", "date_time", "Date and time", "window_start"]
    col = next((c for c in candidates if c in df.columns), None)
    if col is None:
        raise ValueError(f"No recognized timestamp column. Need one of {candidates}")
    s = df[col]
    if col == "window_start" and pd.api.types.is_numeric_dtype(s):
        # Massive futures aggregates use nanoseconds.
        ts = pd.to_datetime(s.astype("int64"), unit="ns", utc=True)
    else:
        raw = pd.to_datetime(s, errors="coerce", format="mixed")
        if raw.isna().any():
            raise ValueError("Unparseable timestamp values")
        # Naive textual timestamps are treated as ET by design. Aware timestamps
        # are converted to UTC. Massive numeric window_start is handled above.
        try:
            tz = raw.dt.tz
        except AttributeError:
            # Mixed-aware object fallback: normalize element by element.
            vals = []
            for x in raw:
                t = pd.Timestamp(x)
                if t.tzinfo is None:
                    t = t.tz_localize(ET)
                vals.append(t.tz_convert(UTC))
            return pd.Series(pd.DatetimeIndex(vals), index=s.index)
        if tz is None:
            raw = raw.dt.tz_localize(ET, ambiguous="raise", nonexistent="raise")
        ts = raw.dt.tz_convert(UTC)
    return ts


def load_bars(paths: Iterable[str]) -> pd.DataFrame:
    frames = []
    for p in paths:
        df = pd.read_csv(p)
        df = df.copy()
        df["_source_file"] = Path(p).name
        df["ts_utc"] = _parse_timestamp_series(df)
        rename = {}
        for wanted in ["open", "high", "low", "close", "volume", "ticker", "contract"]:
            if wanted not in df.columns:
                for c in df.columns:
                    if c.lower().strip() == wanted:
                        rename[c] = wanted
                        break
        df = df.rename(columns=rename)
        for c in ["open", "high", "low", "close"]:
            if c not in df.columns:
                raise ValueError(f"Missing required OHLC column {c} in {p}")
            df[c] = pd.to_numeric(df[c], errors="raise")
        if "contract" not in df.columns:
            if "ticker" in df.columns:
                df["contract"] = df["ticker"].astype(str)
            else:
                df["contract"] = ""
        frames.append(df[["ts_utc","open","high","low","close","contract","_source_file"] + (["volume"] if "volume" in df.columns else [])])
    out = pd.concat(frames, ignore_index=True).sort_values("ts_utc").reset_index(drop=True)
    out["ts_et"] = out["ts_utc"].dt.tz_convert(ET)
    out["date_et"] = out["ts_et"].dt.date
    out["hm"] = out["ts_et"].dt.strftime("%H:%M")
    return out


def fingerprint_bars(df: pd.DataFrame) -> str:
    cols = ["ts_utc","open","high","low","close","contract"]
    txt = df[cols].to_csv(index=False, lineterminator="\n")
    return hashlib.sha256(txt.encode()).hexdigest()


def recorder_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def rth(df: pd.DataFrame, d: date, contract: str) -> pd.DataFrame:
    x = df[(df.date_et == d) & (df.contract.astype(str) == contract) & (df.hm >= "09:30") & (df.hm < "16:00")].copy()
    return x.sort_values("ts_et").reset_index(drop=True)


def previous_completed_rth(df: pd.DataFrame, d: date, contract: str) -> Optional[pd.DataFrame]:
    prior_dates = sorted({x for x in df.loc[(df.date_et < d) & (df.contract.astype(str) == contract), "date_et"]})
    for pdte in reversed(prior_dates):
        x = rth(df, pdte, contract)
        if len(x) >= 1:
            return x
    return None


def required_window_contiguous(day: pd.DataFrame) -> bool:
    # Candidate logic can create an entry no later than 12:03 and resolves within 15 bars.
    # Require a complete 09:30..12:20 ET minute spine for prospective integrity.
    if day.empty:
        return False
    times = set(day["hm"])
    start = datetime(2000,1,1,9,30)
    end = datetime(2000,1,1,12,20)
    cur = start
    needed = []
    while cur <= end:
        needed.append(cur.strftime("%H:%M"))
        cur += timedelta(minutes=1)
    return all(t in times for t in needed)


def _net_usd(side: str, entry: float, exit_: float) -> float:
    gross = (exit_ - entry) * POINT_VALUE_MNQ if side == "LONG" else (entry - exit_) * POINT_VALUE_MNQ
    return gross - BASE_COST_USD


def _stress(net: float, ticks_per_side: int) -> float:
    # MNQ tick value = $0.50. Extra ticks/side => $1.00 RT per tick/side level.
    return net - ticks_per_side * 1.0


def resolve_trade(day: pd.DataFrame, entry_idx: int, side: str, entry: float, stop: float, target: float):
    # Hold entry bar + next 14 bars = 15 one-minute bars total.
    last_idx = min(entry_idx + 14, len(day)-1)
    for j in range(entry_idx, last_idx + 1):
        b = day.iloc[j]
        if side == "SHORT":
            stop_hit = b.high >= stop
            target_hit = b.low <= target - TICK
            if stop_hit:  # STOP_FIRST when both true
                px = max(stop, b.open) if b.open >= stop else stop
                return j, float(px), "STOP"
            if target_hit:
                return j, float(target), "TARGET"
        else:
            stop_hit = b.low <= stop
            target_hit = b.high >= target + TICK
            if stop_hit:
                px = min(stop, b.open) if b.open <= stop else stop
                return j, float(px), "STOP"
            if target_hit:
                return j, float(target), "TARGET"
    b = day.iloc[last_idx]
    return last_idx, float(b.close), "TIME"


def c1(day: pd.DataFrame, prev: Optional[pd.DataFrame], source: str, integrity: str) -> tuple[list[Trade], str]:
    if prev is None or prev.empty:
        return [], "NOT_EVALUABLE_NO_SAME_CONTRACT_PREVIOUS_RTH"
    pdh = float(prev.high.max())
    prior_touch = False
    reject_consumed = False
    for i, b in day.iterrows():
        reject = (b.high >= pdh + 1.0) and (b.close < pdh)
        had_prior_touch = prior_touch
        if reject and not reject_consumed:
            reject_consumed = True
            eligible_time = "09:35" <= b.hm < "12:00"
            stop = float(b.high + 1.0)
            prescreen = stop - float(b.close)
            if eligible_time and had_prior_touch and 4.0 <= prescreen <= 20.0:
                if i + 1 >= len(day):
                    return [], "RISK_PASS_BUT_MISSING_NEXT_OPEN"
                eb = day.iloc[i+1]
                entry = float(eb.open)
                risk = stop - entry
                if not (4.0 <= risk <= 20.0):
                    return [], "FIRST_REJECTION_CONSUMED_FILL_RISK_MISMATCH"
                target = entry - 2.5 * risk
                ex_i, ex_px, reason = resolve_trade(day, i+1, "SHORT", entry, stop, target)
                ex = day.iloc[ex_i]
                net = _net_usd("SHORT", entry, ex_px)
                tr = Trade(
                    candidate="C1_PDHR_REPEAT_SWEEP_SHORT", session_date_et=str(b.date_et), contract=str(b.contract),
                    signal_time_et=b.ts_et.strftime("%Y-%m-%d %H:%M:%S%z"), side="SHORT",
                    signal_price_or_close=float(b.close), structural_stop=stop, prescreen_risk_points=prescreen,
                    entry_time_et=eb.ts_et.strftime("%Y-%m-%d %H:%M:%S%z"), entry_price=entry,
                    actual_risk_points=risk, target_price=target,
                    exit_time_et=ex.ts_et.strftime("%Y-%m-%d %H:%M:%S%z"), exit_price=ex_px,
                    exit_reason=reason, net_usd_1mnq_baseline=round(net,2),
                    plus1_tick_side_net_usd=round(_stress(net,1),2), plus2_ticks_side_net_usd=round(_stress(net,2),2),
                    plus4_ticks_side_net_usd=round(_stress(net,4),2), data_source=source,
                    data_integrity_status=integrity, rule_hash_or_identity=IDENTITY,
                    notes=f"PDH={pdh:.2f}; first rejection consumed; actual-fill target"
                )
                return [tr], "QUALIFYING_TRADE"
            # First rejection is consumed even if it fails filters.
            if not eligible_time:
                return [], "FIRST_REJECTION_CONSUMED_TIME_FAIL"
            if not had_prior_touch:
                return [], "FIRST_REJECTION_CONSUMED_NO_PRIOR_TOUCH"
            if not (4.0 <= prescreen <= 20.0):
                return [], "FIRST_REJECTION_CONSUMED_PRESCREEN_RISK_FAIL"
        if b.high >= pdh:
            prior_touch = True
    return [], "NO_REJECTION"


def c2(day: pd.DataFrame, source: str, integrity: str) -> tuple[list[Trade], str]:
    orb = day[(day.hm >= "09:30") & (day.hm < "09:45")]
    if len(orb) < 15:
        return [], "NOT_EVALUABLE_INCOMPLETE_OR15"
    orh = float(orb.high.max()); orl = float(orb.low.min())
    sweep_i = None; side = None
    for i, b in day.iterrows():
        if not ("09:45" <= b.hm < "12:00"):
            continue
        short = (b.high >= orh + 1.0) and (b.close < orh)
        long = (b.low <= orl - 1.0) and (b.close > orl)
        if short or long:
            sweep_i = i
            side = "SHORT" if short else "LONG"  # SHORT precedence if both
            sweep = b
            break
    if sweep_i is None:
        return [], "NO_SWEEP"
    midpoint = float((sweep.high + sweep.low) / 2.0)
    direct_stop = float(sweep.high + 1.0) if side == "SHORT" else float(sweep.low - 1.0)
    if sweep_i + 1 >= len(day):
        return [], "SWEEP_CONSUMED_MISSING_BAR_PLUS1"
    plus1 = day.iloc[sweep_i+1]
    direct_risk = direct_stop - float(plus1.open) if side == "SHORT" else float(plus1.open) - direct_stop
    if not (direct_risk > 20.0):
        return [], "SWEEP_CONSUMED_EXTREME_GATE_FAIL"
    # First midpoint secondary occurrence in bars +1..+3 consumes setup, regardless of risk gate.
    for j in range(sweep_i+1, min(sweep_i+4, len(day))):
        b = day.iloc[j]
        secondary = ((b.high >= midpoint and b.close < midpoint) if side == "SHORT"
                     else (b.low <= midpoint and b.close > midpoint))
        if not secondary:
            continue
        stop = float(b.high + 1.0) if side == "SHORT" else float(b.low - 1.0)
        prescreen = stop - float(b.close) if side == "SHORT" else float(b.close) - stop
        if not (4.0 <= prescreen <= 20.0):
            return [], "SECONDARY_CONSUMED_PRESCREEN_RISK_FAIL"
        if j + 1 >= len(day):
            return [], "SECONDARY_CONSUMED_MISSING_NEXT_OPEN"
        eb = day.iloc[j+1]
        entry = float(eb.open)
        risk = stop - entry if side == "SHORT" else entry - stop
        if not (4.0 <= risk <= 20.0):
            return [], "SECONDARY_CONSUMED_FILL_RISK_MISMATCH"
        target = entry - 2.5*risk if side == "SHORT" else entry + 2.5*risk
        ex_i, ex_px, reason = resolve_trade(day, j+1, side, entry, stop, target)
        ex = day.iloc[ex_i]
        net = _net_usd(side, entry, ex_px)
        tr = Trade(
            candidate="C2_EXTREME_OR15_SECONDARY", session_date_et=str(b.date_et), contract=str(b.contract),
            signal_time_et=b.ts_et.strftime("%Y-%m-%d %H:%M:%S%z"), side=side,
            signal_price_or_close=float(b.close), structural_stop=stop, prescreen_risk_points=prescreen,
            entry_time_et=eb.ts_et.strftime("%Y-%m-%d %H:%M:%S%z"), entry_price=entry,
            actual_risk_points=risk, target_price=target,
            exit_time_et=ex.ts_et.strftime("%Y-%m-%d %H:%M:%S%z"), exit_price=ex_px,
            exit_reason=reason, net_usd_1mnq_baseline=round(net,2),
            plus1_tick_side_net_usd=round(_stress(net,1),2), plus2_ticks_side_net_usd=round(_stress(net,2),2),
            plus4_ticks_side_net_usd=round(_stress(net,4),2), data_source=source,
            data_integrity_status=integrity, rule_hash_or_identity=IDENTITY,
            notes=f"ORH={orh:.2f}; ORL={orl:.2f}; sweep_mid={midpoint:.2f}; direct_risk={direct_risk:.2f}"
        )
        return [tr], "QUALIFYING_TRADE"
    return [], "SWEEP_CONSUMED_NO_SECONDARY_IN_3_BARS"


def append_unique_csv(path: str, rows: list[dict], columns: list[str], key: Optional[str] = None):
    p = Path(path)
    existing = pd.DataFrame(columns=columns)
    if p.exists() and p.stat().st_size > 0:
        existing = pd.read_csv(p)
    add = pd.DataFrame(rows, columns=columns)
    if add.empty:
        if not p.exists():
            pd.DataFrame(columns=columns).to_csv(p, index=False)
        return
    out = pd.concat([existing, add], ignore_index=True)
    if key and key in out.columns:
        out = out.drop_duplicates(subset=[key], keep="first")
    out.to_csv(p, index=False)


def process(paths: list[str], session_date: str, contract: Optional[str], decisions_out: str, audit_out: str) -> dict:
    bars = load_bars(paths)
    d = date.fromisoformat(session_date)
    expected = active_mnq_contract(d)
    contract = contract or expected
    if contract != expected:
        raise ValueError(f"Contract {contract} violates frozen deterministic roll for {d}; expected {expected}")
    if (bars["contract"].astype(str).str.len() == 0).all():
        bars["contract"] = contract
    target = rth(bars, d, contract)
    if target.empty:
        raise ValueError(f"No target RTH bars for {d} {contract}")
    dupes = int(target.ts_utc.duplicated().sum())
    contiguous = required_window_contiguous(target)
    integrity = "PASS" if dupes == 0 and contiguous else "FAIL"
    prev = previous_completed_rth(bars, d, contract)
    source = ";".join(sorted(set(target._source_file)))
    c1_trades, c1_state = c1(target, prev, source, integrity)
    c2_trades, c2_state = c2(target, source, integrity)
    decision_rows = [t.to_row() for t in c1_trades + c2_trades]
    append_unique_csv(decisions_out, decision_rows, DECISION_COLUMNS, key="record_id")
    prev_date = str(prev.iloc[0].date_et) if prev is not None and not prev.empty else ""
    prev_high = float(prev.high.max()) if prev is not None and not prev.empty else math.nan
    audit = {
        "session_date_et": session_date,
        "contract": contract,
        "processed_at_utc": datetime.now(tz=UTC).isoformat(),
        "target_rth_rows": len(target),
        "target_first_et": target.iloc[0].ts_et.isoformat(),
        "target_last_et": target.iloc[-1].ts_et.isoformat(),
        "previous_completed_rth_date": prev_date,
        "previous_rth_rows": 0 if prev is None else len(prev),
        "previous_rth_high": "" if math.isnan(prev_high) else round(prev_high, 8),
        "duplicate_timestamp_rows": dupes,
        "required_window_contiguous": contiguous,
        "c1_state": c1_state,
        "c1_decisions": len(c1_trades),
        "c2_state": c2_state,
        "c2_decisions": len(c2_trades),
        "source_fingerprint_sha256": fingerprint_bars(bars[(bars.contract.astype(str)==contract) & (bars.date_et <= d)]),
        "recorder_sha256": recorder_sha256(),
        "identity": IDENTITY,
        "notes": f"expected_contract={expected}; prospective unchanged recorder"
    }
    append_unique_csv(audit_out, [audit], SESSION_AUDIT_COLUMNS, key="session_date_et")
    return {"audit": audit, "decisions": decision_rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", nargs="+", required=True)
    ap.add_argument("--session-date", required=True, help="YYYY-MM-DD ET")
    ap.add_argument("--contract", default=None)
    ap.add_argument("--decisions-out", required=True)
    ap.add_argument("--audit-out", required=True)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    result = process(args.bars, args.session_date, args.contract, args.decisions_out, args.audit_out)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()