import base64
import hmac
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import settings
from app.db import get_db, hash_pin, init_db
from app.services.importer import import_leads_file
from app.services.pitch_generator import generate_pitch_script
from app.services.verifier import build_quick_jump_urls, compute_lead_grade, run_verification_batch

app = FastAPI(title="本地新店拓客雷达 MVP", version="1.0.0")
STATIC_DIR = Path(__file__).parent / "static"


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def _create_token(user: Dict[str, Any]) -> str:
    payload = {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "exp": int(time.time()) + 86400 * 7,
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("utf-8")
    sig = hmac.new(settings.app_secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def _verify_token(token: str) -> Dict[str, Any]:
    try:
        raw, sig = token.split(".", 1)
        expected = hmac.new(settings.app_secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad sig")
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("utf-8")).decode("utf-8"))
        if payload.get("exp", 0) < time.time():
            raise ValueError("expired")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="登录状态无效或已过期，请重新登录")


def get_current_user(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="请先登录")
    token = authorization[7:].strip()
    return _verify_token(token)


def require_admin(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return user


class LoginRequest(BaseModel):
    username: str
    pin: str


class CreateUserRequest(BaseModel):
    username: str
    display_name: str
    pin: str
    role: str = "sales"


class FollowupRequest(BaseModel):
    follow_status: str
    note: str = ""


class AssignRequest(BaseModel):
    user_id: int


class ManualVerifyRequest(BaseModel):
    douyin_status: str
    meituan_status: str


class SettingsUpdateRequest(BaseModel):
    amap_api_key: Optional[str] = None
    default_city: Optional[str] = None
    enable_playwright: Optional[str] = None


@app.get("/")
def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/auth/login")
def login(req: LoginRequest):
    with get_db() as db:
        user = db.fetchone(
            "SELECT id, username, display_name, role, pin_hash, is_active FROM users WHERE username = %s",
            (req.username.strip(),),
        )
    if not user or not user["is_active"] or user["pin_hash"] != hash_pin(req.pin.strip()):
        raise HTTPException(status_code=401, detail="账号或口令错误")
    token = _create_token(user)
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "role": user["role"],
        },
    }


@app.get("/api/auth/me")
def me(user: Dict[str, Any] = Depends(get_current_user)):
    return {"user": user}


@app.get("/api/users")
def list_users(user: Dict[str, Any] = Depends(get_current_user)):
    with get_db() as db:
        rows = db.fetchall("SELECT id, username, display_name, role, is_active FROM users ORDER BY id ASC")
    return {"users": rows}


@app.post("/api/users")
def create_user(req: CreateUserRequest, admin: Dict[str, Any] = Depends(require_admin)):
    if req.role not in ("admin", "sales"):
        raise HTTPException(status_code=400, detail="角色仅支持 admin 或 sales")
    with get_db() as db:
        exists = db.fetchone("SELECT id FROM users WHERE username = %s", (req.username.strip(),))
        if exists:
            raise HTTPException(status_code=400, detail="账号已存在")
        uid = db.execute(
            "INSERT INTO users (username, display_name, role, pin_hash) VALUES (%s, %s, %s, %s)",
            (req.username.strip(), req.display_name.strip(), req.role, hash_pin(req.pin.strip())),
        )
    return {"id": uid, "username": req.username.strip(), "display_name": req.display_name.strip(), "role": req.role}


@app.get("/api/stats")
def get_stats(user: Dict[str, Any] = Depends(get_current_user)):
    with get_db() as db:
        total = db.fetchone("SELECT COUNT(*) AS c FROM leads")["c"]
        s_cnt = db.fetchone("SELECT COUNT(*) AS c FROM leads WHERE grade = 'S'")["c"]
        a_cnt = db.fetchone("SELECT COUNT(*) AS c FROM leads WHERE grade = 'A'")["c"]
        b_cnt = db.fetchone("SELECT COUNT(*) AS c FROM leads WHERE grade = 'B'")["c"]
        agency_cnt = db.fetchone("SELECT COUNT(*) AS c FROM leads WHERE is_agency_phone = 1")["c"]
        public_cnt = db.fetchone("SELECT COUNT(*) AS c FROM leads WHERE claim_user_id IS NULL")["c"]
        mine_cnt = db.fetchone("SELECT COUNT(*) AS c FROM leads WHERE claim_user_id = %s", (user["id"],))["c"]
        signed_cnt = db.fetchone("SELECT COUNT(*) AS c FROM leads WHERE follow_status = '已签约'")["c"]
        streets = db.fetchall(
            "SELECT street_name, COUNT(*) AS cnt FROM leads WHERE street_name != '' GROUP BY street_name ORDER BY cnt DESC LIMIT 50"
        )
    return {
        "total": total,
        "grade_s": s_cnt,
        "grade_a": a_cnt,
        "grade_b": b_cnt,
        "agency_phone": agency_cnt,
        "public_pool": public_cnt,
        "my_claimed": mine_cnt,
        "signed": signed_cnt,
        "streets": streets,
    }


@app.get("/api/leads")
def list_leads(
    pool: str = Query(default="all"),
    grade: str = Query(default=""),
    store_track: str = Query(default=""),
    follow_status: str = Query(default=""),
    street_name: str = Query(default=""),
    recommended_action: str = Query(default=""),
    q: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: Dict[str, Any] = Depends(get_current_user),
):
    conditions: List[str] = []
    params: List[Any] = []

    if pool == "public":
        conditions.append("claim_user_id IS NULL")
    elif pool == "mine":
        conditions.append("claim_user_id = %s")
        params.append(user["id"])

    if grade:
        conditions.append("grade = %s")
        params.append(grade)
    if store_track:
        conditions.append("store_track = %s")
        params.append(store_track)
    if follow_status:
        conditions.append("follow_status = %s")
        params.append(follow_status)
    if street_name:
        conditions.append("street_name = %s")
        params.append(street_name)
    if recommended_action:
        conditions.append("recommended_action = %s")
        params.append(recommended_action)
    if q.strip():
        like_q = f"%{q.strip()}%"
        conditions.append("(license_name LIKE %s OR core_trade_name LIKE %s OR address LIKE %s OR phone LIKE %s)")
        params.extend([like_q, like_q, like_q, like_q])

    where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"SELECT * FROM leads {where_sql} ORDER BY CASE grade WHEN 'S' THEN 1 WHEN 'A' THEN 2 WHEN 'B' THEN 3 ELSE 4 END, id DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    with get_db() as db:
        rows = db.fetchall(sql, tuple(params))

    for r in rows:
        r["jump_urls"] = build_quick_jump_urls(r)

    return {"leads": rows, "count": len(rows)}


@app.post("/api/leads/import")
async def upload_leads(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(get_current_user),
):
    file_bytes = await file.read()
    with get_db() as db:
        cfg = db.fetchone("SELECT setting_value FROM system_settings WHERE setting_key = 'default_city'")
        default_city = cfg["setting_value"] if cfg else ""
    try:
        summary = import_leads_file(
            file_bytes=file_bytes,
            filename=file.filename or "upload.xlsx",
            uploaded_by=user["id"],
            default_city=default_city,
        )
        return {"ok": True, "summary": summary}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/leads/{lead_id}/claim")
def claim_lead(lead_id: int, user: Dict[str, Any] = Depends(get_current_user)):
    with get_db() as db:
        lead = db.fetchone("SELECT id, claim_user_id, claim_user_name FROM leads WHERE id = %s", (lead_id,))
        if not lead:
            raise HTTPException(status_code=404, detail="线索不存在")
        if lead["claim_user_id"] is not None:
            if lead["claim_user_id"] == user["id"]:
                return {"ok": True, "message": "你已认领该线索"}
            raise HTTPException(status_code=409, detail=f"该线索已被 {lead['claim_user_name']} 认领，防止撞单")
        db.execute(
            "UPDATE leads SET claim_user_id = %s, claim_user_name = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND claim_user_id IS NULL",
            (user["id"], user["display_name"], lead_id),
        )
    return {"ok": True, "claim_user_name": user["display_name"]}


@app.post("/api/leads/{lead_id}/release")
def release_lead(lead_id: int, user: Dict[str, Any] = Depends(get_current_user)):
    with get_db() as db:
        lead = db.fetchone("SELECT id, claim_user_id FROM leads WHERE id = %s", (lead_id,))
        if not lead:
            raise HTTPException(status_code=404, detail="线索不存在")
        if lead["claim_user_id"] != user["id"] and user["role"] != "admin":
            raise HTTPException(status_code=403, detail="仅认领人本人或管理员可退回公海池")
        db.execute(
            "UPDATE leads SET claim_user_id = NULL, claim_user_name = '', updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (lead_id,),
        )
    return {"ok": True}


@app.post("/api/leads/{lead_id}/assign")
def assign_lead(lead_id: int, req: AssignRequest, admin: Dict[str, Any] = Depends(require_admin)):
    with get_db() as db:
        target = db.fetchone("SELECT id, display_name FROM users WHERE id = %s AND is_active = 1", (req.user_id,))
        if not target:
            raise HTTPException(status_code=404, detail="目标销售账号不存在")
        db.execute(
            "UPDATE leads SET claim_user_id = %s, claim_user_name = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (target["id"], target["display_name"], lead_id),
        )
    return {"ok": True, "claim_user_name": target["display_name"]}


@app.post("/api/leads/{lead_id}/followup")
def add_followup(lead_id: int, req: FollowupRequest, user: Dict[str, Any] = Depends(get_current_user)):
    valid_statuses = ("待联系", "电话未接", "已加微信", "待线下扫街", "已签约", "无意向")
    if req.follow_status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"无效状态，可选: {valid_statuses}")
    with get_db() as db:
        lead = db.fetchone("SELECT id FROM leads WHERE id = %s", (lead_id,))
        if not lead:
            raise HTTPException(status_code=404, detail="线索不存在")
        db.execute(
            "UPDATE leads SET follow_status = %s, last_note = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (req.follow_status, req.note.strip(), lead_id),
        )
        db.execute(
            "INSERT INTO follow_up_logs (lead_id, user_id, user_name, status_after, note) VALUES (%s, %s, %s, %s, %s)",
            (lead_id, user["id"], user["display_name"], req.follow_status, req.note.strip()),
        )
    return {"ok": True}


@app.get("/api/leads/{lead_id}/logs")
def get_lead_logs(lead_id: int, user: Dict[str, Any] = Depends(get_current_user)):
    with get_db() as db:
        logs = db.fetchall(
            "SELECT * FROM follow_up_logs WHERE lead_id = %s ORDER BY id DESC LIMIT 50",
            (lead_id,),
        )
    return {"logs": logs}


@app.post("/api/leads/{lead_id}/manual-verify")
def manual_verify(lead_id: int, req: ManualVerifyRequest, user: Dict[str, Any] = Depends(get_current_user)):
    with get_db() as db:
        lead = db.fetchone("SELECT * FROM leads WHERE id = %s", (lead_id,))
        if not lead:
            raise HTTPException(status_code=404, detail="线索不存在")
        grading = compute_lead_grade(
            amap_poi_found=lead["amap_poi_found"],
            douyin_status=req.douyin_status,
            meituan_status=req.meituan_status,
        )
        pitch = generate_pitch_script(
            store_name=lead.get("core_trade_name") or lead["license_name"],
            store_track=lead["store_track"],
            grade=grading["grade"],
            street_name=lead.get("street_name", ""),
            is_agency_phone=bool(lead.get("is_agency_phone")),
        )
        db.execute(
            """
            UPDATE leads
            SET douyin_status = %s, meituan_status = %s, doubao_status = %s, grade = %s, pitch_script = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (req.douyin_status, req.meituan_status, grading["doubao_status"], grading["grade"], pitch, lead_id),
        )
    return {"ok": True, "grade": grading["grade"], "doubao_status": grading["doubao_status"]}


@app.post("/api/verify/run")
def trigger_verification(
    limit: int = Query(default=30, ge=1, le=200),
    use_playwright: Optional[bool] = Query(default=None),
    user: Dict[str, Any] = Depends(get_current_user),
):
    result = run_verification_batch(limit=limit, use_playwright=use_playwright)
    return {"ok": True, "result": result}


@app.get("/api/settings")
def get_settings(admin: Dict[str, Any] = Depends(require_admin)):
    with get_db() as db:
        rows = db.fetchall("SELECT setting_key, setting_value FROM system_settings")
    return {"settings": {r["setting_key"]: r["setting_value"] for r in rows}}


@app.put("/api/settings")
def update_settings(req: SettingsUpdateRequest, admin: Dict[str, Any] = Depends(require_admin)):
    updates = {}
    if req.amap_api_key is not None:
        updates["amap_api_key"] = req.amap_api_key.strip()
    if req.default_city is not None:
        updates["default_city"] = req.default_city.strip()
    if req.enable_playwright is not None:
        updates["enable_playwright"] = req.enable_playwright.strip().lower()

    with get_db() as db:
        for k, v in updates.items():
            existing = db.fetchone("SELECT setting_key FROM system_settings WHERE setting_key = %s", (k,))
            if existing:
                db.execute(
                    "UPDATE system_settings SET setting_value = %s, updated_at = CURRENT_TIMESTAMP WHERE setting_key = %s",
                    (v, k),
                )
            else:
                db.execute(
                    "INSERT INTO system_settings (setting_key, setting_value) VALUES (%s, %s)",
                    (k, v),
                )
    return {"ok": True}
