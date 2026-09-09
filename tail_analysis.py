from __future__ import annotations

import json
import statistics
from pathlib import Path

import radar


ROOT = Path(__file__).resolve().parent


def stats(rows, horizon=20):
    values = [x[f"return_{horizon}d_pct"] for x in rows if x.get(f"return_{horizon}d_pct") is not None]
    return {
        "events": len(values),
        "mean_pct": round(statistics.mean(values), 2) if values else None,
        "median_pct": round(statistics.median(values), 2) if values else None,
        "win_rate_pct": round(sum(x > 0 for x in values) / len(values) * 100, 1) if values else None,
        "return_80_plus_rate_pct": round(sum(x >= 80 for x in values) / len(values) * 100, 1) if values else None,
        "return_200_plus_count": sum(x >= 200 for x in values),
    }


def run():
    source = json.loads((ROOT / "data/local/broker_backtest.json").read_text(encoding="utf-8"))
    events = source["events"]
    broad = [x for x in events if x["broker_signal"]["qualified_brokers"] >= 40 and x["broker_signal"]["largest_capital_share_pct"] <= 20]
    broad_volume = [x for x in broad if x["volume_multiple"] >= 5]
    concentrated = [x for x in events if x["broker_signal"]["qualified_brokers"] <= 5 and x["broker_signal"]["largest_capital_share_pct"] >= 60]
    extreme = sorted([x for x in events if (x.get("return_20d_pct") or -999) >= 200], key=lambda x: x["return_20d_pct"], reverse=True)
    result = {
        "generated_at": radar.datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "exploratory_hypothesis_not_out_of_sample",
        "definitions": {
            "broad_ignition": "合格留存建倉分點>=40且最大分點資金占比<=20%",
            "broad_ignition_with_volume": "廣泛點火且突破量>=前20日中位量5倍",
            "concentrated_control": "合格分點<=5且最大分點資金占比>=60%",
        },
        "profiles": {
            "all": stats(events),
            "broad_ignition": stats(broad),
            "broad_ignition_with_volume": stats(broad_volume),
            "concentrated_control": stats(concentrated),
        },
        "extreme_200_plus": [{k: x.get(k) for k in ("stock_id", "name", "event_date", "trigger_pct_proxy", "volume_multiple", "prior_range_pct", "return_5d_pct", "return_20d_pct")} | {"qualified_brokers": x["broker_signal"]["qualified_brokers"], "new_build_count": x["broker_signal"]["new_build_count"], "largest_capital_share_pct": x["broker_signal"]["largest_capital_share_pct"]} for x in extreme],
        "warning": "門檻由同一批極端案例探索而來，只能列為候選雷達；需按年份走勢外推與未來樣本驗證。",
    }
    radar.save_json(ROOT / "data/local/tail_analysis.json", result)
    radar.save_json(ROOT / radar.CONFIG["site_directory"] / "data/tail_analysis.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
