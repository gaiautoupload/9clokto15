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
from collections import defaultdict
from datetime import datetime, time as clock_time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
SOURCE = Path(CONFIG["source_directory"])
LOCAL = ROOT / "data" / "local"
PUBLIC = ROOT / CONFIG["site_directory"] / "data"
DB = LOCAL / "radar.db"


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


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


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
    return {
        "range_high": range_high,
        "range_low": range_low,
        "range_pct": (range_high / range_low - 1) * 100,
        "median_volume_lots": statistics.median(prior_volumes),
        "prior_max_jump_pct": max(prior_jumps),
    }


def is_first_breakout(price: float, volume_lots: float, setup: dict | None) -> bool:
    return bool(setup
        and setup["range_pct"] <= float(CONFIG["consolidation_max_range_pct"])
        and price > setup["range_high"]
        and volume_lots >= setup["median_volume_lots"] * float(CONFIG["volume_multiple"])
        and setup["prior_max_jump_pct"] < float(CONFIG["first_leg_prior_jump_pct"]))


def build_core_cache() -> dict:
    files = sorted(report_dir("EMdss004").glob("EMdss004.*-C.csv"))[-int(CONFIG["core_lookback_sessions"]):]
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
                "broker_id": broker_id, "buy_lots": round(value["buy_lots"], 2),
                "sell_lots": round(value["sell_lots"], 2), "net_lots": round(net_lots, 2),
                "net_amount": round(net_amount), "inventory_lots": round(value["inventory"], 2),
                "estimated_cost": round(value["cost"], 2) if value["inventory"] else None,
                "inventory_peak_lots": round(value["peak"], 2),
                "inventory_retention": round(value["inventory"] / value["peak"], 3) if value["peak"] else 0,
                "capital_share_pct": round(max(net_amount, 0) / positive_total * 100, 2) if positive_total else 0,
                "active_sessions": len(value["days"]),
            })
        core = sorted(rows, key=lambda item: item["net_amount"], reverse=True)[:int(CONFIG["core_top_n"])]
        result[stock_id] = {"name": names.get(stock_id, stock_id), "start_date": source_date(files[0]), "end_date": source_date(files[-1]), "brokers": core}
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
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new"); options.add_argument("--disable-gpu"); options.add_argument("--no-sandbox")
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    major = chrome.stat().st_size and chrome.resolve() and chrome_version_major(chrome)
    cached = sorted(Path.home().glob(f".wdm/drivers/chromedriver/win64/{major}.*/*/chromedriver.exe"), reverse=True)
    if not cached:
        cached = sorted(Path.home().glob(f".wdm/drivers/chromedriver/win64/{major}.*/*/chromedriver-win32/chromedriver.exe"), reverse=True)
    if not cached:
        raise RuntimeError(f"找不到與 Chrome {major} 相容的本機 ChromeDriver；請先執行既有盤中爬蟲更新驅動。")
    driver = webdriver.Chrome(service=Service(str(cached[0])), options=options)
    try:
        driver.get(CONFIG["intraday_url"])
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'td[data-th="代號"]')))
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


def scan_once() -> dict:
    latest = load_json(LOCAL / "daily_latest.json", {})
    setups = load_json(LOCAL / "setup_cache.json", {})
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
        if not prior_vwap:
            continue
        pct = (item["price"] / prior_vwap - 1) * 100
        db.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?)", (now, item["stock_id"], item["name"], item["price"], item["volume_lots"], pct))
        if pct <= float(CONFIG["trigger_pct"]):
            continue
        setup = setups.get(item["stock_id"])
        if not is_first_breakout(item["price"], item["volume_lots"], setup):
            continue
        event_id = f"{today_key}-{item['stock_id']}"
        recent = db.execute("SELECT event_id,trigger_ts FROM events WHERE stock_id=? ORDER BY trigger_ts DESC LIMIT 1", (item["stock_id"],)).fetchone()
        if recent:
            recent_date = recent[1][:10].replace("-", "")
            age = date_pos.get(today_key, len(date_pos)) - date_pos.get(recent_date, -999)
            if 0 <= age <= int(CONFIG["tracking_sessions"]):
                event_id = recent[0]
        core = core_cache.get(item["stock_id"], {}).get("brokers", [])
        status, evidence = classify(core)
        existing = db.execute("SELECT trigger_ts,trigger_price,trigger_pct,frozen_core_json,max_pct FROM events WHERE event_id=?", (event_id,)).fetchone()
        if existing:
            trigger_ts, trigger_price, trigger_pct, frozen_json, max_pct = existing
            frozen = json.loads(frozen_json); max_pct = max(max_pct or pct, pct)
        else:
            trigger_ts, trigger_price, trigger_pct, frozen, max_pct = now, item["price"], pct, core, pct
        db.execute("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?,?)", (event_id, item["stock_id"], item["name"], trigger_ts, trigger_price, trigger_pct, status, json.dumps(frozen, ensure_ascii=False), now, max_pct))
        found.append({**item, "change_pct": round(pct, 2), "event_id": event_id, "trigger_ts": trigger_ts, "trigger_price": trigger_price, "max_pct": round(max_pct, 2), "breakout_setup": setup, "chip_status": status, "evidence": evidence, "frozen_core": frozen, "latest_core": core})
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
        core = core_cache.get(stock_id, {}).get("brokers", [])
        chip_status, evidence = classify(core)
        found.append({"stock_id": stock_id, "name": name, "price": price, "volume_lots": live.get("volume_lots"), "change_pct": round(pct, 2) if pct is not None else None, "event_id": event_id, "trigger_ts": trigger_ts, "trigger_price": trigger_price, "trigger_pct": trigger_pct, "max_pct": round(max_pct or trigger_pct, 2), "tracking_age": age, "above_threshold": pct is not None and pct > float(CONFIG["trigger_pct"]), "chip_status": chip_status, "evidence": evidence, "frozen_core": frozen, "latest_core": core})
    db.commit(); db.close()
    for item in found:
        item.setdefault("tracking_age", 0); item.setdefault("above_threshold", True)
    payload = {"schema_version": 2, "market_time": now, "broker_data_date": core_payload.get("stocks", {}).get(next(iter(core_payload.get("stocks", {})), ""), {}).get("end_date"), "trigger_pct": CONFIG["trigger_pct"], "stocks": sorted(found, key=lambda x: (x["above_threshold"], x["change_pct"] or -999), reverse=True), "data_status": "live"}
    save_json(PUBLIC / "dashboard.json", payload)
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
        scan_once()
        if do_publish and time.time() >= next_publish:
            publish(); next_publish = time.time() + int(CONFIG["publish_interval_minutes"]) * 60
        time.sleep(int(CONFIG["scan_interval_seconds"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="興櫃盤中15%起漲與主力續航雷達")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("research")
    sub.add_parser("prepare")
    sub.add_parser("publish")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
