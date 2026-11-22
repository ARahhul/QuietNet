"""Owns the companion MCP connection and the running assistant sessions."""

import asyncio
import logging
from typing import Any

from .agent import AgentRunner
from .guided import run_guided
from .mcp_client import CompanionClient, resolve_companion_command
from .session import AssistantSession, Cancelled

log = logging.getLogger("pahra.assistant")
MAX_SESSIONS = 20


class AssistantService:
    def __init__(self, engine, target: Any = "auto", genai_client: Any = "auto"):
        self.engine = engine
        if target == "auto":
            target = resolve_companion_command(engine.settings.companion_command)
        self.companion = CompanionClient(target)
        if genai_client == "auto":
            genai_client = None
            if engine.settings.gemini_api_key:
                from google import genai

                genai_client = genai.Client(api_key=engine.settings.gemini_api_key)
        self.genai = genai_client
        self.sessions: dict[str, AssistantSession] = {}

    async def start(self) -> None:
        await self.companion.start()
        self.engine.companion = self.companion
        # The relay lives in the companion process, so it starts fresh with it: restore saved TVs.
        from .. import forwarder

        if forwarder.mappings(self.engine.conn):
            try:
                await forwarder.apply(self.engine)
            except Exception as e:  # never block startup on the relay
                log.warning("could not restore the DNS relay: %s", e)

    async def stop(self) -> None:
        for s in self.sessions.values():
            if s.task:
                s.task.cancel()
        await self.companion.stop()

    def create(self, description: str) -> AssistantSession:
        s = AssistantSession(self.engine, self.companion, description)
        self.sessions[s.id] = s
        for old in list(self.sessions)[:-MAX_SESSIONS]:
            self.sessions.pop(old, None)
        s.task = asyncio.create_task(self._run(s), name=f"assistant-{s.id}")
        return s

    async def _run(self, s: AssistantSession) -> None:
        try:
            if self.genai is not None:
                try:
                    await AgentRunner(s, self.genai, self.engine.settings.gemini_model, getattr(self.engine, "tracing", None)).run()
                except (Cancelled, asyncio.CancelledError):
                    raise
                except Exception as e:  # LLM outage / quota -> deterministic playbook
                    log.warning("agent failed, switching to guided mode: %s", e)
                    if not s.done:
                        await s.emit({"type": "mode", "mode": "guided", "note": "the AI is unavailable, so I'll walk you through it"})
                        await run_guided(s)
            else:
                await run_guided(s)
        except Cancelled:
            await s.finish("failed", "No reply for a while, so I stopped. Start again any time.", s.guide_from_options())
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("assistant session crashed")
            if not s.done:
                await s.emit({"type": "error", "message": f"Something went wrong: {e}"})
                await s.finish("failed", "Something went wrong. Use the manual setup below.", s.guide_from_options())
