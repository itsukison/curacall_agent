import json
import os

import httpx
from livekit.agents import function_tool, RunContext

NEXT_APP_URL = os.environ.get("NEXT_APP_URL", "http://localhost:3000")
AGENT_INTERNAL_KEY = os.environ.get("AGENT_INTERNAL_KEY", "")


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


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {AGENT_INTERNAL_KEY}",
        "Content-Type": "application/json",
    }


def _clinic_id(ctx: RunContext) -> str:
    return ctx.session.userdata.get("clinic_id", "")


def _conversation_id(ctx: RunContext) -> str | None:
    return ctx.session.userdata.get("conversation_id")


@function_tool()
async def update_collected_data(
    ctx: RunContext,
    patient_name: str | None = None,
    patient_phone: str | None = None,
    symptom_id: str | None = None,
    suggested_slot: str | None = None,
    suggested_slot_iso: str | None = None,
) -> dict:
    """患者情報が収集できた時点でUIパネルを更新するために呼び出す。"""
    payload = {}
    if patient_name is not None:
        payload["patientName"] = patient_name
    if patient_phone is not None:
        payload["patientPhone"] = patient_phone
    if symptom_id is not None:
        payload["symptomId"] = symptom_id
    if suggested_slot is not None:
        payload["suggestedSlot"] = suggested_slot
    if suggested_slot_iso is not None:
        payload["suggestedSlotISO"] = suggested_slot_iso

    await _send_data(ctx.session, "collected_data_update", payload)
    return {"result": "UI updated"}


@function_tool()
async def check_availability(
    ctx: RunContext,
    symptom_id: str,
    preferred_date: str | None = None,
) -> dict:
    """症状に対応した空き予約枠を検索します。symptom_idは症状一覧から選んでください。preferred_dateはYYYY-MM-DD形式。"""
    await _send_data(ctx.session, "tool_status", {"status": "空き枠を検索中..."})

    params: dict[str, str] = {
        "symptomId": symptom_id,
        "clinicId": _clinic_id(ctx),
    }
    if preferred_date:
        params["preferredDate"] = preferred_date

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{NEXT_APP_URL}/api/tools/check_availability",
                params=params,
                headers=_headers(),
                timeout=15.0,
            )
        data = resp.json()
    except Exception as e:
        data = {"error": str(e), "no_slots": True}

    # Send periods to frontend
    if "periods" in data:
        await _send_data(ctx.session, "availability_update", {"periods": data["periods"]})
    # Update collected data with symptom_id
    await _send_data(ctx.session, "collected_data_update", {"symptomId": symptom_id})
    await _send_data(ctx.session, "tool_status", {"status": None})
    return data


@function_tool()
async def book_appointment(
    ctx: RunContext,
    patient_name: str,
    patient_phone: str,
    symptom_id: str,
    start_time: str,
) -> dict:
    """予約を確定してDBに登録します。患者が「はい」と確認した後にのみ呼び出してください。doctor_idは不要です。"""
    await _send_data(ctx.session, "tool_status", {"status": "予約を登録中..."})

    payload = {
        "patient_name": patient_name,
        "patient_phone": patient_phone,
        "symptom_id": symptom_id,
        "start_time": start_time,
        "conversation_id": _conversation_id(ctx),
        "clinic_id": _clinic_id(ctx),
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{NEXT_APP_URL}/api/tools/book_appointment",
                json=payload,
                headers=_headers(),
                timeout=15.0,
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
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{NEXT_APP_URL}/api/tools/transfer_to_human",
                json=payload,
                headers=_headers(),
                timeout=15.0,
            )
        data = resp.json()
    except Exception as e:
        data = {"error": str(e)}

    await _send_data(ctx.session, "transferred", {})
    await _send_data(ctx.session, "tool_status", {"status": None})
    return data
