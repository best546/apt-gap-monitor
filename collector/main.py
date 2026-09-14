from __future__ import annotations

import json, os, re, statistics, sys, time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "complexes.json"
ASKING = ROOT / "config" / "asking-prices.json"
OUTPUT = ROOT / "docs" / "data" / "latest.json"
HISTORY = ROOT / "docs" / "data" / "history.json"
API_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"

def load_config(path):
    repo = (os.environ.get("GITHUB_REPOSITORY") or "best546/apt-gap-monitor").strip()
    branch = (os.environ.get("GITHUB_BRANCH") or "main").strip()
    if os.environ.get("GITHUB_DATA_TOKEN"):
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/config/{path.name}"
        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            print(f"Loaded latest config from GitHub: {path.name}")
            return response.json()
        except Exception as exc:
            print(f"Remote config unavailable; using local {path.name}: {type(exc).__name__}", file=sys.stderr)
    return json.loads(path.read_text(encoding="utf-8"))

def norm(s):
    return re.sub(r"[\s·\-()._]", "", str(s or "").lower()).replace("아파트", "")

def pick(d, *keys, default=""):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default

def month_keys(n):
    y, m = date.today().year, date.today().month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}{m:02d}")
        m -= 1
        if m == 0: y, m = y - 1, 12
    return out

def parse_items(xml_text):
    root = ET.fromstring(xml_text)
    result = []
    for node in root.findall(".//item"):
        result.append({child.tag: (child.text or "").strip() for child in node})
    return result

def fetch_month(key, lawd, ym):
    params = {"serviceKey": unquote(key), "LAWD_CD": lawd, "DEAL_YMD": ym,
              "pageNo": 1, "numOfRows": 9999}
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(total=4, connect=4, read=4, backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))))
    r = session.get(API_URL, params=params, timeout=(30, 90))
    if not r.ok:
        raise RuntimeError(f"MOLIT API HTTP {r.status_code}: {r.text[:300]}")
    rows = parse_items(r.text)
    if not rows and "NORMAL SERVICE" not in r.text and "00" not in r.text:
        raise RuntimeError(r.text[:500])
    return rows

def tx_from(row):
    price = int(str(pick(row, "dealAmount", "거래금액")).replace(",", "").strip())
    area = float(pick(row, "excluUseAr", "전용면적"))
    year = int(pick(row, "dealYear", "년")); month = int(pick(row, "dealMonth", "월")); day = int(pick(row, "dealDay", "일"))
    return {
        "apt": pick(row, "aptNm", "아파트"), "price_manwon": price, "area": area,
        "date": f"{year:04d}-{month:02d}-{day:02d}", "floor": int(pick(row, "floor", "층", default=0) or 0),
        "direct": pick(row, "dealingGbn", "거래유형") == "직거래",
        "cancelled": bool(pick(row, "cdealDay", "해제사유발생일"))
    }

def summarize(c, rows, tol, default_min_floor=4, representative_months=6):
    aliases = [norm(x) for x in c.get("aliases", []) + [c["name"]]]
    area_tolerance = c.get("area_tolerance", tol)
    min_floor = c.get("min_floor", default_min_floor)
    matches = []
    for row in rows:
        try: tx = tx_from(row)
        except (ValueError, TypeError): continue
        apt_name = norm(tx["apt"])
        name_ok = bool(apt_name) and (apt_name in aliases if c.get("match_mode") == "exact" else any(a in apt_name or apt_name in a for a in aliases if a))
        dong_ok = not c.get("dong") or norm(pick(row, "umdNm", "법정동")) == norm(c["dong"])
        area_ok = c.get("area_min", 0) <= tx["area"] <= c.get("area_max", 85) and abs(tx["area"] - c["area"]) <= area_tolerance
        if name_ok and dong_ok and area_ok and not tx["cancelled"]:
            matches.append(tx)
    matches.sort(key=lambda x: x["date"], reverse=True)
    normal = [x for x in matches if x["floor"] >= min_floor and not x["direct"]]
    recent_months = {m[:4] + "-" + m[4:] for m in month_keys(representative_months)}
    basis = [x for x in normal if x["date"][:7] in recent_months]
    prices = [x["price_manwon"] for x in basis]
    monthly = []
    for month in sorted({x["date"][:7] for x in normal}):
        month_prices = [x["price_manwon"] for x in normal if x["date"].startswith(month)]
        monthly.append({"month": month, "price_manwon": round(statistics.median(month_prices)), "count": len(month_prices)})
    return {**c, "area_tolerance": area_tolerance, "min_floor": min_floor, "count": sum(x["date"][:7] in recent_months for x in matches), "history_count": len(matches),
            "representative_manwon": round(statistics.median(prices)) if prices else None,
            "latest": matches[0] if matches else None, "monthly_prices": monthly, "transactions": matches[:20]}

def main():
    key = (os.environ.get("MOLIT_API_KEY") or "").strip().strip('"').strip("'")
    if not key: sys.exit("MOLIT_API_KEY is required")
    cfg = load_config(CONFIG); asking = load_config(ASKING)
    cache = {}
    for lawd in sorted({c["lawd_cd"] for c in cfg["complexes"]}):
        cache[lawd] = []
        for ym in month_keys(cfg.get("history_months", cfg["months"])):
            cache[lawd].extend(fetch_month(key, lawd, ym)); time.sleep(.12)
    items = [summarize(c, cache[c["lawd_cd"]], cfg["area_tolerance"], cfg.get("min_floor", 4), cfg["months"]) for c in cfg["complexes"]]
    home = next(x for x in items if x["id"] == cfg["reference_id"])
    base = home["representative_manwon"]
    home_monthly = {x["month"]: x["price_manwon"] for x in home["monthly_prices"]}
    for x in items:
        x["gap_manwon"] = x["representative_manwon"] - base if base is not None and x["representative_manwon"] is not None else None
        x["asking_manwon"] = asking.get("prices", {}).get(x["id"])
        x["monthly_gaps"] = [
            {"month": p["month"], "gap_manwon": p["price_manwon"] - home_monthly[p["month"]],
             "target_price_manwon": p["price_manwon"], "home_price_manwon": home_monthly[p["month"]]}
            for p in x["monthly_prices"] if p["month"] in home_monthly
        ] if x["id"] != cfg["reference_id"] else []
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = {"generated_at": stamp, "months": cfg["months"], "history_months": cfg.get("history_months", cfg["months"]), "reference_id": cfg["reference_id"], "items": items}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True); OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    history = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
    history.append({"generated_at": stamp, "values": {x["id"]: {"price":x["representative_manwon"], "gap":x["gap_manwon"]} for x in items}})
    HISTORY.write_text(json.dumps(history[-104:], ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        safe = re.sub(r"serviceKey=[^&\s]+", "serviceKey=***", str(exc), flags=re.IGNORECASE)
        print(f"COLLECTOR_ERROR: {type(exc).__name__}: {safe}", file=sys.stderr)
        sys.exit(1)

