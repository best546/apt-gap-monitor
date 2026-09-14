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

def norm(s):
    return "".join(str(s or "").lower().split()).replace("·", "").replace("-", "")

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

def summarize(c, rows, tol):
    aliases = [norm(x) for x in c.get("aliases", []) + [c["name"]]]
    matches = []
    for row in rows:
        try: tx = tx_from(row)
        except (ValueError, TypeError): continue
        if any(a in norm(tx["apt"]) or norm(tx["apt"]) in a for a in aliases) and abs(tx["area"] - c["area"]) <= tol and not tx["cancelled"]:
            matches.append(tx)
    matches.sort(key=lambda x: x["date"], reverse=True)
    normal = [x for x in matches if x["floor"] > 1 and not x["direct"]]
    basis = normal or matches
    prices = [x["price_manwon"] for x in basis]
    return {**c, "count": len(matches), "representative_manwon": round(statistics.median(prices)) if prices else None,
            "latest": matches[0] if matches else None, "transactions": matches[:20]}

def main():
    key = (os.environ.get("MOLIT_API_KEY") or "").strip().strip('"').strip("'")
    if not key: sys.exit("MOLIT_API_KEY is required")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")); asking = json.loads(ASKING.read_text(encoding="utf-8"))
    cache = {}
    for lawd in sorted({c["lawd_cd"] for c in cfg["complexes"]}):
        cache[lawd] = []
        for ym in month_keys(cfg["months"]):
            cache[lawd].extend(fetch_month(key, lawd, ym)); time.sleep(.12)
    items = [summarize(c, cache[c["lawd_cd"]], cfg["area_tolerance"]) for c in cfg["complexes"]]
    home = next(x for x in items if x["id"] == cfg["reference_id"])
    base = home["representative_manwon"]
    for x in items:
        x["gap_manwon"] = x["representative_manwon"] - base if base is not None and x["representative_manwon"] is not None else None
        x["asking_manwon"] = asking.get("prices", {}).get(x["id"])
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = {"generated_at": stamp, "months": cfg["months"], "reference_id": cfg["reference_id"], "items": items}
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
