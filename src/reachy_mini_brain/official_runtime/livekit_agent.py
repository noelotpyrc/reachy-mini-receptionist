"""Minimal LiveKit agent server for replay experiments.

Run this after installing the livekit optional dependencies and setting LiveKit
credentials/model provider configuration. The replay client joins the same room
and publishes a WAV as microphone audio.
"""

from __future__ import annotations

import os
from pathlib import Path

from .env import PROJECT_ROOT, load_project_env


load_project_env()


DEFAULT_INSTRUCTIONS = """You are a professional clinic receptionist.
Answer in short, clear spoken sentences. Do not mention implementation details.
If you are unsure, say so briefly and ask a concise follow-up question."""

DEFAULT_PROFILE_INSTRUCTIONS = PROJECT_ROOT / "profiles" / "clinic_receptionist" / "instructions.txt"


try:
    from livekit import agents
    from livekit.agents import Agent, AgentServer, AgentSession, TurnHandlingOptions, inference
    from livekit.plugins import silero
    from livekit.plugins.turn_detector.multilingual import MultilingualModel
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERROR: Exception | None = exc
    agents = None  # type: ignore[assignment]
    Agent = object  # type: ignore[assignment,misc]
    AgentServer = None  # type: ignore[assignment]
    AgentSession = None  # type: ignore[assignment]
    TurnHandlingOptions = None  # type: ignore[assignment]
    inference = None  # type: ignore[assignment]
    silero = None  # type: ignore[assignment]
    MultilingualModel = None  # type: ignore[assignment]
else:
    _IMPORT_ERROR = None


class ReceptionistAgent(Agent):  # type: ignore[misc,valid-type]
    def __init__(self) -> None:
        super().__init__(instructions=_load_instructions())


server = AgentServer() if AgentServer is not None else None


if server is not None:

    @server.rtc_session(agent_name=os.getenv("LIVEKIT_AGENT_NAME", "reachy-mini-receptionist"))
    async def receptionist(ctx: agents.JobContext) -> None:  # type: ignore[union-attr]
        session = AgentSession(
            stt=inference.STT(
                model=os.getenv("LIVEKIT_STT_MODEL", "deepgram/nova-3"),
                language=os.getenv("LIVEKIT_STT_LANGUAGE", "multi"),
            ),
            llm=inference.LLM(model=os.getenv("LIVEKIT_LLM_MODEL", "openai/gpt-5.2-chat-latest")),
            tts=inference.TTS(
                model=os.getenv("LIVEKIT_TTS_MODEL", "cartesia/sonic-3"),
                voice=os.getenv("LIVEKIT_TTS_VOICE", "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"),
            ),
            vad=silero.VAD.load(),
            turn_handling=TurnHandlingOptions(turn_detection=MultilingualModel()),
        )
        await session.start(room=ctx.room, agent=ReceptionistAgent())
        await ctx.connect()


def main() -> None:
    """Run the LiveKit agent server."""

    if _IMPORT_ERROR is not None or agents is None or server is None:
        raise RuntimeError(
            "LiveKit agent packages are not installed. Install with: "
            ".venv/bin/python -m pip install -e '.[livekit]'"
        ) from _IMPORT_ERROR

    agents.cli.run_app(server)


def _load_instructions() -> str:
    path = os.getenv("LIVEKIT_AGENT_INSTRUCTIONS_FILE", "").strip()
    if not path:
        inline = os.getenv("LIVEKIT_AGENT_INSTRUCTIONS", "").strip()
        if inline:
            return inline
        if DEFAULT_PROFILE_INSTRUCTIONS.exists():
            return DEFAULT_PROFILE_INSTRUCTIONS.read_text(encoding="utf-8")
        return DEFAULT_INSTRUCTIONS
    return Path(path).read_text(encoding="utf-8")


if __name__ == "__main__":
    main()
