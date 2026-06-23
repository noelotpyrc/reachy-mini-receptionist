"""Legacy voice brain powered by ``claude -p``.

Status: legacy/fallback. The accepted product path uses the m1max local S2S
backend plus remote LLM providers. Keep this module runnable for
regression/reference until legacy removal is explicitly approved.

Each user utterance goes to a headless Claude Code agent (``claude -p``, Haiku)
running a fixed receptionist persona. Conversation continuity is Claude Code's own
session management: capture the ``session_id`` on turn 1, ``--resume`` it after, so
the agent remembers the whole exchange.

Why claude -p (vs a raw Messages loop): it's a real agent (tool use + sessions) out
of the box, uses Claude Code's own auth (no separate ANTHROPIC_API_KEY), and is the
simplest first pass. Trade-off: each turn spawns a process (~3s) — move to the
in-process Agent SDK later if the latency hurts.

v0 scope: conversation only (text reply -> spoken). Robot-action tools (nod/look)
and a real FAQ/appointment tool layer come next (via MCP); for now the clinic facts
live in the persona.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time

# Run the agent from a neutral, empty dir so it doesn't load any project's CLAUDE.md
# / files — keeps it a pure receptionist, not a coding agent looking at a repo.
_BRAIN_CWD = os.path.join(tempfile.gettempdir(), "reachy_brain")
os.makedirs(_BRAIN_CWD, exist_ok=True)

PERSONA = """You are Reachy, the friendly front-desk receptionist robot at a medical clinic.
You greet visitors and answer their questions at the front desk.

Style: every reply is SPOKEN ALOUD by a robot, so keep it to 1-2 short, natural
sentences. Plain text only — no lists, markdown, emoji, or stage directions. Warm and brief.

Rules: Never give medical advice. The clinic facts below are complete and correct —
your single source of truth. If the answer is in the facts, give it exactly: quote
the Wi-Fi network, hours, room numbers, floors, and names verbatim, and treat any
listed provider or department as definitely available (name them). Only say a staff
member will help when the facts genuinely don't contain the answer. Never guess,
infer, or invent details.

Stay fully in character as Reachy at all times. Treat every message as something a
visitor is saying to you at the front desk and respond only as the receptionist —
never comment on code, testing, systems, or how you work."""

# Authoritative clinic facts live in a markdown file next to this module (edit it to
# update clinic info). Read at startup and appended to the persona.
_FACTS_PATH = os.path.join(os.path.dirname(__file__), "clinic_facts.md")


class ReceptionBrain:
    """A receptionist agent over ONE persistent ``claude -p`` process per conversation.

    Previously each turn spawned a fresh ``claude -p`` (~2-3s startup *every* turn). Instead we
    keep one process alive for the whole conversation via ``--input-format/--output-format
    stream-json``: write each user turn as a JSON line on stdin, read stdout events until the
    ``result`` event. The process keeps the conversation context itself (no ``--resume``), so
    only the first turn pays startup; later turns are just a write+read.

    NOTE: ``--input-format stream-json`` is undocumented; the input/output shapes here were
    verified empirically against Claude Code 2.1.x (Haiku).
    """

    def __init__(self, model: str = "sonnet", persona: str = PERSONA,
                 facts_path: str = _FACTS_PATH, claude_bin: str | None = None,
                 conversation_timeout: float = 120.0, turn_timeout: float = 60.0):
        self.model = model
        self.persona = self._with_facts(persona, facts_path)
        # "Same conversation" = turns within conversation_timeout of each other; a longer idle
        # gap (the visitor left) ends the process so the next turn starts a fresh conversation.
        self.conversation_timeout = conversation_timeout
        self.turn_timeout = turn_timeout
        self._bin = claude_bin or shutil.which("claude") or "claude"
        self._proc: subprocess.Popen | None = None
        self._last_ts: float | None = None

    def prewarm(self) -> None:
        """Spawn the claude process now (pays the ~2-3s startup) so the first real turn is fast
        — call at conversation start, e.g. while the opener is being spoken."""
        if self._proc is None or self._proc.poll() is not None:
            self._start()

    def respond(self, utterance: str, timeout: float | None = None) -> str:
        """Send one user turn to the persistent process; return the receptionist's reply.
        Starts (or restarts) the process as needed; resets after a long idle gap."""
        now = time.monotonic()
        if self._last_ts is not None and now - self._last_ts > self.conversation_timeout:
            self.reset()  # idle gap -> new visitor / new conversation
        self._last_ts = now

        if self._proc is None or self._proc.poll() is not None:
            self._start()
        line = json.dumps({"type": "user",
                           "message": {"role": "user", "content": utterance}}) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            self._start()  # process had died -> restart and retry once
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        return self._read_reply(timeout or self.turn_timeout)

    def _start(self) -> None:
        self.reset()
        # --tools "" = talk only; --exclude-dynamic... + project settings keep the coding-agent
        # context from bleeding into the persona; --verbose is required for stream-json output.
        cmd = [self._bin, "-p", "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose",
               "--model", self.model, "--tools", "",
               "--exclude-dynamic-system-prompt-sections", "--setting-sources", "project",
               "--system-prompt", self.persona]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=self._env(), cwd=_BRAIN_CWD)

    def _read_reply(self, timeout: float) -> str:
        """Read stdout events until this turn's ``result`` event; return its final text."""
        import select

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("claude -p stream: turn timed out")
            ready, _, _ = select.select([self._proc.stdout], [], [], remaining)
            if not ready:
                continue
            raw = self._proc.stdout.readline()
            if not raw:
                raise RuntimeError("claude -p stream: process closed stdout")
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                if event.get("is_error"):
                    raise RuntimeError(f"claude -p error: {event.get('result')}")
                return (event.get("result") or "").strip()

    def reset(self) -> None:
        """End the conversation — terminate the persistent process (next turn starts fresh)."""
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._proc = None

    @staticmethod
    def _env() -> dict:
        # claude -p refuses to nest inside another Claude Code session — strip the markers
        env = dict(os.environ)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            env.pop(k, None)
        return env

    @staticmethod
    def _with_facts(persona: str, facts_path: str) -> str:
        """Append the authoritative clinic-facts file to the persona (if present)."""
        try:
            facts = open(facts_path, encoding="utf-8").read().strip()
        except OSError:
            return persona
        return (f"{persona}\n\n--- AUTHORITATIVE CLINIC FACTS "
                f"(use exactly; never invent) ---\n{facts}")


# --- Alternative backend: in-process Pydantic AI over OpenRouter -------------------
# `claude -p` (above) is the first-pass stopgap (single-provider, needs the tmux/keychain auth
# hack, per-turn process). PydanticBrain is the chosen next iteration: in-process (no spawn),
# multi-provider via OpenRouter (swap models with one string), ~0.6s/turn in benchmarks. DSPy is
# the long-term path (prompt optimization). See docs/brain-backend-research.md.

_DEFAULT_OR_MODEL = os.environ.get("REACHY_BRAIN_MODEL", "openai/gpt-oss-20b")


def default_openrouter_model() -> str:
    """Model PydanticBrain will use when the caller does not pass one explicitly."""
    return _DEFAULT_OR_MODEL


def _openrouter_key() -> str:
    """OpenRouter key from env, falling back to the project `.env` (the daemon often launches
    without it exported)."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    try:
        for raw in open(env_path, encoding="utf-8"):
            if raw.startswith("OPENROUTER_API_KEY="):
                return raw.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    raise RuntimeError("OPENROUTER_API_KEY not set (export it or put it in the project .env)")


class PydanticBrain:
    """Receptionist brain over Pydantic AI + OpenRouter — in-process, multi-provider.

    Same interface as ``ReceptionBrain`` (``respond``/``prewarm``/``reset``) so the daemon can
    swap backends. Conversation memory is the in-process ``message_history``; an idle gap longer
    than ``conversation_timeout`` (or an explicit ``reset()``) ends the conversation so the next
    visitor starts fresh. No per-turn process spawn and no keychain/tmux hack — just an API call.
    """

    def __init__(self, model: str = _DEFAULT_OR_MODEL, persona: str = PERSONA,
                 facts_path: str = _FACTS_PATH, api_key: str | None = None,
                 conversation_timeout: float = 120.0):
        self.model = model
        self.persona = ReceptionBrain._with_facts(persona, facts_path)
        self.conversation_timeout = conversation_timeout
        self._api_key = api_key or _openrouter_key()
        self._agent = None
        self._history: list = []
        self._last_ts: float | None = None

    def _build(self) -> None:
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openrouter import OpenRouterProvider
        model = OpenAIChatModel(self.model, provider=OpenRouterProvider(api_key=self._api_key))
        self._agent = Agent(model, instructions=self.persona)

    def prewarm(self) -> None:
        """Construct the agent now (just import+build, no process) so the first turn isn't slowed."""
        if self._agent is None:
            self._build()

    def respond(self, utterance: str, timeout: float | None = None) -> str:
        now = time.monotonic()
        if self._last_ts is not None and now - self._last_ts > self.conversation_timeout:
            self.reset()  # idle gap -> new visitor / new conversation
        self._last_ts = now
        if self._agent is None:
            self._build()
        settings = {"timeout": timeout} if timeout else None
        r = self._agent.run_sync(utterance, message_history=self._history, model_settings=settings)
        self._history = r.all_messages()
        return (r.output or "").strip()

    def reset(self) -> None:
        """End the conversation — clear in-process history (next turn starts fresh)."""
        self._history = []
        self._last_ts = None
