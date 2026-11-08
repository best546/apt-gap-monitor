from __future__ import annotations

import hashlib, json, os, re, statistics, sys, time
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

DEFAULT_MIN_REPRESENTATIVE_COUNT = 5
DEFAULT_MAX_REPRESENTATIVE_MONTHS = 12
CONFIDENCE_RANK = {"NONE": -1, "VERY_LOW": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
GAP_ALERT_THRESHOLDS = {"HIGH": 2000, "MEDIUM": 3000, "LOW": 5000}


def load_config(path):
    repo = (os.environ.get("GITHUB_REPOSITORY") or "best546/apt-gap-monitor").strip()
    branch = (os.environ.get("GITHUB_BRANCH") or "main").strip()
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/config/{path.name}"
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        print(f"Loaded latest config from GitHub: {path.name}")
        return response.json()
    except Exception as exc:
        if (os.environ.get("ALLOW_LOCAL_CONFIG_FALLBACK") or "").lower() in ("1", "true", "yes"):
            print(f"Remote config unavailable; explicitly allowed local fallback for {path.name}: {type(exc).__name__}", file=sys.stderr)
            return json.loads(path.read_text(encoding="utf-8"))
        raise RuntimeError(f"Latest GitHub config is required: {path.name} ({type(exc).__name__})") from exc


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
        if m == 0:
            y, m = y - 1, 12
    return out


def month_set(n):
    return {m[:4] + "-" + m[4:] for m in month_keys(n)}


def parse_items(xml_text):
    root = ET.fromstring(xml_text)
    result = []
    for node in root.findall(".//item"):
        result.append({child.tag: (child.text or "").strip() for child in node})
    return result


def fetch_month(key, lawd, ym):
    params = {
        "serviceKey": unquote(key),
        "LAWD_CD": lawd,
        "DEAL_YMD": ym,
        "pageNo": 1,
        "numOfRows": 9999,
    }
    session = requests.Session()
    session.mount(
        "https://",
        HTTPAdapter(
            max_retries=Retry(
                total=4,
                connect=4,
                read=4,
                backoff_factor=2,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=("GET",),
            )
        ),
    )
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
    year = int(pick(row, "dealYear", "년"))
    month = int(pick(row, "dealMonth", "월"))
    day = int(pick(row, "dealDay", "일"))
    return {
        "apt": pick(row, "aptNm", "아파트"),
        "price_manwon": price,
        "area": area,
        "date": f"{year:04d}-{month:02d}-{day:02d}",
        "floor": int(pick(row, "floor", "층", default=0) or 0),
        "direct": pick(row, "dealingGbn", "거래유형") == "직거래",
        "cancelled": bool(pick(row, "cdealDay", "해제사유발생일")),
    }


def select_representative_basis(
    normal,
    representative_months=6,
    min_count=DEFAULT_MIN_REPRESENTATIVE_COUNT,
    max_months=DEFAULT_MAX_REPRESENTATIVE_MONTHS,
):
    max_months = max(representative_months, max_months)
    basis = []
    used_months = representative_months
    for months in range(representative_months, max_months + 1):
        recent_months = month_set(months)
        basis = [x for x in normal if x["date"][:7] in recent_months]
        used_months = months
        if len(basis) >= min_count:
            break
    return basis, used_months


def representative_confidence(basis):
    count = len(basis)
    if count == 0:
        return "NONE"
    latest_day = max(date.fromisoformat(x["date"]) for x in basis)
    age_days = max(0, (date.today() - latest_day).days)
    if count >= 8 and age_days <= 45:
        return "HIGH"
    if count >= 5:
        return "MEDIUM"
    if count >= 3:
        return "LOW"
    return "VERY_LOW"


def representative_signature(basis):
    if not basis:
        return None
    parts = sorted(
        f'{x["date"]}|{x["price_manwon"]}|{x["floor"]}|{x["area"]:.4f}|{norm(x["apt"])}'
        for x in basis
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def tx_identity(tx):
    if not tx:
        return None
    return (
        tx.get("date"),
        tx.get("price_manwon"),
        tx.get("floor"),
        round(float(tx.get("area", 0) or 0), 4),
    )


def classify_price_change(current, previous):
    if not previous:
        return "INITIAL"
    if current.get("representative_manwon") == previous.get("representative_manwon"):
        return "UNCHANGED"
    if "representative_count" not in previous:
        return "MIGRATED"

    current_latest = current.get("representative_latest")
    previous_latest = previous.get("representative_latest")
    if previous_latest and current_latest and tx_identity(current_latest) != tx_identity(previous_latest):
        if current_latest.get("date", "") >= previous_latest.get("date", ""):
            return "NEW_TRANSACTION"

    current_count = current.get("representative_count", 0)
    previous_count = previous.get("representative_count", previous.get("count", 0))
    if current_count > previous_count:
        return "NEW_TRANSACTION"

    current_months = current.get("representative_months")
    previous_months = previous.get("representative_months")
    if current_count < previous_count or (
        previous_months is not None and current_months != previous_months
    ):
        return "WINDOW_SHIFT"

    if (
        current.get("representative_signature")
        and previous.get("representative_signature")
        and current["representative_signature"] != previous["representative_signature"]
    ):
        return "DATA_REVISION"
    return "RECALCULATED"


def weaker_confidence(a, b):
    a = a if a in CONFIDENCE_RANK else "NONE"
    b = b if b in CONFIDENCE_RANK else "NONE"
    return a if CONFIDENCE_RANK[a] <= CONFIDENCE_RANK[b] else b


def summarize(
    c,
    rows,
    tol,
    default_min_floor=4,
    representative_months=6,
    representative_min_count=DEFAULT_MIN_REPRESENTATIVE_COUNT,
    representative_max_months=DEFAULT_MAX_REPRESENTATIVE_MONTHS,
):
    aliases = [norm(x) for x in c.get("aliases", []) + [c["name"]]]
    area_tolerance = c.get("area_tolerance", tol)
    min_floor = c.get("min_floor", default_min_floor)
    matches = []
    for row in rows:
        try:
            tx = tx_from(row)
        except (ValueError, TypeError):
            continue
        apt_name = norm(tx["apt"])
        name_ok = bool(apt_name) and (
            apt_name in aliases
            if c.get("match_mode") == "exact"
            else any(a in apt_name or apt_name in a for a in aliases if a)
        )
        allowed_dongs = c.get("dong_aliases", []) + ([c["dong"]] if c.get("dong") else [])
        dong_ok = not allowed_dongs or norm(pick(row, "umdNm", "법정동")) in {
            norm(d) for d in allowed_dongs
        }
        area_ok = (
            c.get("area_min", 0) <= tx["area"] <= c.get("area_max", 85)
            and abs(tx["area"] - c["area"]) <= area_tolerance
        )
        if name_ok and dong_ok and area_ok and not tx["cancelled"]:
            matches.append(tx)

    matches.sort(key=lambda x: x["date"], reverse=True)
    normal = [x for x in matches if x["floor"] >= min_floor and not x["direct"]]
    basis, used_months = select_representative_basis(
        normal,
        representative_months,
        representative_min_count,
        representative_max_months,
    )
    prices = [x["price_manwon"] for x in basis]
    monthly = []
    for month in sorted({x["date"][:7] for x in normal}):
        month_prices = [x["price_manwon"] for x in normal if x["date"].startswith(month)]
        monthly.append(
            {
                "month": month,
                "price_manwon": round(statistics.median(month_prices)),
                "count": len(month_prices),
            }
        )

    representative_latest = basis[0] if basis else None
    confidence = representative_confidence(basis)
    return {
        **c,
        "area_tolerance": area_tolerance,
        "min_floor": min_floor,
        "count": len(basis),
        "representative_count": len(basis),
        "representative_months": used_months,
        "representative_min_count": representative_min_count,
        "representative_max_months": representative_max_months,
        "confidence": confidence,
        "representative_signature": representative_signature(basis),
        "representative_latest": representative_latest,
        "history_count": len(matches),
        "representative_manwon": round(statistics.median(prices)) if prices else None,
        "latest": matches[0] if matches else None,
        "monthly_prices": monthly,
        "transactions": matches[:20],
    }


def load_previous_payload():
    if not OUTPUT.exists():
        return None
    try:
        payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) and isinstance(payload.get("items"), list) else None
    except (OSError, json.JSONDecodeError):
        return None


def main():
    key = (os.environ.get("MOLIT_API_KEY") or "").strip().strip('"').strip("'")
    if not key:
        sys.exit("MOLIT_API_KEY is required")

    cfg = load_config(CONFIG)
    asking = load_config(ASKING)
    previous_payload = load_previous_payload()
    previous_by_id = {
        x["id"]: x
        for x in (previous_payload or {}).get("items", [])
        if isinstance(x, dict) and x.get("id")
    }

    representative_months = cfg["months"]
    representative_min_count = cfg.get(
        "representative_min_count", DEFAULT_MIN_REPRESENTATIVE_COUNT
    )
    representative_max_months = cfg.get(
        "representative_max_months", DEFAULT_MAX_REPRESENTATIVE_MONTHS
    )

    cache = {}
    for lawd in sorted({c["lawd_cd"] for c in cfg["complexes"]}):
        cache[lawd] = []
        for ym in month_keys(cfg.get("history_months", cfg["months"])):
            cache[lawd].extend(fetch_month(key, lawd, ym))
            time.sleep(0.12)

    items = [
        summarize(
            c,
            cache[c["lawd_cd"]],
            cfg["area_tolerance"],
            cfg.get("min_floor", 4),
            representative_months,
            representative_min_count,
            representative_max_months,
        )
        for c in cfg["complexes"]
    ]

    for x in items:
        previous = previous_by_id.get(x["id"])
        x["price_change_reason"] = classify_price_change(x, previous)
        previous_price = previous.get("representative_manwon") if previous else None
        x["representative_change_manwon"] = (
            x["representative_manwon"] - previous_price
            if x["representative_manwon"] is not None and previous_price is not None
            else None
        )

    home = next(x for x in items if x["id"] == cfg["reference_id"])
    base = home["representative_manwon"]
    home_monthly = {x["month"]: x["price_manwon"] for x in home["monthly_prices"]}

    for x in items:
        x["gap_manwon"] = (
            x["representative_manwon"] - base
            if base is not None and x["representative_manwon"] is not None
            else None
        )
        x["asking_manwon"] = asking.get("prices", {}).get(x["id"])
        x["monthly_gaps"] = (
            [
                {
                    "month": p["month"],
                    "gap_manwon": p["price_manwon"] - home_monthly[p["month"]],
                    "target_price_manwon": p["price_manwon"],
                    "home_price_manwon": home_monthly[p["month"]],
                }
                for p in x["monthly_prices"]
                if p["month"] in home_monthly
            ]
            if x["id"] != cfg["reference_id"]
            else []
        )

        previous = previous_by_id.get(x["id"])
        previous_gap = previous.get("gap_manwon") if previous else None
        x["gap_change_manwon"] = (
            x["gap_manwon"] - previous_gap
            if x["gap_manwon"] is not None and previous_gap is not None
            else None
        )
        x["gap_confidence"] = (
            home["confidence"]
            if x["id"] == cfg["reference_id"]
            else weaker_confidence(home["confidence"], x["confidence"])
        )
        threshold = GAP_ALERT_THRESHOLDS.get(x["gap_confidence"])
        market_change = (
            x["price_change_reason"] == "NEW_TRANSACTION"
            or home["price_change_reason"] == "NEW_TRANSACTION"
        )
        x["significant_gap_change"] = bool(
            x["id"] != cfg["reference_id"]
            and threshold is not None
            and market_change
            and x["gap_change_manwon"] is not None
            and abs(x["gap_change_manwon"]) >= threshold
        )

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = {
        "generated_at": stamp,
        "months": cfg["months"],
        "history_months": cfg.get("history_months", cfg["months"]),
        "reference_id": cfg["reference_id"],
        "representative_policy": {
            "base_months": representative_months,
            "max_months": representative_max_months,
            "min_count": representative_min_count,
            "method": "adaptive_median",
            "excludes": ["below_min_floor", "direct_trade", "cancelled"],
        },
        "items": items,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    history = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
    history.append(
        {
            "generated_at": stamp,
            "values": {
                x["id"]: {
                    "price": x["representative_manwon"],
                    "gap": x["gap_manwon"],
                    "representative_count": x["representative_count"],
                    "representative_months": x["representative_months"],
                    "confidence": x["confidence"],
                    "gap_confidence": x["gap_confidence"],
                    "price_change_reason": x["price_change_reason"],
                    "significant_gap_change": x["significant_gap_change"],
                }
                for x in items
            },
        }
    )
    HISTORY.write_text(
        json.dumps(history[-104:], ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        safe = re.sub(
            r"serviceKey=[^&\s]+", "serviceKey=***", str(exc), flags=re.IGNORECASE
        )
        print(
            f"COLLECTOR_ERROR: {type(exc).__name__}: {safe}",
            file=sys.stderr,
        )
        sys.exit(1)
