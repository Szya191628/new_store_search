from difflib import SequenceMatcher
import re
from typing import Any, Dict, List, Optional
import httpx

BRANCH_SUFFIX_RE = re.compile(r"[（(][^（）()]{1,12}(?:店|分店|旗舰店|总店)[）)]$")


def compute_name_similarity(core_name: str, poi_name: str) -> int:
    """计算字号核心词与高德 POI 名称的匹配度 (0-100)。"""
    a = (core_name or "").strip()
    b = BRANCH_SUFFIX_RE.sub("", (poi_name or "").strip()).strip()
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a in b or b in a:
        return 90

    a_chars = set(a)
    b_chars = set(b)
    char_overlap = len(a_chars & b_chars) / max(min(len(a_chars), len(b_chars)), 1)
    seq_ratio = SequenceMatcher(None, a, b).ratio()

    score = int((char_overlap * 0.5 + seq_ratio * 0.5) * 100)
    return min(max(score, 0), 95)


class AmapClient:
    GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"
    AROUND_URL = "https://restapi.amap.com/v3/place/around"

    def __init__(self, api_key: str, timeout: float = 8.0):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    def geocode(self, address: str, city: str = "") -> Optional[Dict[str, float]]:
        if not self.api_key or not address:
            return None
        params = {"key": self.api_key, "address": address}
        if city:
            params["city"] = city
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(self.GEOCODE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        if data.get("status") != "1" or not data.get("geocodes"):
            return None
        loc = data["geocodes"][0].get("location", "")
        if "," not in loc:
            return None
        lng_str, lat_str = loc.split(",", 1)
        return {"lng": float(lng_str), "lat": float(lat_str)}

    def search_around_100m(self, lng: float, lat: float, radius: int = 100) -> List[Dict[str, Any]]:
        if not self.api_key:
            return []
        params = {
            "key": self.api_key,
            "location": f"{lng},{lat}",
            "radius": radius,
            "offset": 20,
            "page": 1,
            "extensions": "base",
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(self.AROUND_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        if data.get("status") != "1":
            return []
        return data.get("pois") or []

    def check_store_poi(self, address: str, core_trade_name: str, city: str = "") -> Dict[str, Any]:
        geo = self.geocode(address, city=city)
        if not geo:
            return {
                "lng": None,
                "lat": None,
                "amap_poi_found": 0,
                "amap_poi_name": "",
                "amap_poi_distance": None,
                "amap_match_score": 0,
            }

        pois = self.search_around_100m(geo["lng"], geo["lat"], radius=100)
        best_score = 0
        best_name = ""
        best_dist: Optional[int] = None

        for poi in pois:
            pname = str(poi.get("name", ""))
            score = compute_name_similarity(core_trade_name, pname)
            if score > best_score:
                best_score = score
                best_name = pname
                try:
                    best_dist = int(float(poi.get("distance", 0)))
                except (ValueError, TypeError):
                    best_dist = None

        poi_found = 1 if best_score >= 55 else 0
        return {
            "lng": geo["lng"],
            "lat": geo["lat"],
            "amap_poi_found": poi_found,
            "amap_poi_name": best_name if poi_found else "",
            "amap_poi_distance": best_dist if poi_found else None,
            "amap_match_score": best_score,
        }
