import json
import os
import re
import unicodedata

import httpx
from livekit.agents import function_tool, RunContext

NEXT_APP_URL = os.environ.get("NEXT_APP_URL", "http://localhost:3000")
AGENT_INTERNAL_KEY = os.environ.get("AGENT_INTERNAL_KEY", "")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {AGENT_INTERNAL_KEY}",
        "Content-Type": "application/json",
    }


# Persistent HTTP client — reuses TCP connections across all tool calls
http_client = httpx.AsyncClient(
    base_url=NEXT_APP_URL,
    headers=_headers(),
    timeout=15.0,
)


def normalize_phone(raw: str) -> str:
    """Full-width → half-width, then strip non-digits."""
    half = unicodedata.normalize("NFKC", raw)
    return re.sub(r"\D", "", half)


async def _send_data(session, msg_type: str, payload: dict) -> None:
    """Publish a JSON message to the LiveKit room data channel."""
    room = session.userdata.get("room")
    if not room:
        return
    message = json.dumps({"type": msg_type, "data": payload})
    await room.local_participant.publish_data(
        message.encode("utf-8"),
        reliable=True,
    )


def _clinic_id(ctx: RunContext) -> str:
    return ctx.session.userdata.get("clinic_id", "")


def _conversation_id(ctx: RunContext) -> str | None:
    return ctx.session.userdata.get("conversation_id")


@function_tool()
async def update_collected_data(
    ctx: RunContext,
    patient_name: str | None = None,
    patient_phone: str | None = None,
    treatment_id: str | None = None,
    suggested_slot: str | None = None,
    suggested_slot_iso: str | None = None,
) -> dict:
    """患者情報が収集できた時点でUIパネルを更新するために呼び出す。"""
    payload = {}
    if patient_name is not None:
        payload["patientName"] = patient_name
    if patient_phone is not None:
        payload["patientPhone"] = patient_phone
    if treatment_id is not None:
        payload["treatmentId"] = treatment_id
    if suggested_slot is not None:
        payload["suggestedSlot"] = suggested_slot
    if suggested_slot_iso is not None:
        payload["suggestedSlotISO"] = suggested_slot_iso

    await _send_data(ctx.session, "collected_data_update", payload)
    return {"result": "UI updated"}


@function_tool()
async def check_availability(
    ctx: RunContext,
    treatment_id: str,
    preferred_date: str | None = None,
    preferred_hour: int | None = None,
) -> dict:
    """治療メニューに対応した空き予約枠を検索します。treatment_idは治療メニュー一覧から選んでください。preferred_dateはYYYY-MM-DD形式。preferred_hourは患者の希望時間帯（0〜23の整数）。"""
    await _send_data(ctx.session, "tool_status", {"status": "空き枠を検索中..."})

    params: dict[str, str] = {
        "treatmentId": treatment_id,
        "clinicId": _clinic_id(ctx),
    }
    if preferred_date:
        params["preferredDate"] = preferred_date
    if preferred_hour is not None:
        params["preferredHour"] = str(preferred_hour)

    try:
        resp = await http_client.get(
                "/api/tools/check_availability",
                params=params,
            )
        data = resp.json()
    except Exception as e:
        data = {"error": str(e), "no_slots": True}

    # Send periods to frontend + cache valid slot ISOs in session
    if "periods" in data:
        await _send_data(ctx.session, "availability_update", {"periods": data["periods"]})
        # Store all valid slot_isos so book_appointment can validate against them
        valid_isos: set[str] = ctx.session.userdata.get("valid_slot_isos") or set()
        for period in data["periods"]:
            for iso in period.get("slot_isos", []):
                valid_isos.add(iso)
        ctx.session.userdata["valid_slot_isos"] = valid_isos
    # Update collected data with treatment_id
    await _send_data(ctx.session, "collected_data_update", {"treatmentId": treatment_id})
    await _send_data(ctx.session, "tool_status", {"status": None})
    return data


@function_tool()
async def book_appointment(
    ctx: RunContext,
    patient_name: str,
    patient_phone: str,
    treatment_id: str,
    start_time: str,
) -> dict:
    """予約を確定してDBに登録します。患者が「はい」と確認した後にのみ呼び出してください。"""
    # Validate that start_time is one the system actually offered
    valid_isos: set[str] = ctx.session.userdata.get("valid_slot_isos") or set()
    if valid_isos and start_time not in valid_isos:
        return {
            "error": "指定された時刻はシステムが提示した候補に含まれていません。check_availabilityのslot_isosから正確にコピーしてください。",
            "valid_slot_isos_sample": sorted(valid_isos)[:10],
        }

    await _send_data(ctx.session, "tool_status", {"status": "予約を登録中..."})

    payload = {
        "patient_name": patient_name,
        "patient_phone": patient_phone,
        "treatment_id": treatment_id,
        "start_time": start_time,
        "conversation_id": _conversation_id(ctx),
        "clinic_id": _clinic_id(ctx),
    }

    try:
        resp = await http_client.post(
                "/api/tools/book_appointment",
                json=payload,
            )
        data = resp.json()
    except Exception as e:
        data = {"error": str(e)}

    # Send results to frontend
    if data.get("success"):
        await _send_data(ctx.session, "booking_success", {
            "appointment_id": data.get("appointment_id", ""),
        })
        await _send_data(ctx.session, "collected_data_update", {
            "patientName": patient_name,
            "patientPhone": patient_phone,
            "suggestedSlotISO": start_time,
        })
    if data.get("conflict") and "periods" in data:
        await _send_data(ctx.session, "availability_update", {"periods": data["periods"]})

    await _send_data(ctx.session, "tool_status", {"status": None})

    # Strip appointment_id from LLM response (don't expose UUIDs)
    response = {k: v for k, v in data.items() if k != "appointment_id"}
    return response


@function_tool()
async def transfer_to_human(
    ctx: RunContext,
    reason: str,
) -> dict:
    """AI対応不可、またはスタッフへの転送を求めた場合に呼び出してください。"""
    await _send_data(ctx.session, "tool_status", {"status": "スタッフに接続中..."})

    payload = {
        "reason": reason,
        "conversation_id": _conversation_id(ctx),
        "clinic_id": _clinic_id(ctx),
    }

    try:
        resp = await http_client.post(
                "/api/tools/transfer_to_human",
                json=payload,
            )
        data = resp.json()
    except Exception as e:
        data = {"error": str(e)}

    await _send_data(ctx.session, "transferred", {})
    await _send_data(ctx.session, "tool_status", {"status": None})
    return data


@function_tool()
async def identify_patient(
    ctx: RunContext,
    phone_number: str,
) -> dict:
    """電話番号で患者を照会します。初診・再診どちらの場合でも、予約意向が確認できた時点で呼び出してください。結果は患者に読み上げず、会話の文脈として使ってください。

    返り値 status:
    - "new": DBに該当なし（初診扱い）
    - "returning": 再診患者（patient.full_name, last_appointment, in_progress_treatments, approved_treatmentsを含む）
    - "lapsed": 6ヶ月以上ぶりの再初診
    - "error": 照会失敗（ネットワーク等）。患者に電話番号再確認を促す
    """
    normalized = normalize_phone(phone_number)

    if not normalized:
        return {"status": "error", "message": "電話番号を抽出できませんでした"}

    try:
        resp = await http_client.post(
                "/api/tools/identify_patient",
                json={
                    "clinic_id": _clinic_id(ctx),
                    "phone_number": normalized,
                },
                timeout=3.0,
            )
        data = resp.json()
    except Exception as e:
        print(f"[identify_patient] FAILED phone={normalized} error={type(e).__name__}: {e}")
        return {"status": "error", "message": "患者照会に失敗しました"}

    # Push patient info to dashboard UI immediately for returning/lapsed
    if data.get("status") in ("returning", "lapsed"):
        patient = data.get("patient", {})
        await _send_data(ctx.session, "collected_data_update", {
            "patientName": patient.get("full_name", ""),
            "patientPhone": normalized,
        })
    else:
        # new or error — still surface the phone we captured
        await _send_data(ctx.session, "collected_data_update", {
            "patientPhone": normalized,
        })

    return data
