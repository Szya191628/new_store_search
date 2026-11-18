import random
import time
import urllib.parse
from typing import Any, Dict, Optional

from app.config import settings
from app.db import get_db
from app.services.amap_client import AmapClient, compute_name_similarity
from app.services.pitch_generator import generate_pitch_script

SLIDER_KEYWORDS = ("验证码", "滑块", "请完成安全验证", "访问异常", "人机验证", "captcha")


def build_quick_jump_urls(lead: Dict[str, Any]) -> Dict[str, str]:
    core_name = lead.get("core_trade_name") or lead.get("license_name", "")
    city = lead.get("city", "")
    street = lead.get("street_name", "")
    query = urllib.parse.quote(f"{city} {core_name}".strip())
    douyin_url = f"https://www.douyin.com/search/{query}"
    meituan_url = f"https://www.meituan.com/s/{query}/"

    if lead.get("lng") and lead.get("lat"):
        encoded_name = urllib.parse.quote(core_name)
        amap_nav_url = f"https://uri.amap.com/marker?position={lead['lng']},{lead['lat']}&name={encoded_name}"
    else:
        encoded_addr = urllib.parse.quote(lead.get("address", ""))
        amap_nav_url = f"https://ditu.amap.com/search?query={encoded_addr}"

    doubao_prompt = f"请帮我查一下{city}{street}附近的「{core_name}」地址、营业时间和联系电话"
    return {
        "douyin_search_url": douyin_url,
        "meituan_search_url": meituan_url,
        "amap_nav_url": amap_nav_url,
        "doubao_url": "https://www.doubao.com/chat/",
        "doubao_prompt": doubao_prompt,
    }


def compute_lead_grade(amap_poi_found: int, douyin_status: str, meituan_status: str) -> Dict[str, str]:
    """
    S 级（纯空白拓荒）：高德/抖音均无 POI 且美团无团购
    A 级（有定位未认领）：高德或抖音已有 POI，但抖音来客未认领且美团无团购
    B 级（美团已开、抖音空白）：美团已上架/有团购，但抖音未认领
    D 级（已成熟运营）：抖音来客已认领/已开团购
    """
    if douyin_status == "claimed":
        return {"grade": "D", "doubao_status": "indexed"}

    if meituan_status == "listed":
        return {"grade": "B", "doubao_status": "partial"}

    if amap_poi_found == 1 or douyin_status == "unclaimed_poi":
        return {"grade": "A", "doubao_status": "inferred_missing"}

    return {"grade": "S", "doubao_status": "inferred_missing"}


def _playwright_check_store(core_name: str, city: str) -> Dict[str, str]:
    """
    使用 Playwright 低频队列检索抖音与美团网页端公开搜索页；
    一旦遇到滑块/验证码或异常，立刻降级为 manual_review，绝不卡死队列。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"douyin_status": "manual_review", "meituan_status": "manual_review"}

    douyin_status = "manual_review"
    meituan_status = "manual_review"
    query = urllib.parse.quote(f"{city} {core_name}".strip())

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=settings.playwright_headless)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            page.set_default_timeout(10000)

            # 1. 抖音搜索初探
            try:
                page.goto(f"https://www.douyin.com/search/{query}", wait_until="domcontentloaded")
                time.sleep(random.uniform(settings.playwright_min_delay, settings.playwright_max_delay))
                content = page.content()
                if any(kw in content for kw in SLIDER_KEYWORDS):
                    douyin_status = "manual_review"
                elif core_name in content and ("团购" in content or "来客" in content or "官方" in content):
                    douyin_status = "claimed"
                elif core_name in content:
                    douyin_status = "unclaimed_poi"
                else:
                    douyin_status = "not_found"
            except Exception:
                douyin_status = "manual_review"

            # 2. 美团搜索初探
            try:
                page.goto(f"https://www.meituan.com/s/{query}/", wait_until="domcontentloaded")
                time.sleep(random.uniform(settings.playwright_min_delay, settings.playwright_max_delay))
                content_mt = page.content()
                if any(kw in content_mt for kw in SLIDER_KEYWORDS):
                    meituan_status = "manual_review"
                elif compute_name_similarity(core_name, content_mt[:3000]) >= 60 or core_name in content_mt:
                    meituan_status = "listed"
                else:
                    meituan_status = "not_found"
            except Exception:
                meituan_status = "manual_review"

            browser.close()
    except Exception:
        return {"douyin_status": "manual_review", "meituan_status": "manual_review"}

    return {"douyin_status": douyin_status, "meituan_status": meituan_status}


def run_verification_batch(limit: int = 50, use_playwright: Optional[bool] = None) -> Dict[str, int]:
    """执行两级漏斗核验：第1级高德官方 API 全量清洗定位 -> 第2级 Playwright 慢速队列核验。"""
    with get_db() as db:
        cfg_rows = db.fetchall("SELECT setting_key, setting_value FROM system_settings")
        cfg = {r["setting_key"]: r["setting_value"] for r in cfg_rows}

        amap_key = cfg.get("amap_api_key", "")
        default_city = cfg.get("default_city", "")
        pw_enabled = (
            use_playwright
            if use_playwright is not None
            else (cfg.get("enable_playwright", "true").lower() == "true")
        )

        leads = db.fetchall(
            """
            SELECT * FROM leads
            WHERE douyin_status = 'pending' OR meituan_status = 'pending'
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        )

    amap = AmapClient(amap_key) if amap_key else None
    processed = 0
    amap_checked = 0
    pw_checked = 0

    for lead in leads:
        amap_res = {
            "lng": lead.get("lng"),
            "lat": lead.get("lat"),
            "amap_poi_found": lead.get("amap_poi_found", 0),
            "amap_poi_name": lead.get("amap_poi_name", ""),
            "amap_poi_distance": lead.get("amap_poi_distance"),
            "amap_match_score": lead.get("amap_match_score", 0),
        }

        if amap and (lead.get("lng") is None or lead.get("lat") is None):
            try:
                amap_res = amap.check_store_poi(
                    address=lead["address"],
                    core_trade_name=lead.get("core_trade_name") or lead["license_name"],
                    city=lead.get("city") or default_city,
                )
                amap_checked += 1
            except Exception:
                pass

        if pw_enabled:
            pw_res = _playwright_check_store(
                core_name=lead.get("core_trade_name") or lead["license_name"],
                city=lead.get("city") or default_city,
            )
            pw_checked += 1
        else:
            pw_res = {"douyin_status": "manual_review", "meituan_status": "manual_review"}

        grading = compute_lead_grade(
            amap_poi_found=amap_res["amap_poi_found"],
            douyin_status=pw_res["douyin_status"],
            meituan_status=pw_res["meituan_status"],
        )

        pitch = generate_pitch_script(
            store_name=lead.get("core_trade_name") or lead["license_name"],
            store_track=lead["store_track"],
            grade=grading["grade"],
            street_name=lead.get("street_name", ""),
            is_agency_phone=bool(lead.get("is_agency_phone")),
        )

        with get_db() as db:
            db.execute(
                """
                UPDATE leads SET
                    lng = %s, lat = %s, amap_poi_found = %s, amap_poi_name = %s,
                    amap_poi_distance = %s, amap_match_score = %s,
                    douyin_status = %s, meituan_status = %s, doubao_status = %s,
                    grade = %s, pitch_script = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (
                    amap_res["lng"],
                    amap_res["lat"],
                    amap_res["amap_poi_found"],
                    amap_res["amap_poi_name"],
                    amap_res["amap_poi_distance"],
                    amap_res["amap_match_score"],
                    pw_res["douyin_status"],
                    pw_res["meituan_status"],
                    grading["doubao_status"],
                    grading["grade"],
                    pitch,
                    lead["id"],
                ),
            )
        processed += 1

    return {"processed": processed, "amap_checked": amap_checked, "pw_checked": pw_checked}
