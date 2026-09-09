from __future__ import annotations

import json
import statistics
from collections import defaultdict, deque
from pathlib import Path

import radar

ROOT = Path(__file__).resolve().parent


def summary(rows):
    result = {"count": len(rows)}
    for h in (1, 3, 5, 10, 20):
        values = [x[f"return_{h}d_pct"] for x in rows if x.get(f"return_{h}d_pct") is not None]
        result[f"{h}d"] = {"samples": len(values), "mean_pct": round(statistics.mean(values), 2) if values else None, "median_pct": round(statistics.median(values), 2) if values else None, "win_rate_pct": round(sum(v > 0 for v in values) / len(values) * 100, 1) if values else None}
    return result


def metrics(rows, day_index, market_volume):
    recent = [r for r in rows if day_index - r[0] <= 5]
    older = [r for r in rows if 5 < day_index - r[0] <= 20]
    window = [r for r in rows if day_index - r[0] <= 20]
    if not recent:
        return None
    recent_lots = sum(r[2] - r[3] for r in recent)
    recent_amount = sum(r[4] - r[5] for r in recent)
    older_lots = sum(r[2] - r[3] for r in older)
    inventory = peak = 0.0
    for r in window:
        inventory = max(0.0, inventory + r[2] - r[3])
        peak = max(peak, inventory)
    retention = inventory / peak if peak else 0
    return {
        "recent_lots": recent_lots, "recent_amount": recent_amount,
        "older_lots": older_lots, "inventory": inventory,
        "retention": retention,
        "active_buy_days": sum((r[2] - r[3]) > 0 for r in recent),
        "volume_share": recent_lots / market_volume if market_volume else 0,
    }


def qualified_history(stats):
    returns = stats["returns"]
    if len(returns) < 8 or len(stats["stocks"]) < 3:
        return False
    positive = [x for x in returns if x > 0]
    contribution = max(positive, default=0) / sum(positive) if positive else 1
    return sum(x > 0 for x in returns) / len(returns) >= .60 and statistics.mean(returns) > 0 and statistics.median(returns) > 0 and contribution <= .40


def run():
    events = json.loads((ROOT / "data/local/research_events.json").read_text(encoding="utf-8"))
    event_map = defaultdict(list)
    for event in events:
        event_map[event["event_date"]].append(event)
    trading_dates = json.loads((ROOT / "data/local/trading_dates.json").read_text(encoding="utf-8"))
    date_index = {date: i for i, date in enumerate(trading_dates)}
    broker_files = {radar.source_date(p): p for p in radar.report_dir("EMdss004").glob("EMdss004.*-C.csv")}
    market = radar.load_market_history()
    market_volume = {}
    for stock_id, rows in market.items():
        rows.sort(key=lambda x: x["date"])
        for i, row in enumerate(rows):
            market_volume[(row["date"], stock_id)] = sum(x["volume_lots"] for x in rows[max(0, i - 20):i])

    flows = defaultdict(lambda: defaultdict(deque))
    history = defaultdict(lambda: {"returns": [], "stocks": set()})
    maturity = defaultdict(list)
    enriched = []
    for date in trading_dates:
        idx = date_index[date]
        for broker_id, stock_id, value in maturity.pop(date, []):
            history[broker_id]["returns"].append(value)
            history[broker_id]["stocks"].add(stock_id)
        for event in event_map.get(date, []):
            stock_id = event["stock_id"]
            candidates = []
            total_volume = market_volume.get((date, stock_id), 0)
            for broker_id, rows in flows[stock_id].items():
                value = metrics(rows, idx, total_volume)
                if not value or value["recent_lots"] <= 0 or value["active_buy_days"] < 2 or value["retention"] < .70:
                    continue
                value["broker_id"] = broker_id
                value["new_build"] = value["volume_share"] >= .01 and max(value["older_lots"], 0) <= value["recent_lots"] * .10
                value["high_accuracy"] = qualified_history(history[broker_id])
                candidates.append(value)
            candidates.sort(key=lambda x: x["recent_amount"], reverse=True)
            positive_amount = sum(max(x["recent_amount"], 0) for x in candidates)
            top_share = max((x["recent_amount"] for x in candidates), default=0) / positive_amount if positive_amount else 1
            new_count = sum(x["new_build"] for x in candidates)
            multi = len(candidates) >= 3 and sum(x["recent_lots"] for x in candidates) / total_volume >= .03 if total_volume else False
            multi = bool(multi and top_share <= .70 and new_count >= 2)
            accurate = any(x["high_accuracy"] for x in candidates)
            event = {**event, "broker_signal": {"qualified_brokers": len(candidates), "new_build_count": new_count, "multi_build": multi, "high_accuracy_add": accurate, "largest_capital_share_pct": round(top_share * 100, 1), "broker_ids": [x["broker_id"] for x in candidates[:10]]}}
            enriched.append(event)
            if event.get("return_5d_pct") is not None and idx + 5 < len(trading_dates):
                for item in candidates:
                    maturity[trading_dates[idx + 5]].append((item["broker_id"], stock_id, event["return_5d_pct"]))
        path = broker_files.get(date)
        if path:
            daily = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
            for row in radar.body_rows(path):
                if len(row) < 7:
                    continue
                stock_id, broker_id, price = row[1].strip(), row[3].strip(), radar.number(row[4])
                buy, sell = radar.number(row[5]) / 1000, radar.number(row[6]) / 1000
                item = daily[(stock_id, broker_id)]
                item[0] += buy; item[1] += sell; item[2] += price * buy * 1000; item[3] += price * sell * 1000
            for (stock_id, broker_id), x in daily.items():
                queue = flows[stock_id][broker_id]
                queue.append((idx, date, x[0], x[1], x[2], x[3]))
                while queue and idx - queue[0][0] > 20:
                    queue.popleft()

    groups = {
        "all_first_breakouts": enriched,
        "new_build": [x for x in enriched if x["broker_signal"]["new_build_count"] >= 1],
        "multi_build": [x for x in enriched if x["broker_signal"]["multi_build"]],
        "high_accuracy_add": [x for x in enriched if x["broker_signal"]["high_accuracy_add"]],
        "strict_combination": [x for x in enriched if x["broker_signal"]["new_build_count"] >= 1 and x["broker_signal"]["multi_build"] and x["broker_signal"]["high_accuracy_add"]],
    }
    output = {
        "generated_at": radar.datetime.now().astimezone().isoformat(timespec="seconds"),
        "point_in_time": True,
        "broker_data_cutoff": "事件日前一完整交易日",
        "definitions": {"new_build": "近5日至少2日淨買、留存>=70%、新買>=前20日市場量1%、前段庫存<=新買10%", "multi_build": "至少3家留存建倉、其中2家新建倉、合計>=市場量3%、最大資金占比<=70%", "high_accuracy_add": "事前>=8次成熟事件、跨>=3股、5日勝率>=60%、平均與中位數>0、單一獲利貢獻<=40%"},
        "groups": {name: summary(rows) for name, rows in groups.items()},
        "events": enriched,
    }
    radar.save_json(ROOT / "data/local/broker_backtest.json", output)
    public_events = []
    for event in enriched[-100:]:
        signal = {k: v for k, v in event["broker_signal"].items() if k != "broker_ids"}
        public_events.append({**event, "broker_signal": signal})
    radar.save_json(ROOT / radar.CONFIG["site_directory"] / "data/broker_backtest.json", {**output, "events": public_events})
    print(json.dumps(output["groups"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
