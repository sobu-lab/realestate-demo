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
# 不動産取引価格API（無料・キー不要）
# ---------------------------------------------------------------------------

async def get_trade_prices(pref_code: str, city_code: str) -> list:
    """国土交通省 土地総合情報システム 取引価格API（キー不要）"""
    year = datetime.now().year
    url = "https://www.land.mlit.go.jp/webland/api/TradeListSearch"
    params = {
        "from": f"{year - 2}1",
        "to": f"{year}4",
        "area": pref_code,
        "city": city_code,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                return resp.json().get("data", [])[:20]
        except Exception:
            pass
    return []


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


def parse_prices(trades: list) -> dict:
    """取引価格データの解析"""
    if not trades:
        return {"available": True, "count": 0, "samples": []}
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
            "city_planning": t.get("CityPlanning", ""),
        })
    return {"available": True, "count": len(trades), "samples": samples}


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

    # 3. 不動産情報ライブラリ（用途地域・ハザード）を並行取得
    zoning_raw, flood_raw, tsunami_raw, landslide_raw = await asyncio.gather(
        get_reinfolib("XKT002", lat, lon),  # 用途地域
        get_reinfolib("XKT026", lat, lon),  # 洪水浸水想定区域（想定最大規模）
        get_reinfolib("XKT028", lat, lon),  # 津波浸水想定
        get_reinfolib("XKT029", lat, lon),  # 土砂災害警戒区域
    )

    # 4. 取引価格（キー不要の無料API）
    prices_raw = []
    if city_info.get("pref_code") and city_info.get("muniCd"):
        prices_raw = await get_trade_prices(city_info["pref_code"], city_info["muniCd"])

    return {
        "address": address,
        "lat": lat,
        "lon": lon,
        "city_info": city_info,
        "has_api_key": bool(MLIT_API_KEY),
        "zoning": parse_zoning(zoning_raw),
        "hazard": parse_hazard(flood_raw, tsunami_raw, landslide_raw),
        "prices": parse_prices(prices_raw),
    }


# 静的ファイル配信（フロントエンド）
app.mount("/", StaticFiles(directory="static", html=True), name="static")
