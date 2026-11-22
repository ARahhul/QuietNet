"""Langfuse: a trace for every AI call, and the prompts those calls use.

Off (every helper is a no-op) unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set. Then:

* Each AI job is a trace (explain-domains, home-wifi-guide, tv-assistant) holding one generation per
  model call: the exact messages sent, the reply, model, temperature, token usage, latency, and errors.
  TV assistant tool calls are recorded too. What goes out is what the model sees, so the assistant's
  IP/MAC addresses stay replaced by handles.
* Prompts (pahra.prompts) live in Langfuse Prompt Management. At startup any prompt Langfuse doesn't
  have yet is created, labelled "production". After that the production version in Langfuse is what
  runs (cached 5 min), and generations link to the version they used. If the code's text changes and
  nobody edited the prompt in Langfuse, the new text is pushed as the next version; an edit made in
  Langfuse is never overwritten (force with `python -m pahra.tracing push-prompts --update`).
  If Langfuse is unreachable, the code's text is used.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from .prompts import ALL_PROMPTS, PromptDef

log = logging.getLogger("pahra.tracing")

PROMPT_LABEL = "production"
PROMPT_CACHE_S = 300


@dataclass
class Prompt:
    """A prompt ready to send: compiled messages, model settings, and the Langfuse version it came from."""

    messages: list[dict]
    config: dict
    client: Any = None  # Langfuse prompt client to link generations to; None = the code's text

    def _join(self, role: str) -> str:
        return "\n\n".join(m["content"] for m in self.messages if m["role"] == role)

    @property
    def system(self) -> str:
        return self._join("system")

    @property
    def user(self) -> str:
        return self._join("user")

    @property
    def temperature(self) -> float:
        try:
            return float(self.config.get("temperature", 0.2))
        except (TypeError, ValueError):
            return 0.2


class _Noop:
    def update(self, **_: Any) -> "_Noop":
        return self


NOOP = _Noop()


def _digest(messages: Any, config: Any) -> str:
    # Langfuse returns chat messages with an extra "type": "chatmessage"; compare role and text only.
    msgs = [{"role": m.get("role"), "content": m.get("content")} if isinstance(m, dict) else m for m in messages or []]
    return hashlib.sha256(json.dumps([msgs, config or {}], sort_keys=True).encode()).hexdigest()[:16]


def _not_found(e: Exception) -> bool:
    return getattr(e, "status_code", None) == 404 or e.__class__.__name__ == "NotFoundError"


class Tracing:
    def __init__(self, client: Any = None):
        self.lf = client
        self.prompts: dict[str, str] = {}  # name -> what the last push did
        self.error: str | None = None

    @classmethod
    def from_settings(cls, s) -> "Tracing":
        if not (s.langfuse_public_key and s.langfuse_secret_key):
            return cls()
        try:
            from langfuse import Langfuse

            return cls(
                Langfuse(
                    public_key=s.langfuse_public_key,
                    secret_key=s.langfuse_secret_key,
                    base_url=s.langfuse_base_url,
                    environment=s.langfuse_environment or None,
                )
            )
        except Exception as e:  # never let observability stop the app
            log.warning("Langfuse disabled: %s", e)
            t = cls()
            t.error = str(e)[:200]
            return t

    @property
    def enabled(self) -> bool:
        return self.lf is not None

    def status(self) -> dict:
        return {"enabled": self.enabled, "prompts": self.prompts, "error": self.error}

    # ------------------------------------------------------------ prompts

    async def prompt(self, d: PromptDef, **values: Any) -> Prompt:
        """The production prompt from Langfuse (or the code's text), with {{values}} filled in."""
        if self.lf is not None:
            try:
                pc = await asyncio.to_thread(
                    self.lf.get_prompt,
                    d.name,
                    type="chat",
                    label=PROMPT_LABEL,
                    cache_ttl_seconds=PROMPT_CACHE_S,
                    fallback=d.messages,
                    max_retries=1,
                    fetch_timeout_seconds=5,
                )
                msgs = [
                    {"role": m["role"], "content": m["content"]}
                    for m in pc.compile(**{k: str(v) for k, v in values.items()})
                    if isinstance(m, dict) and "role" in m and "content" in m
                ]
                if msgs:
                    return Prompt(msgs, {**d.config, **(pc.config or {})}, None if pc.is_fallback else pc)
            except Exception as e:
                log.warning("Langfuse prompt %s unavailable, using the built-in text: %s", d.name, e)
        return Prompt(d.compile(**values), dict(d.config))

    def _push_one(self, d: PromptDef, pushed: str | None, update: bool) -> tuple[str, str | None]:
        """Runs in a thread. Returns (what happened, digest of the code text now in Langfuse or None)."""
        mine = _digest(d.messages, d.config)
        try:
            cur = self.lf.get_prompt(d.name, type="chat", label=PROMPT_LABEL, cache_ttl_seconds=0, max_retries=0)
        except Exception as e:
            if not _not_found(e):
                raise
            cur = None
        create = dict(name=d.name, prompt=d.messages, type="chat", labels=[PROMPT_LABEL], config=d.config, tags=["quietnet"])
        if cur is None:
            self.lf.create_prompt(**create, commit_message=f"From QuietNet: {d.description}"[:250])
            return "created", mine
        theirs = _digest(cur.prompt, cur.config)
        if theirs == mine:
            return f"in sync (v{cur.version})", mine
        if update or (pushed is not None and theirs == pushed):
            self.lf.create_prompt(**create, commit_message="Updated from QuietNet's code")
            return "updated from code", mine
        return f"kept Langfuse's edit (v{cur.version})", pushed

    async def push_prompts(self, conn: sqlite3.Connection | None = None, update: bool = False) -> dict[str, str]:
        """Make Langfuse hold every prompt in pahra.prompts (see the module docstring for the rules)."""
        if self.lf is None:
            return {}
        from .db import get_meta, set_meta

        for d in ALL_PROMPTS:
            key = f"langfuse_prompt:{d.name}"
            try:
                result, digest = await asyncio.to_thread(self._push_one, d, get_meta(conn, key) if conn else None, update)
                if conn is not None and digest:
                    set_meta(conn, key, digest)
            except Exception as e:
                result = f"failed: {e.__class__.__name__}: {e}"[:200]
                log.warning("pushing prompt %s to Langfuse failed: %s", d.name, e)
            self.prompts[d.name] = result
        log.info("Langfuse prompts: %s", self.prompts)
        return self.prompts

    # ------------------------------------------------------------ observations

    @contextmanager
    def trace(
        self,
        name: str,
        *,
        as_type: str = "chain",
        input: Any = None,
        metadata: dict | None = None,
        session_id: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> Iterator[Any]:
        """A root observation naming the trace; everything started inside nests under it."""
        if self.lf is None:
            yield NOOP
            return
        from langfuse import propagate_attributes

        with self.lf.start_as_current_observation(name=name, as_type=as_type, input=input, metadata=metadata) as obs:
            with propagate_attributes(trace_name=name, session_id=session_id, tags=["quietnet", *tags]):
                try:
                    yield obs
                except Exception as e:
                    obs.update(level="ERROR", status_message=f"{e.__class__.__name__}: {e}"[:500])
                    raise

    @contextmanager
    def generation(
        self,
        name: str,
        *,
        model: str,
        prompt: Prompt,
        input: Any = None,
        metadata: dict | None = None,
    ) -> Iterator[Any]:
        """One model call. Update it with output= and usage_details= when the reply arrives."""
        if self.lf is None:
            yield NOOP
            return
        with self.lf.start_as_current_observation(
            name=name,
            as_type="generation",
            model=model,
            input=prompt.messages if input is None else input,
            model_parameters={"temperature": prompt.temperature},
            prompt=prompt.client,
            metadata=metadata,
        ) as gen:
            try:
                yield gen
            except Exception as e:
                gen.update(level="ERROR", status_message=f"{e.__class__.__name__}: {e}"[:500])
                raise

    @contextmanager
    def tool(self, name: str, *, input: Any = None) -> Iterator[Any]:
        if self.lf is None:
            yield NOOP
            return
        with self.lf.start_as_current_observation(name=name, as_type="tool", input=input) as obs:
            try:
                yield obs
            except Exception as e:
                obs.update(level="ERROR", status_message=f"{e.__class__.__name__}: {e}"[:500])
                raise

    def shutdown(self) -> None:
        if self.lf is not None:
            try:
                self.lf.shutdown()  # flushes what's still queued
            except Exception as e:
                log.warning("Langfuse flush failed: %s", e)


def gemini_usage(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage_metadata", None)
    if u is None:
        return {}
    got = {
        "input": getattr(u, "prompt_token_count", None),
        "output": getattr(u, "candidates_token_count", None),
        "total": getattr(u, "total_token_count", None),
    }
    return {k: v for k, v in got.items() if isinstance(v, int)}


def openai_usage(body: dict) -> dict[str, int]:
    u = body.get("usage") or {}
    got = {"input": u.get("prompt_tokens"), "output": u.get("completion_tokens"), "total": u.get("total_tokens")}
    return {k: v for k, v in got.items() if isinstance(v, int)}


# ---------------------------------------------------------------- CLI: push the prompts by hand


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .config import get_settings
    from .db import init_sqlite_wal

    p = argparse.ArgumentParser(prog="python -m pahra.tracing")
    sub = p.add_subparsers(dest="cmd", required=True)
    push = sub.add_parser("push-prompts", help="create / update QuietNet's prompts in Langfuse")
    push.add_argument("--update", action="store_true", help="push the code's text even over edits made in Langfuse")
    args = p.parse_args(argv)

    s = get_settings()
    t = Tracing.from_settings(s)
    if not t.enabled:
        print("Langfuse isn't configured: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.")
        return 1
    if args.cmd == "push-prompts":
        conn = init_sqlite_wal(s.db_path)
        for name, result in asyncio.run(t.push_prompts(conn, update=args.update)).items():
            print(f"{name}: {result}")
        t.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
