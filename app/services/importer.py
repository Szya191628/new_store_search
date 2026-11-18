import io
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from app.db import get_db
from app.services.classifier import (
    classify_store_track,
    extract_core_trade_name,
    extract_street_name,
    is_national_chain,
    is_non_store_industry,
    is_physical_store_address,
)
from app.services.pitch_generator import generate_pitch_script

# 智能表头别名映射（兼容企查查 / 天眼查 / 爱企查 / 探迹）
COLUMN_ALIASES: Dict[str, List[str]] = {
    "license_name": ["企业名称", "名称", "个体户名称", "店名", "商户名称", "公司名称", "主体名称"],
    "credit_code": ["统一社会信用代码", "信用代码", "社会信用代码", "注册号"],
    "address": ["注册地址", "住所", "经营场所", "地址", "企业地址", "营业场所"],
    "phone": ["联系电话", "电话", "手机", "手机号", "有效手机号", "联系方式", "企业联系电话"],
    "operator_name": ["法定代表人", "经营者", "法人", "负责人", "姓名"],
    "reg_date": ["成立日期", "注册日期", "核准日期", "登记日期"],
    "industry_raw": ["所属行业", "行业", "国标行业", "行业门类", "行业分类"],
    "business_scope": ["经营范围", "业务范围"],
    "city": ["城市", "所属城市", "市"],
    "district": ["区县", "所属区县", "区/县", "区"],
}

PHONE_CLEAN_RE = re.compile(r"(1[3-9]\d{9})")


def _normalize_col(col: str) -> str:
    return re.sub(r"\s+", "", str(col or "")).strip()


def _map_columns(columns: List[str]) -> Dict[str, str]:
    norm_to_orig = {_normalize_col(c): c for c in columns}
    mapped: Dict[str, str] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in norm_to_orig:
                mapped[field] = norm_to_orig[alias]
                break
        if field not in mapped:
            # 模糊包含匹配
            for norm_col, orig_col in norm_to_orig.items():
                if any(alias in norm_col for alias in aliases):
                    mapped[field] = orig_col
                    break
    return mapped


def _extract_phone(raw_phone: Any) -> str:
    text = str(raw_phone or "").strip()
    if text in ("", "nan", "None", "-", "--"):
        return ""
    m = PHONE_CLEAN_RE.search(text)
    if m:
        return m.group(1)
    # 保留座机或原始清洗后号码
    cleaned = re.split(r"[;；,，/|\s]+", text)[0].strip()
    return "" if cleaned in ("-", "--", "无", "暂无") else cleaned[:32]


def _read_dataframe(file_bytes: bytes, filename: str) -> pd.DataFrame:
    lower = filename.lower()
    if lower.endswith(".csv"):
        for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
            try:
                return pd.read_csv(io.BytesIO(file_bytes), encoding=enc, dtype=str)
            except UnicodeDecodeError:
                continue
        return pd.read_csv(io.BytesIO(file_bytes), encoding="utf-8", errors="ignore", dtype=str)
    elif lower.endswith((".xlsx", ".xls")):
        # 自动探测表头行（企查查/天眼查导出有时第1行是标题说明，第2行才是表头）
        df_raw = pd.read_excel(io.BytesIO(file_bytes), header=None, dtype=str)
        header_row = 0
        for idx in range(min(5, len(df_raw))):
            row_vals = [str(v) for v in df_raw.iloc[idx].tolist()]
            if any("企业名称" in v or "统一社会信用代码" in v or "注册地址" in v or "经营场所" in v for v in row_vals):
                header_row = idx
                break
        return pd.read_excel(io.BytesIO(file_bytes), header=header_row, dtype=str)
    else:
        raise ValueError("仅支持 .xlsx / .xls / .csv 格式的工商导出文件")


def import_leads_file(
    file_bytes: bytes,
    filename: str,
    uploaded_by: Optional[int] = None,
    default_city: str = "",
) -> Dict[str, Any]:
    df = _read_dataframe(file_bytes, filename)
    df = df.fillna("")
    col_map = _map_columns(list(df.columns))

    if "license_name" not in col_map or "address" not in col_map:
        raise ValueError(
            f"未能识别必要列名（需要店名/企业名称与注册地址/经营场所）。当前表头: {list(df.columns)}"
        )

    total_rows = len(df)
    imported_rows = 0
    skipped_dup = 0
    filtered_invalid = 0
    agency_flagged = 0

    with get_db() as db:
        batch_id = db.execute(
            "INSERT INTO import_batches (filename, total_rows, uploaded_by) VALUES (%s, %s, %s)",
            (filename, total_rows, uploaded_by),
        )

        # 预加载现有手机号出现频次，用于代账号码识别
        existing_phones = db.fetchall(
            "SELECT phone, COUNT(*) AS cnt FROM leads WHERE phone != '' GROUP BY phone"
        )
        phone_counts: Dict[str, int] = {r["phone"]: int(r["cnt"]) for r in existing_phones}

        inserted_phones: List[str] = []

        for _, row in df.iterrows():
            license_name = str(row.get(col_map["license_name"], "")).strip()
            address = str(row.get(col_map["address"], "")).strip()
            if not license_name or not address:
                filtered_invalid += 1
                continue

            credit_code = str(row.get(col_map.get("credit_code", ""), "")).strip() or None
            phone = _extract_phone(row.get(col_map.get("phone", ""), ""))
            operator_name = str(row.get(col_map.get("operator_name", ""), "")).strip()[:64]
            reg_date = str(row.get(col_map.get("reg_date", ""), "")).strip()[:32]
            industry_raw = str(row.get(col_map.get("industry_raw", ""), "")).strip()[:128]
            business_scope = str(row.get(col_map.get("business_scope", ""), "")).strip()
            city = str(row.get(col_map.get("city", ""), "")).strip() or default_city
            district = str(row.get(col_map.get("district", ""), "")).strip()

            # 1. 全局去重（按信用代码或 店名+地址）
            dedup_key = credit_code if credit_code else f"{license_name}::{address}"
            if credit_code:
                dup = db.fetchone(
                    "SELECT id FROM leads WHERE credit_code = %s OR dedup_key = %s",
                    (credit_code, dedup_key),
                )
            else:
                dup = db.fetchone("SELECT id FROM leads WHERE dedup_key = %s", (dedup_key,))
            if dup:
                skipped_dup += 1
                continue

            # 2. 非实体/住宅/行业黑名单过滤
            ok_addr, _ = is_physical_store_address(address)
            if not ok_addr:
                filtered_invalid += 1
                continue

            bad_ind, _ = is_non_store_industry(license_name, industry_raw, business_scope)
            if bad_ind:
                filtered_invalid += 1
                continue

            # 3. 连锁品牌剔除
            if is_national_chain(license_name):
                filtered_invalid += 1
                continue

            # 4. 实体双轨标签与字号/街道提取
            store_track = classify_store_track(license_name, industry_raw, business_scope)
            core_trade_name = extract_core_trade_name(license_name)
            street_name = extract_street_name(address)

            if phone:
                phone_counts[phone] = phone_counts.get(phone, 0) + 1
                inserted_phones.append(phone)

            freq = phone_counts.get(phone, 1) if phone else 0
            is_agency = 1 if (phone and freq >= 2) else 0
            rec_action = "offline_visit" if (is_agency or not phone) else "telesales"

            pitch = generate_pitch_script(
                store_name=core_trade_name or license_name,
                store_track=store_track,
                grade="S",
                street_name=street_name,
                is_agency_phone=bool(is_agency),
            )

            db.execute(
                """
                INSERT INTO leads (
                    credit_code, dedup_key, license_name, core_trade_name, operator_name,
                    phone, phone_freq, is_agency_phone, address, street_name, district, city,
                    reg_date, industry_raw, business_scope, store_track, is_chain_brand,
                    grade, recommended_action, pitch_script, batch_id
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, 0,
                    'S', %s, %s, %s
                )
                """,
                (
                    credit_code, dedup_key, license_name, core_trade_name, operator_name,
                    phone, freq, is_agency, address, street_name, district, city,
                    reg_date, industry_raw, business_scope, store_track,
                    rec_action, pitch, batch_id,
                ),
            )
            imported_rows += 1

        # 回刷本批次触发“同号多店 >= 2”的历史/同批记录为代账号码
        flagged_phones = {p for p in inserted_phones if phone_counts.get(p, 0) >= 2}
        for p in flagged_phones:
            cnt = phone_counts[p]
            db.execute(
                """
                UPDATE leads
                SET phone_freq = %s, is_agency_phone = 1, recommended_action = 'offline_visit'
                WHERE phone = %s
                """,
                (cnt, p),
            )
        if flagged_phones:
            placeholders = ",".join(["%s"] * len(flagged_phones))
            row_cnt = db.fetchone(
                f"SELECT COUNT(*) AS c FROM leads WHERE phone IN ({placeholders}) AND is_agency_phone = 1",
                tuple(flagged_phones),
            )
            agency_flagged = int(row_cnt["c"]) if row_cnt else 0

        db.execute(
            """
            UPDATE import_batches
            SET imported_rows = %s, skipped_dup = %s, filtered_invalid = %s, agency_flagged = %s
            WHERE id = %s
            """,
            (imported_rows, skipped_dup, filtered_invalid, agency_flagged, batch_id),
        )

    return {
        "batch_id": batch_id,
        "filename": filename,
        "total_rows": total_rows,
        "imported_rows": imported_rows,
        "skipped_dup": skipped_dup,
        "filtered_invalid": filtered_invalid,
        "agency_flagged": agency_flagged,
    }
