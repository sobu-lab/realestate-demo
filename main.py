import math
import os
import asyncio
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="不動産調査ツール")

MLIT_API_KEY = os.getenv("MLIT_API_KEY", "")
REINFOLIB_BASE = "https://www.reinfolib.mlit.go.jp/ex-api/external"
REINFOLIB_ZOOM = 15  # タイルズームレベル（15が有効）

# 洪水浸水深コード変換
FLOOD_DEPTH_MAP = {
    1: "0.5m未満",
    2: "0.5〜1.0m",
    3: "1.0〜2.0m",
    4: "2.0〜3.0m",
    5: "3.0〜5.0m",
    6: "5.0〜10.0m",
    7: "10.0m以上",
}

# 土砂災害区域区分コード変換
LANDSLIDE_TYPE_MAP = {
    "1": "土石流",
    "2": "急傾斜地崩壊",
    "3": "地すべり",
}


def latlon_to_tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    """緯度経度をタイル座標（x, y）に変換"""
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


# ---------------------------------------------------------------------------
# 地理情報API
# ---------------------------------------------------------------------------

async def geocode(address: str) -> tuple[float, float]:
    """住所→緯度経度（国土地理院 AddressSearch API）"""
    url = "https://msearch.gsi.go.jp/address-search/AddressSearch"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params={"q": address})
        resp.raise_for_status()
        data = resp.json()
    if not data:
        raise HTTPException(status_code=404, detail=f"住所が見つかりません: {address}")
    coords = data[0]["geometry"]["coordinates"]  # [lon, lat]
    return float(coords[1]), float(coords[0])


async def get_city_info(lat: float, lon: float) -> dict:
    """緯度経度→市区町村コード（国土地理院 逆ジオコーダAPI）"""
    url = "https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params={"lon": lon, "lat": lat})
        data = resp.json()
    result = data.get("results", {})
    muni_cd = result.get("muniCd", "")
    return {
        "muniCd": muni_cd,
        "pref_code": muni_cd[:2] if len(muni_cd) >= 2 else "",
        "lv01Nm": result.get("lv01Nm", ""),
    }


# ---------------------------------------------------------------------------
# 不動産情報ライブラリ API（要APIキー・無料登録）
# ---------------------------------------------------------------------------

async def get_reinfolib(endpoint: str, lat: float, lon: float) -> dict | None:
    """不動産情報ライブラリAPIからGeoJSONデータ取得（タイル座標方式）"""
    if not MLIT_API_KEY:
        return None  # キー未設定 → Noneを返す
    x, y = latlon_to_tile(lat, lon, REINFOLIB_ZOOM)
    url = f"{REINFOLIB_BASE}/{endpoint}/"
    params = {"response_format": "geojson", "z": REINFOLIB_ZOOM, "x": x, "y": y}
    headers = {"Ocp-Apim-Subscription-Key": MLIT_API_KEY}
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
    return {}  # エラー → 空辞書


# ---------------------------------------------------------------------------
# 不動産取引価格・地価公示 API（reinfolib XIT001 / XPT002）
# ---------------------------------------------------------------------------

def _recent_quarters(n: int = 4) -> list[tuple[int, int]]:
    """直近 n 四半期の (year, quarter) リストを返す"""
    now = datetime.now()
    year, month = now.year, now.month
    cur_q = (month - 1) // 3 + 1
    quarters = []
    for i in range(1, n + 1):
        q = cur_q - i
        y = year
        while q <= 0:
            q += 4
            y -= 1
        quarters.append((y, q))
    return quarters


async def _fetch_xit001(pref_code: str, city_code: str, year: int, quarter: int) -> list:
    """XIT001 1四半期分の取引価格取得"""
    params = {"year": year, "quarter": quarter, "area": pref_code, "city": city_code}
    headers = {"Ocp-Apim-Subscription-Key": MLIT_API_KEY}
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(f"{REINFOLIB_BASE}/XIT001/", params=params, headers=headers)
            if resp.status_code == 200:
                return resp.json().get("data", [])
        except Exception:
            pass
    return []


async def get_trade_prices(pref_code: str, city_code: str) -> list:
    """取引価格取得 - XIT001（直近4四半期を並行取得）"""
    if not MLIT_API_KEY:
        return []
    quarters = _recent_quarters(4)
    results_list = await asyncio.gather(*[
        _fetch_xit001(pref_code, city_code, y, q) for y, q in quarters
    ])
    all_data: list = []
    for r in results_list:
        all_data.extend(r)
    return all_data[:20]


async def _fetch_xpt002_tile(client: httpx.AsyncClient, z: int, x: int, y: int, year: int, headers: dict) -> list:
    """XPT002 単タイル取得"""
    params = {"response_format": "geojson", "z": z, "x": x, "y": y, "year": year}
    try:
        resp = await client.get(f"{REINFOLIB_BASE}/XPT002/", params=params, headers=headers)
        if resp.status_code == 200:
            return resp.json().get("features", [])
    except Exception:
        pass
    return []


async def get_land_prices(lat: float, lon: float) -> list:
    """地価公示・地価調査取得 - XPT002
    z=15 単タイル → なければ z=13 で 3×3 グリッド並行検索（タイル境界またぎ対策）。
    """
    if not MLIT_API_KEY:
        return []
    year = datetime.now().year - 1
    headers = {"Ocp-Apim-Subscription-Key": MLIT_API_KEY}

    async with httpx.AsyncClient(timeout=15.0) as client:
        # z=15 単タイル（都市部は十分）
        cx, cy = latlon_to_tile(lat, lon, 15)
        features = await _fetch_xpt002_tile(client, 15, cx, cy, year, headers)
        if features:
            return features

        # z=13 で 3×3 グリッド並行検索（地方・境界またぎ対策）
        cx, cy = latlon_to_tile(lat, lon, 13)
        tasks = [
            _fetch_xpt002_tile(client, 13, cx + dx, cy + dy, year, headers)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
        ]
        results = await asyncio.gather(*tasks)
        seen: set = set()
        merged: list = []
        for tile_features in results:
            for f in tile_features:
                fid = f.get("properties", {}).get("_id") or str(f)
                if fid not in seen:
                    seen.add(fid)
                    merged.append(f)
        return merged


# ---------------------------------------------------------------------------
# データ解析
# ---------------------------------------------------------------------------

def parse_zoning(data: dict | None) -> dict:
    """用途地域データの解析"""
    if data is None:
        return {"available": False, "reason": "no_key"}
    if not data or "features" not in data:
        return {"available": False, "reason": "error"}
    features = data.get("features", [])
    if not features:
        return {"available": True, "zones": [], "message": "この地点の用途地域情報なし（市街化調整区域等）"}
    zones = []
    for f in features[:3]:
        p = f.get("properties", {})
        zones.append({
            "name": p.get("use_area_ja") or p.get("youto") or "不明",
            "coverage": p.get("u_building_coverage_ratio_ja") or p.get("kenpeiritsu") or "-",
            "floor_ratio": p.get("u_floor_area_ratio_ja") or p.get("yosekiritsu") or "-",
            "city": p.get("city_name") or "",
        })
    return {"available": True, "zones": zones}


def parse_hazard_item(data: dict | None, kind: str) -> dict:
    """各ハザードデータの解析"""
    if data is None:
        return {"available": False, "reason": "no_key"}
    if not data or "features" not in data:
        return {"available": False, "reason": "error"}
    features = data.get("features", [])
    if not features:
        return {"available": True, "risk": "low", "label": "リスクなし", "detail": ""}
    # リスクあり
    p = features[0].get("properties", {})
    detail = ""
    if kind == "flood":
        depth_code = p.get("A31a_205") or p.get("depth_code")
        depth = FLOOD_DEPTH_MAP.get(depth_code, "") if isinstance(depth_code, int) else ""
        detail = f"浸水深: {depth}" if depth else f"{len(features)}区域が重複"
    elif kind == "tsunami":
        detail = f"{len(features)}区域が重複"
    elif kind == "landslide":
        ltype = p.get("A33_004") or p.get("type", "")
        detail = LANDSLIDE_TYPE_MAP.get(str(ltype), str(ltype)) if ltype else f"{len(features)}区域が重複"
    return {"available": True, "risk": "high", "label": "リスクあり", "detail": detail}


def parse_hazard(flood: dict | None, tsunami: dict | None, landslide: dict | None) -> dict:
    return {
        "flood": parse_hazard_item(flood, "flood"),
        "tsunami": parse_hazard_item(tsunami, "tsunami"),
        "landslide": parse_hazard_item(landslide, "landslide"),
    }


def parse_prices(trades: list, land_price_features: list) -> dict:
    """取引価格・地価公示データの解析"""
    samples = []
    for t in trades[:8]:
        price = t.get("TradePrice", "")
        if not price:
            continue
        samples.append({
            "type": t.get("Type", ""),
            "area": t.get("Area", ""),
            "price": price,
            "unit_price": t.get("UnitPrice", ""),
            "period": t.get("Period", ""),
            "district": t.get("DistrictName", ""),
        })

    # 地価公示サマリー（近傍ポイントの平均変動率と代表価格）
    land_prices = []
    for f in land_price_features:
        p = f.get("properties", {})
        land_prices.append({
            "price": p.get("u_current_years_price_ja", ""),
            "use": p.get("use_category_name_ja", ""),
            "change": p.get("year_on_year_change_rate", ""),
            "station": p.get("nearest_station_name_ja", ""),
        })

    return {
        "available": True,
        "count": len(trades),
        "samples": samples,
        "land_prices": land_prices,
    }


# ---------------------------------------------------------------------------
# APIエンドポイント
# ---------------------------------------------------------------------------

@app.get("/api/search")
async def search(address: str):
    """住所から不動産情報を一括取得"""
    if not address.strip():
        raise HTTPException(status_code=400, detail="住所を入力してください")

    # 1. ジオコーディング
    lat, lon = await geocode(address)

    # 2. 市区町村コード取得
    city_info = await get_city_info(lat, lon)

    # 3. 不動産情報ライブラリ（用途地域・ハザード・地価）を並行取得
    zoning_raw, flood_raw, tsunami_raw, landslide_raw, land_price_raw = await asyncio.gather(
        get_reinfolib("XKT002", lat, lon),  # 用途地域
        get_reinfolib("XKT026", lat, lon),  # 洪水浸水想定区域（想定最大規模）
        get_reinfolib("XKT028", lat, lon),  # 津波浸水想定
        get_reinfolib("XKT029", lat, lon),  # 土砂災害警戒区域
        get_land_prices(lat, lon),          # 地価公示・地価調査（XPT002）
    )

    # 4. 取引価格（XIT001: year+quarter+area 方式）
    trades_raw = []
    if city_info.get("pref_code") and city_info.get("muniCd"):
        trades_raw = await get_trade_prices(city_info["pref_code"], city_info["muniCd"])

    return {
        "address": address,
        "lat": lat,
        "lon": lon,
        "city_info": city_info,
        "has_api_key": bool(MLIT_API_KEY),
        "zoning": parse_zoning(zoning_raw),
        "hazard": parse_hazard(flood_raw, tsunami_raw, landslide_raw),
        "prices": parse_prices(trades_raw, land_price_raw if isinstance(land_price_raw, list) else []),
    }


# 静的ファイル配信（フロントエンド）
app.mount("/", StaticFiles(directory="static", html=True), name="static")
