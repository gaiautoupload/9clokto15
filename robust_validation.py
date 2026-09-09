from __future__ import annotations

import json
import statistics
from pathlib import Path

import radar


ROOT = Path(__file__).resolve().parent
BUY_FEE = 0.001425
SELL_FEE_AND_TAX = 0.001425 + 0.003
SLIPPAGE_EACH_SIDE = 0.01


def describe(values):
    return {"samples": len(values), "mean_pct": round(statistics.mean(values), 2) if values else None, "median_pct": round(statistics.median(values), 2) if values else None, "win_rate_pct": round(sum(x > 0 for x in values) / len(values) * 100, 1) if values else None, "return_80_plus_rate_pct": round(sum(x >= 80 for x in values) / len(values) * 100, 1) if values else None}


def net_return(entry, exit_price):
    paid = entry * (1 + BUY_FEE + SLIPPAGE_EACH_SIDE)
    received = exit_price * (1 - SELL_FEE_AND_TAX - SLIPPAGE_EACH_SIDE)
    return (received / paid - 1) * 100


def run():
    source = json.loads((ROOT / "data/local/broker_backtest.json").read_text(encoding="utf-8"))
    history = radar.load_market_history()
    positions = {}
    for stock_id, rows in history.items():
        rows.sort(key=lambda x: x["date"])
        positions[stock_id] = {row["date"]: i for i, row in enumerate(rows)}
    rule = lambda x: x["broker_signal"]["qualified_brokers"] >= 40 and x["broker_signal"]["largest_capital_share_pct"] <= 20 and x["volume_multiple"] >= 5
    buckets = {"discovery_2020_2023": [], "validation_2024_2026": []}
    for event in source["events"]:
        if not rule(event):
            continue
        rows = history[event["stock_id"]]
        i = positions[event["stock_id"]].get(event["event_date"])
        if i is None or i < 1 or i + 21 >= len(rows):
            continue
        threshold = rows[i - 1]["vwap"] * 1.15
        exit_20 = rows[i + 20]["vwap"]
        item = {
            "stock_id": event["stock_id"], "event_date": event["event_date"],
            "threshold_fill_net_pct": net_return(threshold, exit_20),
            "event_high_fill_net_pct": net_return(rows[i]["high"], exit_20),
            "next_session_vwap_net_pct": net_return(rows[i + 1]["vwap"], rows[i + 21]["vwap"]),
        }
        key = "discovery_2020_2023" if event["event_date"] < "20240101" else "validation_2024_2026"
        buckets[key].append(item)
    result = {
        "generated_at": radar.datetime.now().astimezone().isoformat(timespec="seconds"),
        "rule": "合格留存分點>=40、最大分點占比<=20%、突破量>=20日中位量5倍",
        "cost_model": {"buy_fee_pct": BUY_FEE * 100, "sell_fee_pct": BUY_FEE * 100, "sell_tax_pct": 0.3, "slippage_each_side_pct": SLIPPAGE_EACH_SIDE * 100},
        "warning": "2024後區間是回溯式次級驗證，不是真正未見資料；規則探索已接觸全資料。真正樣本外驗證必須從現在起凍結規則。",
        "periods": {},
    }
    for name, rows in buckets.items():
        result["periods"][name] = {scenario: describe([x[scenario] for x in rows]) for scenario in ("threshold_fill_net_pct", "event_high_fill_net_pct", "next_session_vwap_net_pct")}
    radar.save_json(ROOT / "data/local/robust_validation.json", result)
    radar.save_json(ROOT / radar.CONFIG["site_directory"] / "data/robust_validation.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
