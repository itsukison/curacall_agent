import asyncio
import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli
from livekit.agents.voice.agent_session import (
    ConversationItemAddedEvent,
    CloseEvent,
)
from livekit.plugins import deepgram, google, silero

from prompts import build_system_prompt
from tools import (
    check_availability,
    book_appointment,
    transfer_to_human,
    update_collected_data,
    identify_patient,
    http_client,
)


async def _create_conversation(clinic_id: str) -> str | None:
    """Create a conversation record via the Next.js API."""
    try:
        resp = await http_client.post(
            "/api/conversations",
            json={"channel": "voice", "clinic_id": clinic_id},
        )
        if resp.status_code == 200:
            return resp.json().get("conversation_id")
    except Exception as e:
        print(f"[agent] Failed to create conversation: {e}")
    return None


async def entrypoint(ctx: JobContext):
    await ctx.connect()

    # Read clinic context from room metadata (set by /api/livekit-token via RoomServiceClient)
    metadata = json.loads(ctx.room.metadata or "{}")
    clinic_id = metadata.get("clinic_id", "")
    clinic_name = metadata.get("clinic_name", "クリニック")
    staff = metadata.get("staff", [])
    treatments = metadata.get("treatments", [])
    rules = metadata.get("rules", {})
    llm_model = metadata.get("llm_model", "gemini-2.5-flash")
    voice_name = metadata.get("voice_name", "ja-JP-Chirp3-HD-Aoede")

    greeting_message = rules.get("greeting_message") or f"お電話ありがとうございます。{clinic_name}、AI予約受付センターです。"
    is_private_only = rules.get("is_private_only", False)

    system_prompt = build_system_prompt(clinic_name, staff, treatments, rules)

    # Fire conversation creation in background — don't block greeting
    conv_task = asyncio.create_task(_create_conversation(clinic_id))
    conversation_id: str | None = None

    session = AgentSession(
        vad=silero.VAD.load(
            min_silence_duration=0.5,
            prefix_padding_duration=0.3,
        ),
        stt=deepgram.STT(
            model="nova-3",
            language="ja",
            interim_results=True,
            endpointing_ms=300,
        ),
        llm=google.LLM(model=llm_model),
        tts=google.TTS(
            model_name="chirp_3",
            voice_name=voice_name,
            language="ja-JP",
        ),
        turn_handling={
            "endpointing": {"min_delay": 0.3, "max_delay": 1.5},
        },
    )

    session.userdata = {
        "clinic_id": clinic_id,
        "conversation_id": None,  # Will be set once conv_task resolves
        "room": ctx.room,
    }

    # ── Transcript saving ──────────────────────────────────────────
    async def _append_turn(role: str, text: str) -> None:
        """Send a single transcript turn to the Next.js API."""
        if not conversation_id or not text:
            return
        try:
            await http_client.patch(
                f"/api/conversations/{conversation_id}/transcript",
                json={
                    "clinic_id": clinic_id,
                    "turns": [
                        {"role": role, "text": text, "ts": datetime.now(timezone.utc).isoformat()}
                    ],
                },
            )
        except Exception as e:
            print(f"[agent] Failed to append transcript turn: {e}")

    @session.on("conversation_item_added")
    def _on_item(ev: ConversationItemAddedEvent) -> None:
        item = ev.item
        if item.role not in ("user", "assistant"):
            return
        text = item.text_content
        if not text:
            return
        asyncio.ensure_future(_append_turn(item.role, text))

    @session.on("close")
    def _on_close(ev: CloseEvent) -> None:
        async def _mark_ended() -> None:
            if not conversation_id:
                return
            try:
                await http_client.patch(
                    f"/api/conversations/{conversation_id}/end",
                    json={"clinic_id": clinic_id},
                )
            except Exception as e:
                print(f"[agent] Failed to mark conversation ended: {e}")
        asyncio.ensure_future(_mark_ended())
    # ───────────────────────────────────────────────────────────────

    await session.start(
        room=ctx.room,
        agent=Agent(
            instructions=system_prompt,
            tools=[
                check_availability,
                book_appointment,
                transfer_to_human,
                update_collected_data,
                identify_patient,
            ],
        ),
    )

    # Build greeting text directly — no LLM round-trip needed for a fixed string
    greeting_text = greeting_message
    if is_private_only:
        greeting_text += " 当院は自費診療のみのクリニックです。ご了承いただける方は、このままご希望をお話しください。"
    else:
        greeting_text += " 今日はどのようなご用件でしょうか？"

    # Agent speaks first via TTS directly (skips LLM, saves 2-4s)
    await session.say(greeting_text)

    # Resolve conversation_id now (should be done by this point)
    conversation_id = await conv_task
    session.userdata["conversation_id"] = conversation_id


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
