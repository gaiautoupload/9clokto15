from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, time as clock_time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
SOURCE = Path(CONFIG["source_directory"])
LOCAL = ROOT / "data" / "local"
PUBLIC = ROOT / CONFIG["site_directory"] / "data"
DB = LOCAL / "radar.db"
DISCORD_WEBHOOK = LOCAL / "discord_webhook.txt"
DISCORD_STATE = LOCAL / "discord_alert_state.json"
MONITOR_HEALTH = LOCAL / "monitor_health.json"
MONITOR_LOG = LOCAL / "monitor.log"


def number(value) -> float:
    try:
        return float(str(value).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def body_rows(path: Path):
    for encoding in ("utf-8-sig", "cp950"):
        try:
            with path.open(encoding=encoding, newline="") as handle:
                for row in csv.reader(handle):
                    if row and row[0].strip().upper() == "BODY":
                        yield row
            return
        except UnicodeDecodeError:
            continue


def source_date(path: Path) -> str:
    match = re.search(r"\.(\d{8})-C\.csv$", path.name)
    return match.group(1) if match else ""


def report_dir(code: str) -> Path:
    candidates = [path for path in SOURCE.glob(f"{code}_*") if path.is_dir()]
    if not candidates:
        raise RuntimeError(f"找不到 {code} 資料夾：{SOURCE}")
    return max(candidates, key=lambda path: len(list(path.glob(f"{code}.*-C.csv"))))


def load_broker_names() -> dict[str, str]:
    """Load the newest official emerging-market broker/branch name table."""
    mapping: dict[str, str] = {}
    try:
        files = sorted(report_dir("EMdss001").glob("EMdss001.*-C.csv"))
        if files:
            raw = files[-1].read_bytes()
            try:
                broker_text = raw.decode("big5hkscs")
            except UnicodeDecodeError:
                broker_text = raw.decode("utf-8-sig", errors="replace")
            for row in csv.reader(broker_text.splitlines()):
                if not row or row[0].strip().upper() != "BODY":
                    continue
                if len(row) >= 4 and row[2].strip():
                    mapping[row[2].strip()] = row[3].strip()
    except RuntimeError:
        pass
    fallback = ROOT.parent / "7932_strategy" / "broker_names.csv"
    if fallback.exists():
        with fallback.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("broker_id"):
                    mapping.setdefault(row["broker_id"].strip(), row.get("broker_name", "").strip())
    return mapping


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def log_monitor(message: str) -> None:
    LOCAL.mkdir(parents=True, exist_ok=True)
    with MONITOR_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")


def update_monitor_health(status: str, detail: str) -> None:
    """Persist monitor health and notify only on meaningful daily transitions."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    previous = load_json(MONITOR_HEALTH, {})
    prior_status = previous.get("status")
    message = None
    if status == "UP" and previous.get("online_notice_date") != today:
        message = f"✅ **盤中盯盤已上線｜{today}**\n{detail}\n戰情室：https://gaiautoupload.github.io/9clokto15/"
    elif status == "UP" and prior_status == "DOWN":
        message = f"✅ **盤中盯盤已恢復｜{now:%H:%M:%S}**\n{detail}"
    elif status == "DOWN" and prior_status != "DOWN":
        message = f"⚠️ **盤中盯盤異常｜{now:%H:%M:%S}**\n{detail}\n系統會自動重試，不會因推播失敗而停止。"
    current = {
        "status": status,
        "updated_at": now.isoformat(timespec="seconds"),
        "detail": detail,
        "online_notice_date": today if status == "UP" else previous.get("online_notice_date"),
    }
    save_json(MONITOR_HEALTH, current)
    log_monitor(f"{status} {detail}")
    if message:
        try:
            post_discord(message)
        except Exception as exc:
            log_monitor(f"DISCORD_ERROR {type(exc).__name__}: {exc}")


def discord_message(item: dict) -> str:
    plan = item.get("action_plan") or {}
    routes = "、".join(item.get("tail_routes") or []) or "—"
    return (
        f"📡 **{plan.get('label', '策略更新')}｜{item.get('stock_id')} {item.get('name')}**\n"
        f"現價 `{item.get('price')}`｜漲幅 `{item.get('change_pct')}%`｜首次捕捉 `{item.get('trigger_price')}`\n"
        f"原因：{plan.get('reason', '—')}\n"
        f"試單 `{plan.get('trial_zone_low')}–{plan.get('trial_zone_high')}`｜加碼站上 `{plan.get('add_above')}`\n"
        f"減碼跌破 `{plan.get('reduce_below')}`｜出清跌破 `{plan.get('exit_below')}`\n"
        f"核心成本 `{plan.get('core_cost')}`｜核心庫存變化 `{plan.get('core_inventory_change_lots')}張`｜路徑 {routes}\n"
        f"戰情室：https://gaiautoupload.github.io/9clokto15/"
    )


def post_discord(content: str) -> None:
    if not DISCORD_WEBHOOK.exists():
        return
    url = DISCORD_WEBHOOK.read_text(encoding="utf-8").strip()
    if not url.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
        raise RuntimeError("Discord Webhook 設定格式不正確")
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "intraday-15-radar/1.0"}, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord Webhook 回傳 HTTP {response.status}")


def notify_discord(stocks: list[dict]) -> int:
    if not DISCORD_WEBHOOK.exists():
        return 0
    previous = load_json(DISCORD_STATE, {})
    current = dict(previous)
    sent = 0
    for item in stocks:
        plan = item.get("action_plan") or {}
        state = plan.get("state", "WATCH")
        event_id = item.get("event_id", item.get("stock_id", ""))
        old_state = previous.get(event_id)
        should_send = (state == "ENTRY" and old_state is None) or (state in {"ADD", "REDUCE", "EXIT"} and state != old_state)
        if should_send:
            post_discord(discord_message(item))
            sent += 1
        current[event_id] = state
    save_json(DISCORD_STATE, current)
    return sent


def load_market_history() -> dict[str, list[dict]]:
    history = defaultdict(list)
    for path in sorted(report_dir("EMdes010").glob("EMdes010.*-C.csv")):
        date = source_date(path)
        for row in body_rows(path):
            if len(row) < 13:
                continue
            stock_id = row[1].strip()
            vwap = number(row[5])
            if not stock_id or vwap <= 0:
                continue
            history[stock_id].append({
                "date": date, "stock_id": stock_id, "name": row[2].strip(),
                "vwap": vwap, "high": number(row[9]), "low": number(row[10]),
                "last": number(row[11]), "volume_lots": number(row[12]) / 1000,
            })
    return history


def setup_metrics(rows: list[dict], index: int) -> dict | None:
    consolidation = int(CONFIG["consolidation_sessions"])
    volume_window = int(CONFIG["volume_baseline_sessions"])
    quiet = int(CONFIG["first_leg_quiet_sessions"])
    if index < max(consolidation, volume_window, quiet + 1):
        return None
    prior_range = rows[index - consolidation:index]
    prior_volumes = [x["volume_lots"] for x in rows[index - volume_window:index] if x["volume_lots"] > 0]
    range_low = min((x["low"] for x in prior_range if x["low"] > 0), default=0)
    range_high = max((x["high"] for x in prior_range), default=0)
    if not range_low or not prior_volumes:
        return None
    prior_jumps = []
    for j in range(index - quiet, index):
        base = rows[j - 1]["vwap"]
        prior_jumps.append((rows[j]["high"] / base - 1) * 100 if base else 999)
    recent = rows[max(0, index - 60):index]
    true_ranges = []
    for pos, bar in enumerate(recent):
        previous_close = recent[pos - 1]["last"] if pos else bar["vwap"]
        true_ranges.append(max(bar["high"] - bar["low"], abs(bar["high"] - previous_close), abs(bar["low"] - previous_close)))
    atr = statistics.mean(true_ranges[-14:]) if true_ranges else max(range_high - range_low, range_high * 0.03)
    tolerance = max(atr * 0.35, range_high * 0.005)
    raw_levels = [(range_high, "箱型上緣", 4), (range_low, "箱型下緣", 4)]
    for pos in range(2, len(recent) - 2):
        window = recent[pos - 2:pos + 3]
        bar = recent[pos]
        if bar["high"] >= max(x["high"] for x in window):
            raw_levels.append((bar["high"], "波段高點", 2))
        if bar["low"] <= min(x["low"] for x in window):
            raw_levels.append((bar["low"], "波段低點", 2))
    if recent:
        for window, label in ((5, "5日VWAP"), (20, "20日VWAP")):
            subset = recent[-window:]
            volume = sum(x["volume_lots"] for x in subset)
            if volume:
                raw_levels.append((sum(x["vwap"] * x["volume_lots"] for x in subset) / volume, label, 2))
    clusters: list[dict] = []
    for price, label, base_score in sorted(raw_levels):
        if price <= 0:
            continue
        target = next((x for x in clusters if abs(x["price"] - price) <= tolerance), None)
        if target:
            weight = target["weight"] + base_score
            target["price"] = (target["price"] * target["weight"] + price * base_score) / weight
            target["weight"] = weight
            if label not in target["sources"]:
                target["sources"].append(label)
        else:
            clusters.append({"price": price, "weight": base_score, "sources": [label]})
    for level in clusters:
        touches = sum(1 for bar in recent if bar["low"] - tolerance <= level["price"] <= bar["high"] + tolerance)
        level["touches"] = touches
        level["score"] = min(100, 20 + level["weight"] * 8 + min(touches, 8) * 5)
        level["price"] = round(level["price"], 2)
    return {
        "range_high": range_high,
        "range_low": range_low,
        "range_pct": (range_high / range_low - 1) * 100,
        "median_volume_lots": statistics.median(prior_volumes),
        "prior_max_jump_pct": max(prior_jumps),
        "atr14": round(atr, 3),
        "structure_levels": sorted(clusters, key=lambda x: (x["score"], x["price"]), reverse=True)[:12],
    }


def is_first_breakout(price: float, volume_lots: float, setup: dict | None) -> bool:
    return bool(setup
        and setup["range_pct"] <= float(CONFIG["consolidation_max_range_pct"])
        and price > setup["range_high"]
        and volume_lots >= setup["median_volume_lots"] * float(CONFIG["volume_multiple"])
        and setup["prior_max_jump_pct"] < float(CONFIG["first_leg_prior_jump_pct"]))


def build_core_cache() -> dict:
    files = sorted(report_dir("EMdss004").glob("EMdss004.*-C.csv"))[-int(CONFIG["core_lookback_sessions"]):]
    broker_names = load_broker_names()
    grouped = defaultdict(lambda: defaultdict(lambda: {"buy_lots": 0.0, "sell_lots": 0.0, "buy_amount": 0.0, "sell_amount": 0.0, "days": set(), "inventory": 0.0, "cost": 0.0, "peak": 0.0}))
    names = {}
    for path in files:
        date = source_date(path)
        for row in body_rows(path):
            if len(row) < 7:
                continue
            stock_id, name, broker_id = row[1].strip(), row[2].strip(), row[3].strip()
            price, buy, sell = number(row[4]), number(row[5]) / 1000, number(row[6]) / 1000
            if not stock_id or not broker_id or price <= 0:
                continue
            names[stock_id] = name
            item = grouped[stock_id][broker_id]
            item["buy_lots"] += buy; item["sell_lots"] += sell
            item["buy_amount"] += price * buy * 1000; item["sell_amount"] += price * sell * 1000
            # 逐價位交易資料按日期推進：淨買增加庫存，淨賣按原成本減庫存。
            net = buy - sell
            if net > 0:
                added_cost = price
                new_inventory = item["inventory"] + net
                item["cost"] = ((item["inventory"] * item["cost"]) + (net * added_cost)) / new_inventory
                item["inventory"] = new_inventory
                item["peak"] = max(item["peak"], new_inventory)
            elif net < 0:
                item["inventory"] = max(0.0, item["inventory"] + net)
                if item["inventory"] == 0:
                    item["cost"] = 0.0
            if buy or sell:
                item["days"].add(date)
    result = {}
    for stock_id, brokers in grouped.items():
        rows = []
        positive_total = sum(max(v["buy_amount"] - v["sell_amount"], 0) for v in brokers.values())
        for broker_id, value in brokers.items():
            net_lots = value["buy_lots"] - value["sell_lots"]
            net_amount = value["buy_amount"] - value["sell_amount"]
            rows.append({
                "broker_id": broker_id, "broker_name": broker_names.get(broker_id, ""),
                "buy_lots": round(value["buy_lots"], 2),
                "sell_lots": round(value["sell_lots"], 2), "net_lots": round(net_lots, 2),
                "net_amount": round(net_amount), "inventory_lots": round(value["inventory"], 2),
                "estimated_cost": round(value["cost"], 2) if value["inventory"] else None,
                "inventory_peak_lots": round(value["peak"], 2),
                "inventory_retention": round(value["inventory"] / value["peak"], 3) if value["peak"] else 0,
                "capital_share_pct": round(max(net_amount, 0) / positive_total * 100, 2) if positive_total else 0,
                "active_sessions": len(value["days"]),
            })
        ranked = sorted(rows, key=lambda item: item["net_amount"], reverse=True)
        core = ranked[:int(CONFIG["core_top_n"])]
        qualified = [x for x in ranked if x["net_lots"] > 0 and x["inventory_retention"] >= float(CONFIG["inventory_support_floor"]) and x["active_sessions"] >= 2]
        largest_share = max((x["capital_share_pct"] for x in qualified), default=0)
        recent_builder_count = sum(2 <= x["active_sessions"] <= 5 for x in qualified)
        result[stock_id] = {"name": names.get(stock_id, stock_id), "start_date": source_date(files[0]), "end_date": source_date(files[-1]), "brokers": core, "tail_features": {"qualified_brokers": len(qualified), "largest_capital_share_pct": round(largest_share, 2), "recent_builder_count": recent_builder_count}}
    payload = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "lookback_sessions": len(files), "stocks": result}
    save_json(LOCAL / "core_cache.json", payload)
    return payload


def run_research() -> dict:
    history = load_market_history()
    events = []
    raw_stock_days = 0
    suppressed_repeats = 0
    for stock_id, rows in history.items():
        rows.sort(key=lambda item: item["date"])
        next_eligible_index = max(int(CONFIG["volume_baseline_sessions"]), 1)
        for index in range(1, len(rows)):
            prior, today = rows[index - 1], rows[index]
            trigger_pct = (today["high"] / prior["vwap"] - 1) * 100 if prior["vwap"] else 0
            if trigger_pct <= float(CONFIG["trigger_pct"]):
                continue
            raw_stock_days += 1
            setup = setup_metrics(rows, index)
            if not is_first_breakout(today["high"], today["volume_lots"], setup):
                continue
            # 同一段行情只建立一個事件：首次突破後進入完整追蹤期，期間再突破只屬於同一事件。
            if index < next_eligible_index:
                suppressed_repeats += 1
                continue
            next_eligible_index = index + int(CONFIG["tracking_sessions"]) + 1
            event = {"stock_id": stock_id, "name": today["name"], "event_date": today["date"], "trigger_pct_proxy": round(trigger_pct, 2), "event_vwap": today["vwap"], "event_volume_lots": today["volume_lots"], "volume_multiple": round(today["volume_lots"] / setup["median_volume_lots"], 2), "prior_range_pct": round(setup["range_pct"], 2), "mature_5d": index + 5 < len(rows)}
            future = rows[index + 1:index + 21]
            for horizon in (1, 3, 5, 10, 20):
                key = f"return_{horizon}d_pct"
                event[key] = round((future[horizon - 1]["vwap"] / today["vwap"] - 1) * 100, 2) if len(future) >= horizon else None
            first5 = future[:5]
            event["max_5d_pct"] = round((max(x["high"] for x in first5) / today["vwap"] - 1) * 100, 2) if len(first5) == 5 else None
            event["min_5d_pct"] = round((min(x["low"] for x in first5) / today["vwap"] - 1) * 100, 2) if len(first5) == 5 else None
            events.append(event)
    mature = [event for event in events if event["mature_5d"]]
    wins = [event for event in mature if (event["return_5d_pct"] or 0) > 0]
    extension = [event for event in mature if (event["max_5d_pct"] or 0) >= 10]
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "market_start": min((rows[0]["date"] for rows in history.values() if rows), default=None),
        "market_end": max((rows[-1]["date"] for rows in history.values() if rows), default=None),
        "event_count": len(events), "raw_stock_day_count": raw_stock_days,
        "suppressed_repeat_count": suppressed_repeats,
        "setup_definition": f"前{CONFIG['consolidation_sessions']}日振幅≤{CONFIG['consolidation_max_range_pct']}%、突破區間高點、成交量≥前{CONFIG['volume_baseline_sessions']}日中位量{CONFIG['volume_multiple']}倍、前{CONFIG['first_leg_quiet_sessions']}日無≥{CONFIG['first_leg_prior_jump_pct']}%先行漲幅",
        "event_unit": f"每檔每日最多一次；首次突破後 {CONFIG['tracking_sessions']} 個交易日內不重複開新事件",
        "mature_5d_count": len(mature),
        "five_day_positive_rate": round(len(wins) / len(mature) * 100, 1) if mature else None,
        "five_day_extension_rate": round(len(extension) / len(mature) * 100, 1) if mature else None,
        "definition": "歷史事件要求盤整後帶量突破且只取波段第一根；同一檔進入20日追蹤期後不重複計次。日資料未知首次突破時間，不當成可成交績效。",
    }
    save_json(LOCAL / "research_events.json", events)
    save_json(PUBLIC / "research.json", {"summary": summary, "recent_events": sorted(events, key=lambda x: x["event_date"], reverse=True)[:100]})
    latest = {stock_id: rows[-1] for stock_id, rows in history.items() if rows}
    save_json(LOCAL / "daily_latest.json", latest)
    setups = {}
    for stock_id, rows in history.items():
        metric = setup_metrics(rows, len(rows))
        if metric:
            setups[stock_id] = metric
    save_json(LOCAL / "setup_cache.json", setups)
    listing_cache = {}
    listing_events = []
    limit = int(CONFIG["new_listing_sessions"])
    global_market_end = max((rows[-1]["date"] for rows in history.values() if rows), default="")
    for stock_id, rows in history.items():
        rows.sort(key=lambda x: x["date"])
        recent_volumes = [x["volume_lots"] for x in rows[-limit:] if x["volume_lots"] > 0]
        listing_cache[stock_id] = {"listing_date": rows[0]["date"], "completed_sessions": len(rows), "prior_high": max((x["high"] for x in rows[-limit:]), default=None), "median_volume_lots": statistics.median(recent_volumes) if recent_volumes else None}
        for index in range(2, min(len(rows), limit)):
            prior, today = rows[index - 1], rows[index]
            pct = (today["high"] / prior["vwap"] - 1) * 100 if prior["vwap"] else 0
            prior_volumes = [x["volume_lots"] for x in rows[:index] if x["volume_lots"] > 0]
            baseline = statistics.median(prior_volumes) if prior_volumes else 0
            if pct <= float(CONFIG["new_listing_trigger_pct"]) or not baseline or today["volume_lots"] < baseline * float(CONFIG["new_listing_volume_multiple"]):
                continue
            event = {"stock_id": stock_id, "name": today["name"], "listing_date": rows[0]["date"], "event_date": today["date"], "listing_session": index + 1, "trigger_pct_proxy": round(pct, 2), "volume_multiple": round(today["volume_lots"] / baseline, 2)}
            for horizon in (5, 10, 20):
                event[f"return_{horizon}d_pct"] = round((rows[index + horizon]["vwap"] / today["vwap"] - 1) * 100, 2) if index + horizon < len(rows) else None
            completed = rows[-1]["date"] < global_market_end
            event["completed_emerging"] = completed
            event["last_emerging_date"] = rows[-1]["date"] if completed else None
            event["return_to_last_emerging_pct"] = round((rows[-1]["vwap"] / today["vwap"] - 1) * 100, 2) if completed else None
            event["max_to_last_emerging_pct"] = round((max(x["high"] for x in rows[index:]) / today["vwap"] - 1) * 100, 2) if completed else None
            listing_events.append(event)
            break
    save_json(LOCAL / "new_listing_cache.json", listing_cache)
    mature_listing = [x for x in listing_events if x["return_20d_pct"] is not None]
    completed_listing = [x for x in listing_events if x["completed_emerging"]]
    listing_summary = {"event_count": len(listing_events), "mature_20d_count": len(mature_listing), "twenty_day_mean_pct": round(statistics.mean(x["return_20d_pct"] for x in mature_listing), 2) if mature_listing else None, "twenty_day_median_pct": round(statistics.median(x["return_20d_pct"] for x in mature_listing), 2) if mature_listing else None, "twenty_day_win_rate_pct": round(sum(x["return_20d_pct"] > 0 for x in mature_listing) / len(mature_listing) * 100, 1) if mature_listing else None, "twenty_day_80_plus_count": sum(x["return_20d_pct"] >= 80 for x in mature_listing), "completed_emerging_count": len(completed_listing), "to_last_emerging_200_plus_count": sum(x["return_to_last_emerging_pct"] >= 200 for x in completed_listing), "max_before_transfer_200_plus_count": sum(x["max_to_last_emerging_pct"] >= 200 for x in completed_listing)}
    save_json(PUBLIC / "new_listings.json", {"summary": listing_summary, "recent_events": sorted(listing_events, key=lambda x: x["event_date"], reverse=True)[:100]})
    save_json(LOCAL / "trading_dates.json", sorted({row["date"] for rows in history.values() for row in rows}))
    print(f"研究完成：{len(events)} 個15%事件，成熟五日 {len(mature)} 個")
    return summary


def connect_db():
    LOCAL.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS observations(ts TEXT, stock_id TEXT, name TEXT, price REAL, volume_lots REAL, change_pct REAL, PRIMARY KEY(ts, stock_id))")
    db.execute("CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, stock_id TEXT, name TEXT, trigger_ts TEXT, trigger_price REAL, trigger_pct REAL, status TEXT, frozen_core_json TEXT, last_seen_ts TEXT, max_pct REAL)")
    db.commit()
    return db


def fetch_intraday() -> list[dict]:
    from bs4 import BeautifulSoup
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new"); options.add_argument("--disable-gpu"); options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage"); options.add_argument("--no-first-run")
    options.set_capability("pageLoadStrategy", "eager")
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    major = chrome.stat().st_size and chrome.resolve() and chrome_version_major(chrome)
    cached = sorted(Path.home().glob(f".wdm/drivers/chromedriver/win64/{major}.*/*/chromedriver.exe"), reverse=True)
    if not cached:
        cached = sorted(Path.home().glob(f".wdm/drivers/chromedriver/win64/{major}.*/*/chromedriver-win32/chromedriver.exe"), reverse=True)
    if not cached:
        raise RuntimeError(f"找不到與 Chrome {major} 相容的本機 ChromeDriver；請先執行既有盤中爬蟲更新驅動。")
    driver = webdriver.Chrome(service=Service(str(cached[0])), options=options)
    try:
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(20)
        try:
            driver.get(CONFIG["intraday_url"])
        except TimeoutException:
            driver.execute_script("window.stop();")
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'td[data-th="代號"]')))
        soup = BeautifulSoup(driver.page_source, "html.parser")
    finally:
        driver.quit()
    rows = []
    for row in soup.select("tbody tr"):
        code = row.select_one('td[data-th="代號"] a')
        name = row.select_one('td[data-th="名稱"]')
        price = row.select_one('td[data-th="最近成交價"]')
        volume = row.select_one('td[data-th="成交量"]')
        if not all((code, name, price, volume)):
            continue
        value = number(price.text)
        if value > 0:
            rows.append({"stock_id": code.text.strip(), "name": name.text.strip(), "price": value, "volume_lots": number(volume.text) / 1000})
    if not rows:
        raise RuntimeError("興櫃即時行情頁沒有可解析的股票資料")
    return rows


def chrome_version_major(chrome: Path) -> str:
    command = ["powershell", "-NoProfile", "-Command", f"(Get-Item -LiteralPath '{chrome}').VersionInfo.FileVersion"]
    version = subprocess.run(command, text=True, capture_output=True, check=True).stdout.strip()
    return version.split(".")[0]


def classify(core: list[dict]) -> tuple[str, dict]:
    top5 = core[:5]
    floor = float(CONFIG["inventory_support_floor"])
    positive = sum(item["net_lots"] > 0 for item in top5)
    retained = sum(item.get("inventory_lots", 0) > 0 and item.get("inventory_retention", 0) >= floor for item in top5)
    total_net = sum(item["net_lots"] for item in top5)
    share = sum(item["capital_share_pct"] for item in top5)
    evidence = {"positive_top5": positive, "retained_top5": retained, "retention_floor_pct": round(floor * 100), "top5_net_lots": round(total_net, 2), "top5_capital_share_pct": round(share, 2)}
    if len(top5) < 5:
        return "INSUFFICIENT", evidence
    status = "CHIP_SUPPORT" if retained >= int(CONFIG["minimum_support_brokers"]) and total_net > 0 else "MIXED"
    return status, evidence


def extreme_routes(stock_meta: dict, setup: dict | None, volume_lots: float) -> list[str]:
    feature = stock_meta.get("tail_features", {})
    qualified = feature.get("qualified_brokers", 0)
    share = feature.get("largest_capital_share_pct", 0)
    volume_multiple = volume_lots / setup["median_volume_lots"] if setup and setup.get("median_volume_lots") else 0
    routes = []
    if qualified >= 40 and share <= 30:
        routes.append("BROAD_IGNITION")
    if qualified <= 5 and share >= 60:
        routes.append("CONCENTRATED_CONTROL")
    if feature.get("recent_builder_count", 0) >= 1 and setup and setup.get("range_pct", 999) <= 10 and volume_multiple >= 5:
        routes.append("EARLY_SEED")
    return routes


def build_action_plan(item: dict, setup: dict | None) -> dict:
    """Turn each event's own price/chip context into an auditable action state."""
    trigger = number(item.get("trigger_price"))
    price = number(item.get("price"))
    setup = setup or {}
    breakout = number(setup.get("range_high")) or trigger
    box_low = number(setup.get("range_low"))
    atr = number(setup.get("atr14")) or max(trigger * 0.03, 0.01)
    latest = item.get("latest_core") or []
    frozen_rows = item.get("frozen_core") or []
    inventory = sum(max(number(x.get("inventory_lots")), 0) for x in latest[:5])
    frozen_inventory = sum(max(number(x.get("inventory_lots")), 0) for x in frozen_rows[:5])
    inventory_change = inventory - frozen_inventory
    inventory_change_pct = inventory_change / frozen_inventory * 100 if frozen_inventory else 0
    weighted = [(number(x.get("estimated_cost")), max(number(x.get("inventory_lots")), 0)) for x in latest[:5]]
    core_cost = sum(cost * lots for cost, lots in weighted if cost > 0) / sum(lots for cost, lots in weighted if cost > 0) if sum(lots for cost, lots in weighted if cost > 0) else None

    anchors = [{"price": number(x.get("price")), "score": number(x.get("score")), "sources": list(x.get("sources") or [])} for x in setup.get("structure_levels", [])]
    anchors.append({"price": breakout, "score": 90, "sources": ["突破線"]})
    if core_cost:
        anchors.append({"price": core_cost, "score": 95, "sources": ["核心主力成本"]})
    tolerance = max(atr * 0.35, trigger * 0.005)
    clusters: list[dict] = []
    for anchor in sorted((x for x in anchors if x["price"] > 0), key=lambda x: x["price"]):
        target = next((x for x in clusters if abs(x["price"] - anchor["price"]) <= tolerance), None)
        if target:
            total = target["score"] + anchor["score"]
            target["price"] = (target["price"] * target["score"] + anchor["price"] * anchor["score"]) / total
            target["score"] = min(100, total)
            target["sources"] = list(dict.fromkeys(target["sources"] + anchor["sources"]))
        else:
            clusters.append(anchor.copy())
    supports = [x for x in clusters if x["price"] <= trigger + atr * 0.5]
    support = max(supports, key=lambda x: x["score"] - abs(trigger - x["price"]) / atr * 6, default={"price": breakout, "score": 50, "sources": ["突破線"]})
    resistances = [x for x in clusters if x["price"] > max(trigger, support["price"] + tolerance)]
    resistance = min(resistances, key=lambda x: x["price"], default={"price": max(trigger, breakout), "score": 50, "sources": ["事件高點"]})
    lower_supports = [x for x in clusters if x["price"] < support["price"] - tolerance]
    invalidation = max(lower_supports, key=lambda x: x["price"], default={"price": box_low or support["price"] - atr, "score": 40, "sources": ["箱型下緣" if box_low else "ATR失效帶"]})

    trial_low, trial_high = support["price"] - atr * 0.20, support["price"] + atr * 0.20
    add_above = max(breakout, resistance["price"]) + atr * 0.10
    chase_limit = add_above + atr * 0.75
    reduce_below = support["price"] - atr * 0.50
    exit_below = min(invalidation["price"] - atr * 0.50, reduce_below - atr)
    eligible = bool(item.get("mandatory_strategy") or item.get("listing_signal"))
    chip_support = item.get("chip_status") == "CHIP_SUPPORT"
    first_mandatory_session = bool(item.get("mandatory_strategy") and int(item.get("tracking_age") or 0) == 0)

    if price and (price <= exit_below or (inventory_change_pct <= -20 and price < reduce_below)):
        state, label, reason = "EXIT", "出清訊號", "主要結構失效，或核心庫存大幅下降且價格同步轉弱"
    elif price and (price < reduce_below or inventory_change_pct <= -10):
        state, label, reason = "REDUCE", "減碼訊號", "跌破最近支撐帶，或核心庫存較首次捕捉減少至少一成"
    elif first_mandatory_session:
        state, label, reason = "ENTRY", "必買首筆", "必買策略首次捕捉日先建立首筆；結構價位留給後續加碼與風險控制"
    elif eligible and chip_support and inventory_change > 0 and add_above <= price <= chase_limit:
        state, label, reason = "ADD", "加碼訊號", "突破下一個結構壓力，且核心庫存高於首次捕捉"
    elif eligible and chip_support and trial_low <= price <= trial_high:
        state, label, reason = "TRIAL", "試單訊號", "價格回到結構支撐帶，且核心籌碼仍支持"
    elif eligible and price > chase_limit:
        state, label, reason = "WAIT", "等待拉回", "價格離開結構加碼區超過四分之三個ATR，暫不追價"
    else:
        state, label, reason = "WATCH", "尚未進場", "量價或核心條件尚未同時成立"
    return {
        "state": state, "label": label, "reason": reason,
        "trial_zone_low": round(trial_low, 2), "trial_zone_high": round(trial_high, 2),
        "add_above": round(add_above, 2), "chase_limit": round(chase_limit, 2),
        "reduce_below": round(reduce_below, 2), "exit_below": round(exit_below, 2),
        "core_cost": round(core_cost, 2) if core_cost else None,
        "core_inventory_change_lots": round(inventory_change, 1),
        "core_inventory_change_pct": round(inventory_change_pct, 1),
        "atr14": round(atr, 2), "method": "STRUCTURE_ATR_CORE_V1",
        "initial_entry_price": round(trigger, 2),
        "backtest_slippage_price": round(trigger * 1.10, 2),
        "trial_basis": support["sources"], "trial_strength": int(min(100, support["score"])),
        "add_basis": resistance["sources"], "add_strength": int(min(100, resistance["score"])),
        "reduce_basis": support["sources"],
        "exit_basis": invalidation["sources"], "exit_strength": int(min(100, invalidation["score"])),
    }


def scan_once() -> dict:
    latest = load_json(LOCAL / "daily_latest.json", {})
    setups = load_json(LOCAL / "setup_cache.json", {})
    listings = load_json(LOCAL / "new_listing_cache.json", {})
    core_payload = load_json(LOCAL / "core_cache.json", {"stocks": {}})
    core_cache = core_payload["stocks"]
    intraday = fetch_intraday()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    db = connect_db()
    found = []
    live_by_id = {item["stock_id"]: item for item in intraday}
    dates = load_json(LOCAL / "trading_dates.json", [])
    today_key = now[:10].replace("-", "")
    if today_key not in dates:
        dates.append(today_key)
    date_pos = {date: i for i, date in enumerate(sorted(set(dates)))}
    for item in intraday:
        prior = latest.get(item["stock_id"], {})
        prior_vwap = prior.get("vwap")
        listing = listings.get(item["stock_id"])
        is_day_one = not prior_vwap
        listing_session = (listing.get("completed_sessions", 0) + 1) if listing else (1 if is_day_one else None)
        is_new_listing = bool(listing_session and listing_session <= int(CONFIG["new_listing_sessions"]))
        if not prior_vwap:
            if is_new_listing:
                stock_meta = core_cache.get(item["stock_id"], {})
                core = stock_meta.get("brokers", [])
                status, evidence = classify(core)
                found.append({**item, "change_pct": None, "event_id": f"NEW-{today_key}-{item['stock_id']}", "trigger_ts": now, "trigger_price": item["price"], "max_pct": None, "tracking_age": 0, "above_threshold": False, "strategy_type": "NEW_LISTING", "new_listing": True, "listing_session": listing_session, "signal_level": "WATCH", "mandatory_strategy": False, "tail_routes": [], "chip_status": status, "evidence": evidence, "frozen_core": core, "latest_core": core})
            continue
        pct = (item["price"] / prior_vwap - 1) * 100
        db.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?)", (now, item["stock_id"], item["name"], item["price"], item["volume_lots"], pct))
        setup = setups.get(item["stock_id"])
        regular_signal = pct > float(CONFIG["trigger_pct"]) and is_first_breakout(item["price"], item["volume_lots"], setup)
        listing_baseline = listing.get("median_volume_lots") if listing else None
        listing_signal = bool(is_new_listing and pct > float(CONFIG["new_listing_trigger_pct"]) and (not listing_baseline or item["volume_lots"] >= listing_baseline * float(CONFIG["new_listing_volume_multiple"])))
        if not regular_signal and not is_new_listing:
            continue
        stock_meta = core_cache.get(item["stock_id"], {})
        routes = extreme_routes(stock_meta, setup, item["volume_lots"]) if regular_signal else []
        event_id = f"{today_key}-{item['stock_id']}" if regular_signal else f"NEW-{today_key}-{item['stock_id']}"
        recent = db.execute("SELECT event_id,trigger_ts FROM events WHERE stock_id=? ORDER BY trigger_ts DESC LIMIT 1", (item["stock_id"],)).fetchone()
        if recent:
            recent_date = recent[1][:10].replace("-", "")
            age = date_pos.get(today_key, len(date_pos)) - date_pos.get(recent_date, -999)
            if 0 <= age <= int(CONFIG["tracking_sessions"]):
                event_id = recent[0]
        core = stock_meta.get("brokers", [])
        status, evidence = classify(core)
        existing = db.execute("SELECT trigger_ts,trigger_price,trigger_pct,frozen_core_json,max_pct FROM events WHERE event_id=?", (event_id,)).fetchone()
        if existing:
            trigger_ts, trigger_price, trigger_pct, frozen_json, max_pct = existing
            frozen = json.loads(frozen_json); max_pct = max(max_pct or pct, pct)
        else:
            trigger_ts, trigger_price, trigger_pct, frozen, max_pct = now, item["price"], pct, core, pct
        db.execute("INSERT OR REPLACE INTO events(event_id,stock_id,name,trigger_ts,trigger_price,trigger_pct,status,frozen_core_json,last_seen_ts,max_pct) VALUES(?,?,?,?,?,?,?,?,?,?)", (event_id, item["stock_id"], item["name"], trigger_ts, trigger_price, trigger_pct, status, json.dumps(frozen, ensure_ascii=False), now, max_pct))
        found.append({**item, "change_pct": round(pct, 2), "event_id": event_id, "trigger_ts": trigger_ts, "trigger_price": trigger_price, "max_pct": round(max_pct, 2), "breakout_setup": setup, "strategy_type": "EXTREME_RADAR" if regular_signal else "NEW_LISTING", "mandatory_strategy": bool(regular_signal), "new_listing": is_new_listing, "listing_session": listing_session, "listing_signal": listing_signal, "signal_level": "MANDATORY" if regular_signal else ("HOT" if listing_signal else "WATCH"), "tail_routes": routes, "tail_features": stock_meta.get("tail_features", {}), "chip_status": status, "evidence": evidence, "frozen_core": frozen, "latest_core": core})
    # 首次突破後保留二十個交易日；即使跌回門檻也不會從戰情室消失。
    for row in db.execute("SELECT event_id,stock_id,name,trigger_ts,trigger_price,trigger_pct,status,frozen_core_json,last_seen_ts,max_pct FROM events").fetchall():
        event_id, stock_id, name, trigger_ts, trigger_price, trigger_pct, saved_status, frozen_json, last_seen, max_pct = row
        if any(item["event_id"] == event_id for item in found):
            continue
        trigger_date = trigger_ts[:10].replace("-", "")
        age = date_pos.get(today_key, len(date_pos)) - date_pos.get(trigger_date, date_pos.get(today_key, 0))
        if age > int(CONFIG["tracking_sessions"]):
            continue
        live = live_by_id.get(stock_id, {})
        price = live.get("price")
        prior_vwap = latest.get(stock_id, {}).get("vwap")
        pct = (price / prior_vwap - 1) * 100 if price and prior_vwap else None
        frozen = json.loads(frozen_json)
        stock_meta = core_cache.get(stock_id, {})
        core = stock_meta.get("brokers", [])
        chip_status, evidence = classify(core)
        mandatory = not event_id.startswith("NEW-") and trigger_pct > float(CONFIG["trigger_pct"])
        listing = listings.get(stock_id, {})
        listing_session = listing.get("completed_sessions", 0) + 1 if listing else None
        found.append({"stock_id": stock_id, "name": name, "price": price, "volume_lots": live.get("volume_lots"), "change_pct": round(pct, 2) if pct is not None else None, "event_id": event_id, "trigger_ts": trigger_ts, "trigger_price": trigger_price, "trigger_pct": trigger_pct, "max_pct": round(max_pct or trigger_pct, 2), "tracking_age": age, "above_threshold": pct is not None and pct > float(CONFIG["trigger_pct"]), "strategy_type": "EXTREME_RADAR" if mandatory else "NEW_LISTING", "mandatory_strategy": mandatory, "new_listing": event_id.startswith("NEW-"), "listing_session": listing_session, "tail_routes": extreme_routes(stock_meta, setups.get(stock_id), live.get("volume_lots", 0)) if mandatory else [], "chip_status": chip_status, "evidence": evidence, "frozen_core": frozen, "latest_core": core})
    db.commit(); db.close()
    for item in found:
        item.setdefault("tracking_age", 0); item.setdefault("above_threshold", (item.get("change_pct") or -999) > float(CONFIG["trigger_pct"])); item.setdefault("mandatory_strategy", False); item.setdefault("new_listing", False); item.setdefault("tail_routes", [])
        item["action_plan"] = build_action_plan(item, item.get("breakout_setup") or setups.get(item["stock_id"]))
    payload = {"schema_version": 3, "market_time": now, "broker_data_date": core_payload.get("stocks", {}).get(next(iter(core_payload.get("stocks", {})), ""), {}).get("end_date"), "trigger_pct": CONFIG["trigger_pct"], "new_listing_trigger_pct": CONFIG["new_listing_trigger_pct"], "mandatory_strategy": "EXTREME_RADAR", "stocks": sorted(found, key=lambda x: (x["mandatory_strategy"], x.get("listing_signal", False), x["change_pct"] or -999), reverse=True), "data_status": "live"}
    save_json(PUBLIC / "dashboard.json", payload)
    try:
        alerts = notify_discord(payload["stocks"])
        if alerts:
            print(f"Discord 已推播 {alerts} 則操作訊號")
    except Exception as exc:
        print(f"Discord 推播失敗，盯盤繼續執行：{exc}")
    print(f"{now}：符合 >15% 共 {len(found)} 檔")
    return payload


def publish() -> None:
    if not (ROOT / ".git").exists():
        print("尚未設定 Git repository，已保留本地網站資料。")
        return
    site_data = f'{CONFIG["site_directory"]}/data'
    status = subprocess.run(["git", "status", "--porcelain", "--", site_data], cwd=ROOT, text=True, capture_output=True)
    if not status.stdout.strip():
        return
    subprocess.run(["git", "add", "--", site_data], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-m", f"Update radar {datetime.now():%Y-%m-%d %H:%M}"], cwd=ROOT, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, check=True)


def scan_loop(do_publish: bool) -> None:
    next_publish = 0.0
    while True:
        now = datetime.now()
        if now.weekday() >= 5:
            print("非交易日，盤中程式結束。")
            break
        if now.time() < clock_time(8, 30):
            print("尚未進入盤前時段，盤中程式結束。")
            break
        if now.time() < clock_time(9, 0):
            time.sleep(min(60, max(1, int((datetime.combine(now.date(), clock_time(9, 0)) - now).total_seconds()))))
            continue
        if now.time() >= clock_time(15, 5):
            print("盤後時段，盤中程式結束。")
            break
        try:
            payload = scan_once()
            update_monitor_health("UP", f"本輪掃描完成：門檻內 {len(payload.get('stocks', []))} 檔；下一輪持續監看。")
            if do_publish and time.time() >= next_publish:
                publish(); next_publish = time.time() + int(CONFIG["publish_interval_minutes"]) * 60
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            detail = f"{type(exc).__name__}: {str(exc)[:240]}；60 秒後重試。"
            print(detail)
            update_monitor_health("DOWN", detail)
            time.sleep(60)
            continue
        time.sleep(int(CONFIG["scan_interval_seconds"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="興櫃盤中15%起漲與主力續航雷達")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("research")
    sub.add_parser("prepare")
    sub.add_parser("publish")
    sub.add_parser("discord-test")
    scan = sub.add_parser("scan"); scan.add_argument("--once", action="store_true"); scan.add_argument("--publish", action="store_true")
    eod = sub.add_parser("eod"); eod.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.command == "research": run_research(); build_core_cache()
    elif args.command == "prepare": build_core_cache()
    elif args.command == "scan":
        if args.once: scan_once(); publish() if args.publish else None
        else: scan_loop(args.publish)
    elif args.command == "eod":
        run_research(); build_core_cache(); publish() if args.publish else None
    elif args.command == "publish": publish()
    elif args.command == "discord-test":
        if not DISCORD_WEBHOOK.exists():
            raise SystemExit(f"尚未建立 {DISCORD_WEBHOOK}")
        post_discord("✅ **6666 推播通知已連線**\n極端飆股戰情室的本地 BAT 將在新訊號或操作狀態改變時推播到此頻道。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
