"""판매점 주소를 GPS 좌표로 변환한다.

우선순위
1) 카카오 로컬 API (한국 도로명/지번 주소에 가장 정확) — KAKAO_REST_API_KEY
2) OpenStreetMap Nominatim (키 없이 동작, 정확도는 주소 품질에 따라 떨어질 수 있음)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


def load_env() -> None:
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


@dataclass
class GeocodeResult:
    lat: float
    lng: float
    source: str
    matched_address: str = ""


def _http_json(url: str, headers: dict[str, str], timeout: int = 12) -> dict | list | None:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code} {url.split('?')[0]} {body[:300]}")
        return None
    except Exception as exc:
        print(f"HTTP error: {exc}")
        return None


def geocode_kakao(address: str) -> GeocodeResult | None:
    key = os.environ.get("KAKAO_REST_API_KEY", "").strip()
    if not key:
        return None

    headers = {"Authorization": f"KakaoAK {key}"}
    query = urllib.parse.quote(address)

    data = _http_json(
        f"https://dapi.kakao.com/v2/local/search/address.json?query={query}",
        headers,
    )
    docs = (data or {}).get("documents") if isinstance(data, dict) else None
    if docs:
        doc = docs[0]
        return GeocodeResult(
            lat=float(doc["y"]),
            lng=float(doc["x"]),
            source="kakao_address",
            matched_address=doc.get("address_name") or address,
        )

    data = _http_json(
        f"https://dapi.kakao.com/v2/local/search/keyword.json?query={query}",
        headers,
    )
    docs = (data or {}).get("documents") if isinstance(data, dict) else None
    if docs:
        doc = docs[0]
        return GeocodeResult(
            lat=float(doc["y"]),
            lng=float(doc["x"]),
            source="kakao_keyword",
            matched_address=doc.get("place_name") or doc.get("address_name") or address,
        )
    return None


def geocode_nominatim(address: str) -> GeocodeResult | None:
    query = urllib.parse.urlencode(
        {"q": address, "format": "json", "limit": 1, "countrycodes": "kr", "addressdetails": 0}
    )
    data = _http_json(
        f"https://nominatim.openstreetmap.org/search?{query}",
        headers={
            "User-Agent": "RS-Treasure-Hunter/0.1 (store geocoding)",
            "Accept-Language": "ko",
        },
    )
    if isinstance(data, list) and data:
        item = data[0]
        return GeocodeResult(
            lat=float(item["lat"]),
            lng=float(item["lon"]),
            source="nominatim",
            matched_address=item.get("display_name") or address,
        )
    return None


def geocode_address(address: str) -> GeocodeResult | None:
    address = (address or "").strip()
    if not address:
        return None
    result = geocode_kakao(address)
    if result:
        return result
    return geocode_nominatim(address)


def geocode_missing_stores(conn, sleep_seconds: float | None = None) -> dict:
    """좌표가 없는 판매점을 주소로 변환해 채운다."""
    rows = conn.execute(
        "SELECT id, name, address, lat, lng FROM stores WHERE lat = 0 AND lng = 0"
    ).fetchall()

    key = os.environ.get("KAKAO_REST_API_KEY", "").strip()
    delay = sleep_seconds if sleep_seconds is not None else (0.25 if key else 1.1)

    filled = 0
    failed: list[str] = []
    source = "kakao" if key else "nominatim"

    for i, row in enumerate(rows, start=1):
        result = geocode_address(row["address"])
        if result:
            conn.execute(
                "UPDATE stores SET lat = ?, lng = ? WHERE id = ?",
                (result.lat, result.lng, row["id"]),
            )
            filled += 1
        else:
            failed.append(f"{row['name']} ({row['address']})")
        if i % 50 == 0:
            conn.commit()
            print(f"geocode progress {i}/{len(rows)} filled={filled} failed={len(failed)}", flush=True)
        if delay:
            time.sleep(delay)

    return {
        "provider": source,
        "attempted": len(rows),
        "filled": filled,
        "failed": failed[:50],
        "failed_count": len(failed),
    }
