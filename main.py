"""
GhanaHotspot Backend — Screenshot Verification Edition
UZU-HOSTEL · Elshadai Impact Technologies

Flow:
  1. Customer picks package → sees hostel MoMo number + exact amount to send
  2. Customer sends MoMo, screenshots the success confirmation
  3. POST /api/verify-screenshot  → Claude Vision reads amount/ref/date/recipient/status
  4. Server checks: amount correct? date fresh? reference never used? sent to hostel?
  5. All pass → voucher assigned → MikroTik user created → login code returned instantly
"""

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
import logging
from datetime import datetime, timedelta

from database import get_db, SessionLocal
from models import Transaction, Voucher, HotspotUser, AuditLog
from mikrotik import MikroTikAPI
from config import settings
from verify_screenshot import extract_payment_details, verify_payment
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="GhanaHotspot API — UZU-HOSTEL", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET","POST"], allow_headers=["*"])

redis_client = None

@app.on_event("startup")
async def startup():
    global redis_client
    redis_client = aioredis.from_url(settings.REDIS_URL)

@app.on_event("shutdown")
async def shutdown():
    await redis_client.close()


PACKAGES = {
    "3gb":       {"label":"3GB — 7 Days",       "data_mb":3072,  "price":5.00,  "validity_days":7,  "mikrotik_profile":"3gb-7day"},
    "6gb":       {"label":"6GB — 14 Days",       "data_mb":6144,  "price":9.00,  "validity_days":14, "mikrotik_profile":"6gb-14day"},
    "unlimited": {"label":"Unlimited — 30 Days", "data_mb":None,  "price":40.00, "validity_days":30, "mikrotik_profile":"unlimited-30day"},
}

MAX_RECEIPT_AGE_MINUTES = 120   # reject receipts older than 2 hours


async def check_rate_limit(phone: str):
    key   = f"ratelimit:screenshot:{phone}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, 600)
    if count > 5:
        ttl = await redis_client.ttl(key)
        raise HTTPException(status_code=429, detail=f"Too many attempts. Try again in {ttl//60}m {ttl%60}s.")


@app.get("/api/payment-info")
async def payment_info(package_id: str):
    if package_id not in PACKAGES:
        raise HTTPException(status_code=400, detail="Invalid package")
    pkg = PACKAGES[package_id]
    return {
        "package":        pkg["label"],
        "amount":         pkg["price"],
        "send_to_name":   settings.HOSTEL_MOMO_NAME,
        "send_to_number": settings.HOSTEL_MOMO_NUMBER,
    }


@app.post("/api/verify-screenshot")
async def verify_screenshot_endpoint(
    request:          Request,
    background_tasks: BackgroundTasks,
    phone:            str        = Form(...),
    package_id:       str        = Form(...),
    screenshot:       UploadFile = File(...),
    db: SessionLocal = Depends(get_db),
):
    phone = phone.strip().replace(" ", "")
    if not phone.startswith("0") or len(phone) != 10 or not phone.isdigit():
        raise HTTPException(status_code=400, detail="Invalid phone number. Format: 0XXXXXXXXX")
    if package_id not in PACKAGES:
        raise HTTPException(status_code=400, detail=f"Invalid package. Choose: {list(PACKAGES.keys())}")

    await check_rate_limit(phone)

    image_bytes = await screenshot.read()
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Screenshot too large (max 10 MB).")

    ctype = screenshot.content_type or "image/jpeg"
    if ctype not in ("image/jpeg","image/jpg","image/png","image/webp"):
        ctype = "image/png" if image_bytes[:4] == b'\x89PNG' else "image/jpeg"

    package = PACKAGES[package_id]
    logger.info(f"Screenshot upload: phone={phone} pkg={package_id} size={len(image_bytes)}")

    # 1. Claude Vision reads the screenshot
    extracted = await extract_payment_details(
        image_bytes=image_bytes,
        image_mimetype=ctype,
        anthropic_api_key=settings.ANTHROPIC_API_KEY,
    )
    logger.info(f"Extracted: {extracted}")

    # 2. Business rule checks
    check = verify_payment(
        extracted=extracted,
        expected_amount=package["price"],
        hostel_momo_name=settings.HOSTEL_MOMO_NAME,
        max_age_minutes=MAX_RECEIPT_AGE_MINUTES,
    )
    if not check["ok"]:
        db.add(AuditLog(event="screenshot_rejected", reference=extracted.get("reference"),
            ip=request.client.host,
            details=f"Phone:{phone} Pkg:{package_id} Reason:{check['reason']}"))
        db.commit()
        raise HTTPException(status_code=422, detail=check["reason"])

    # 3. Reference must be unique — prevents reusing same receipt
    ref_on_receipt = extracted["reference"]
    if db.query(Transaction).filter(Transaction.reference == ref_on_receipt).first():
        raise HTTPException(status_code=409,
            detail=f"Receipt ref {ref_on_receipt} has already been used. Each payment can only be used once.")

    # 4. Claim a voucher (row-level lock prevents double-assignment)
    voucher = (
        db.query(Voucher)
        .filter(Voucher.package_id==package_id, Voucher.assigned_to_ref==None, Voucher.expired==False)
        .with_for_update(skip_locked=True)
        .first()
    )
    if not voucher:
        raise HTTPException(status_code=409, detail="This package is sold out. Please contact management.")

    # 5. Save transaction — MoMo reference IS the transaction ID
    transaction = Transaction(
        reference=ref_on_receipt, phone=phone,
        network=extracted.get("network") or "unknown",
        package_id=package_id, amount=extracted["amount"],
        mac_address=None, status="success",
        ip_address=request.client.host, created_at=datetime.utcnow(),
    )
    db.add(transaction)
    db.flush()

    # 6. Assign voucher
    voucher.assigned_to_ref = ref_on_receipt
    voucher.assigned_at     = datetime.utcnow()
    voucher.assigned_phone  = phone
    db.commit()

    logger.info(f"Voucher {voucher.username}/{voucher.pin} -> {phone} (ref {ref_on_receipt})")

    # 7. MikroTik in background
    background_tasks.add_task(provision_mikrotik, voucher=voucher, transaction=transaction, package=package, db=db)

    # 8. Audit
    db.add(AuditLog(event="screenshot_approved", reference=ref_on_receipt, ip=request.client.host,
        details=f"Phone:{phone} Pkg:{package_id} Amount:{extracted['amount']} Voucher:{voucher.username} Date:{extracted.get('date_str')}"))
    db.commit()

    expiry = (datetime.utcnow() + timedelta(days=package["validity_days"])).strftime("%d %b %Y")

    return {
        "status":       "success",
        "voucher_code": {"username": voucher.username, "pin": voucher.pin},
        "package":      package["label"],
        "expiry":       expiry,
        "phone":        phone,
        "receipt": {
            "reference": ref_on_receipt,
            "amount":    extracted["amount"],
            "date":      extracted.get("date_str"),
            "network":   extracted.get("network"),
        },
    }


async def provision_mikrotik(voucher, transaction, package, db):
    mikrotik  = MikroTikAPI(host=settings.MIKROTIK_HOST, username=settings.MIKROTIK_USER, password=settings.MIKROTIK_PASS)
    expiry_dt = datetime.utcnow() + timedelta(days=package["validity_days"])
    try:
        await mikrotik.create_hotspot_user(
            username=voucher.username, password=voucher.pin, mac_address="",
            profile=package["mikrotik_profile"],
            comment=f"Phone:{transaction.phone} Ref:{transaction.reference}",
            limit_uptime=f"{package['validity_days']}d",
            limit_bytes_total=package["data_mb"]*1024*1024 if package["data_mb"] else None,
        )
        voucher.provisioned = True
        db.add(HotspotUser(username=voucher.username, mac_address=None, phone=transaction.phone,
            package_id=transaction.package_id, transaction_ref=transaction.reference,
            is_active=True, expires_at=expiry_dt))
        db.commit()
        logger.info(f"MikroTik user provisioned: {voucher.username}")
    except Exception as e:
        logger.error(f"MikroTik provision FAILED for {transaction.reference}: {e}")


@app.post("/internal/run-expiry-check")
async def run_expiry_check(db: SessionLocal = Depends(get_db)):
    now = datetime.utcnow()
    expired = db.query(HotspotUser).filter(HotspotUser.expires_at < now, HotspotUser.is_active==True).all()
    mikrotik = MikroTikAPI(host=settings.MIKROTIK_HOST, username=settings.MIKROTIK_USER, password=settings.MIKROTIK_PASS)
    count = 0
    for user in expired:
        try:
            await mikrotik.delete_hotspot_user(user.username)
            user.is_active = False
            v = db.query(Voucher).filter(Voucher.username==user.username).first()
            if v: v.expired = True
            count += 1
        except Exception as e:
            logger.error(f"Expiry failed {user.username}: {e}")
    db.commit()
    return {"expired_count": count}

@app.get("/internal/voucher-stock")
async def voucher_stock(db: SessionLocal = Depends(get_db)):
    total     = db.query(Voucher).count()
    available = db.query(Voucher).filter(Voucher.assigned_to_ref==None, Voucher.expired==False).count()
    return {"total": total, "available": available, "used": total-available}
