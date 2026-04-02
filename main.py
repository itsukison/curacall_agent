import json
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli
from livekit.plugins import openai, google, silero

from prompts import build_system_prompt
from tools import (
    check_availability,
    book_appointment,
    transfer_to_human,
    update_collected_data,
    NEXT_APP_URL,
    _headers,
)


async def _create_conversation(clinic_id: str) -> str | None:
    """Create a conversation record via the Next.js API."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{NEXT_APP_URL}/api/conversations",
                json={"channel": "voice", "clinic_id": clinic_id},
                headers=_headers(),
                timeout=10.0,
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
    doctors = metadata.get("doctors", [])
    symptoms = metadata.get("symptoms", [])

    system_prompt = build_system_prompt(clinic_name, doctors, symptoms)
    conversation_id = await _create_conversation(clinic_id)

    session = AgentSession(
        vad=silero.VAD.load(
            min_silence_duration=0.8,
            prefix_padding_duration=0.5,
        ),
        stt=openai.STT(
            model="gpt-4o-mini-transcribe",
            language="ja",
        ),
        llm=google.LLM(model="gemini-2.5-flash"),
        tts=google.TTS(
            model_name="chirp_3",
            voice_name="ja-JP-Chirp3-HD-Aoede",
            language="ja-JP",
        ),
    )

    session.userdata = {
        "clinic_id": clinic_id,
        "conversation_id": conversation_id,
        "room": ctx.room,
    }

    await session.start(
        room=ctx.room,
        agent=Agent(
            instructions=system_prompt,
            tools=[
                check_availability,
                book_appointment,
                transfer_to_human,
                update_collected_data,
            ],
        ),
    )

    # Agent speaks first with a warm greeting
    await session.generate_reply(
        instructions="患者さんに温かく挨拶して、今日はどのようなご用件かを優しく聞いてください。"
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
