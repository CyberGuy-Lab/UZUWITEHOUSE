"""
verify_screenshot.py
════════════════════
Uses Claude Vision (claude-sonnet-4-20250514) to read a MoMo payment
screenshot uploaded by a customer and extract:
  - amount_paid   (float)
  - reference     (string — unique transaction ID on the receipt)
  - date_str      (string — date/time on the receipt)
  - recipient     (string — who was paid)
  - network       (string — MTN / Telecel / AirtelTigo)
  - status        (string — "successful" / "failed" / unknown)

Returns a structured dict so the FastAPI endpoint can decide
whether to assign a voucher or reject the submission.
"""

import base64
import json
import httpx
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import re

logger = logging.getLogger(__name__)

# Anthropic API
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL      = "claude-sonnet-4-20250514"

# ──────────────────────────────────────────────────────────
# MAIN FUNCTION
# ──────────────────────────────────────────────────────────

async def extract_payment_details(
    image_bytes:   bytes,
    image_mimetype: str,          # "image/jpeg" | "image/png" | "image/webp"
    anthropic_api_key: str,
) -> dict:
    """
    Send the screenshot to Claude and extract payment details.
    Returns a dict:
    {
        "success":       bool,
        "amount":        float | None,
        "reference":     str | None,
        "date_str":      str | None,
        "recipient":     str | None,
        "network":       str | None,
        "status":        str | None,   # "successful" | "failed"
        "raw_text":      str | None,
        "error":         str | None,
    }
    """
    b64_image = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = """You are a payment-receipt reader for a Ghanaian WiFi hotspot business.

The user has uploaded a MoMo (Mobile Money) payment screenshot from their phone.

Your job is to extract EXACTLY the following fields from the screenshot:

1. amount_paid   — the amount of money sent (numeric, e.g. 5.00)
2. reference     — the unique transaction / reference / receipt ID shown on the screen
3. date_str      — the full date and time shown (as it appears, e.g. "18/03/2026 14:32")
4. recipient     — the name or number of who received the money
5. network       — the mobile network (MTN, Telecel, AirtelTigo, or Vodafone)
6. status        — whether the transaction was "successful" or "failed" (look for words like Successful, Completed, Failed, Pending)

Reply with ONLY a valid JSON object — no markdown, no explanation, no extra text. Example:
{
  "amount_paid": 5.00,
  "reference": "GHA-4521889023",
  "date_str": "18/03/2026 14:32",
  "recipient": "0241234567 / Kwame Asante",
  "network": "MTN",
  "status": "successful"
}

If you cannot read a field clearly, set it to null.
If the image is NOT a payment receipt, set all fields to null and add "error": "Not a payment receipt"."""

    headers = {
        "x-api-key":         anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }

    body = {
        "model":      CLAUDE_MODEL,
        "max_tokens": 512,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type":   "image",
                        "source": {
                            "type":       "base64",
                            "media_type": image_mimetype,
                            "data":       b64_image,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(ANTHROPIC_API_URL, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        raw_text = data["content"][0]["text"].strip()
        logger.info(f"Claude receipt response: {raw_text}")

        # Strip markdown fences if present
        clean = raw_text.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```[a-z]*\n?", "", clean)
            clean = re.sub(r"\n?```$", "", clean)

        parsed = json.loads(clean)

        return {
            "success":   True,
            "amount":    _to_float(parsed.get("amount_paid")),
            "reference": _clean_str(parsed.get("reference")),
            "date_str":  _clean_str(parsed.get("date_str")),
            "recipient": _clean_str(parsed.get("recipient")),
            "network":   _clean_str(parsed.get("network")),
            "status":    _clean_str(parsed.get("status")),
            "raw_text":  raw_text,
            "error":     parsed.get("error"),
        }

    except Exception as e:
        logger.error(f"Screenshot extraction failed: {e}")
        return {
            "success": False,
            "amount": None, "reference": None, "date_str": None,
            "recipient": None, "network": None, "status": None,
            "raw_text": None, "error": str(e),
        }


# ──────────────────────────────────────────────────────────
# VERIFICATION RULES
# ──────────────────────────────────────────────────────────

def verify_payment(
    extracted:        dict,
    expected_amount:  float,
    hostel_momo_name: str,   # name/number on your MoMo account
    max_age_minutes:  int = 60,
) -> dict:
    """
    Applies business rules to the extracted receipt data.
    Returns { "ok": bool, "reason": str }
    """
    if not extracted.get("success"):
        return {"ok": False, "reason": "Could not read the screenshot. Please upload a clearer image."}

    if extracted.get("error"):
        return {"ok": False, "reason": "The image doesn't look like a payment receipt. Please upload your MoMo confirmation screenshot."}

    # 1. Must be a successful transaction
    status = (extracted.get("status") or "").lower()
    if status not in ("successful", "success", "completed", "approved"):
        return {"ok": False, "reason": f"The receipt shows status '{extracted.get('status')}'. Only successful payments are accepted."}

    # 2. Amount must match
    paid = extracted.get("amount")
    if paid is None:
        return {"ok": False, "reason": "Could not read the payment amount from the screenshot."}

    # Allow ±0.01 for floating point
    if abs(paid - expected_amount) > 0.01:
        return {
            "ok": False,
            "reason": (
                f"Amount on receipt is GH₵ {paid:.2f} but "
                f"GH₵ {expected_amount:.2f} is required for this package. "
                f"Please send the exact amount."
            ),
        }

    # 3. Reference must be present (to prevent reuse — checked in DB separately)
    if not extracted.get("reference"):
        return {"ok": False, "reason": "Could not read the transaction reference number. Please make sure the full receipt is visible."}

    # 4. Date must be recent (within max_age_minutes)
    date_str = extracted.get("date_str")
    if date_str:
        parsed_date = _parse_date(date_str)
        if parsed_date:
            age = datetime.now(timezone.utc) - parsed_date.replace(tzinfo=timezone.utc)
            if age.total_seconds() < 0:
                return {"ok": False, "reason": "The receipt date is in the future. Please upload today's payment receipt."}
            if age.total_seconds() > max_age_minutes * 60:
                mins = int(age.total_seconds() // 60)
                return {
                    "ok": False,
                    "reason": (
                        f"This receipt is {mins} minutes old. "
                        f"Only receipts from the last {max_age_minutes} minutes are accepted. "
                        f"Please make a new payment."
                    ),
                }

    # 5. Recipient should match the hostel number/name (soft check — warn but don't block if can't confirm)
    recipient = (extracted.get("recipient") or "").lower()
    hostel_lower = hostel_momo_name.lower()
    if recipient and hostel_lower not in recipient:
        # Check if any part of the hostel name is in recipient
        hostel_parts = hostel_lower.split()
        match = any(part in recipient for part in hostel_parts if len(part) > 3)
        if not match:
            return {
                "ok": False,
                "reason": (
                    f"The payment appears to have been sent to '{extracted.get('recipient')}' "
                    f"instead of the hostel account. "
                    f"Please send to {hostel_momo_name} and upload that receipt."
                ),
            }

    return {"ok": True, "reason": "Payment verified"}


# ──────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────

def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("GH₵", "").replace("GHS", "").strip())
    except Exception:
        return None


def _clean_str(v) -> Optional[str]:
    if v is None:
        return None
    return str(v).strip() or None


def _parse_date(date_str: str) -> Optional[datetime]:
    """Try several common Ghanaian MoMo receipt date formats."""
    formats = [
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%d %b %Y %H:%M",
        "%d %B %Y %H:%M",
        "%b %d, %Y %H:%M",
        "%d/%m/%Y",
        "%d-%m-%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None
