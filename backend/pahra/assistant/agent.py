"""Gemini function-calling agent driving the companion MCP tools.

* Tool declarations come from the MCP server's list_tools (+ a few backend-only tools).
* Every mutating tool waits for the user's OK in the UI (session.call_tool / set_private_dns).
* IP and MAC addresses are replaced with handles before anything reaches Gemini.
* The agent never sees or chooses the QuietNet hostname: the backend creates the device and injects it.
* The prompt is pahra.prompts.TV_ASSISTANT (via Langfuse when configured). A session is one Langfuse
  trace: every Gemini step and tool call, exactly as redacted for Gemini.
"""

import copy
import json
import logging
from typing import Any

from ..forwarder import FORWARDER_TOOLS
from ..prompts import TV_ASSISTANT
from ..tracing import Tracing, gemini_usage
from .session import AssistantSession

log = logging.getLogger("pahra.agent")

MAX_STEPS = 20

BACKEND_TOOLS = [
    {
        "name": "show_device_choices",
        "description": "Show the user up to 5 candidate devices as tappable choices and wait for their pick. Returns the user's reply.",
        "parameters": {
            "type": "object",
            "properties": {
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "handle": {"type": "string"},
                            "label": {"type": "string", "description": "e.g. 'LG webOS TV (living room)'"},
                            "platform": {"type": "string"},
                            "detail": {"type": "string", "description": "why it might be this one"},
                        },
                        "required": ["handle", "label"],
                    },
                },
                "question": {"type": "string"},
            },
            "required": ["candidates"],
        },
    },
    {
        "name": "ask_user",
        "description": "Ask the user something and wait for the reply (e.g. 'Reply done when ADB debugging is on').",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "kind": {"type": "string", "enum": ["text", "pairing_code"]},
            },
            "required": ["question"],
        },
    },
    {
        "name": "setup_forwarder",
        "description": "Run QuietNet's DNS relay on this laptop for a locked TV (asks the user first). Creates the TV in "
                       "QuietNet and returns forwarder_ip: the DNS server address the user must type into the TV.",
        "parameters": {
            "type": "object",
            "properties": {
                "ip": {"type": "string", "description": "the TV's handle, e.g. host-3"},
                "device_name": {"type": "string", "description": "friendly name for the TV in QuietNet"},
                "brand": {"type": "string"},
            },
            "required": ["ip"],
        },
    },
    {
        "name": "verify_connected",
        "description": "After set_private_dns: wait up to 60 s for the TV's first DNS query through QuietNet.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "finish",
        "description": "End the conversation with the result shown to the user.",
        "parameters": {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": ["connected", "guide", "failed"]},
                "message": {"type": "string"},
                "guide_title": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["outcome", "message"],
        },
    },
]


def _clean_schema(schema: dict) -> dict:
    """Gemini accepts JSON Schema, but drop keys it tends to reject."""
    s = copy.deepcopy(schema or {"type": "object", "properties": {}})
    for k in ("$schema", "additionalProperties", "title"):
        s.pop(k, None)
    for p in (s.get("properties") or {}).values():
        if isinstance(p, dict):
            p.pop("title", None)
            if "anyOf" in p:  # Optional[x] -> x
                non_null = [a for a in p["anyOf"] if a.get("type") != "null"]
                if len(non_null) == 1:
                    p.update(non_null[0])
                    p.pop("anyOf")
            p.pop("default", None)
    return s


def build_declarations(session: AssistantSession) -> list[dict]:
    decls = []
    for name, tool in session.companion.tools.items():
        if name in FORWARDER_TOOLS:
            continue  # the backend owns the relay's device mapping; the model uses setup_forwarder
        if name == "setup_forwarder":
            continue  # never let a companion tool shadow the backend one
        schema = _clean_schema(getattr(tool, "input_schema", None) or {})
        if name == "set_private_dns":
            props = schema.setdefault("properties", {})
            props.pop("hostname", None)
            props["device_name"] = {"type": "string", "description": "friendly name for the TV in QuietNet"}
            props["brand"] = {"type": "string"}
            schema["required"] = [r for r in schema.get("required", []) if r != "hostname"]
        decls.append({"name": name, "description": (tool.description or "")[:1000], "parameters": schema})
    has_relay = any(t in session.companion.tools for t in FORWARDER_TOOLS)
    decls.extend(t for t in BACKEND_TOOLS if t["name"] != "setup_forwarder" or has_relay)
    return decls


class AgentRunner:
    def __init__(self, session: AssistantSession, genai_client: Any, model: str, tracing: Tracing | None = None):
        self.s = session
        self.client = genai_client
        self.model = model
        self.tracing = tracing or Tracing()
        self.payloads: list[str] = []  # what we sent to Gemini (tests assert no IP/MAC leaks)

    async def _dispatch(self, name: str, args: dict) -> dict:
        s = self.s
        if name == "show_device_choices":
            cands = args.get("candidates") or []
            await s.emit({"type": "devices", "devices": [
                {"handle": c.get("handle", ""), "label": c.get("label", "device"), "platform": c.get("platform", ""),
                 "detail": c.get("detail", "")} for c in cands[:5]
            ]})
            reply = await s.ask(args.get("question") or "Which one is it?")
            return {"user_reply": reply}
        if name == "ask_user":
            return {"user_reply": await s.ask(args.get("question") or "?", args.get("kind") or "text")}
        if name == "verify_connected":
            return {"connected": await s.verify_connected()}
        if name == "finish":
            outcome = args.get("outcome") or "guide"
            guide = None
            if outcome != "connected":
                steps = [s.fill(x) for x in args.get("steps") or []]
                lib = s.guide_from_options()
                if steps:
                    known = s.library_steps()
                    guide = {
                        "title": args.get("guide_title") or (lib or {}).get("title") or "Next steps",
                        "steps": steps,
                        "values": (lib or {}).get("values") or {},
                        "ai_suggested": any(x not in known for x in steps),
                    }
                else:
                    guide = lib
            await s.finish(outcome, args.get("message") or "", guide)
            return {"ok": True}
        if name == "setup_forwarder":
            res = await s.setup_forwarder(args.get("ip", ""), args.get("device_name"), args.get("brand"))
            keep = ("ok", "declined", "error", "code", "hint", "forwarder_ip")
            return {k: res[k] for k in keep if k in res}
        if name == "set_private_dns":
            res = await s.set_private_dns(args.get("ip", ""), args.get("device_name"), args.get("brand"))
            # The DoT hostname is the device's ClientID: never echo it (or the read-back) to the model.
            keep = ("ok", "verified", "declined", "error", "code", "status", "sdk", "hint")
            return {k: res[k] for k in keep if k in res}
        if name in s.companion.tools:
            args = {k: v for k, v in args.items() if v is not None}
            return await s.call_tool(name, args)
        return {"ok": False, "error": f"unknown tool {name}"}

    async def run(self) -> None:
        s = self.s
        with self.tracing.trace("tv-assistant", as_type="agent", input=s.description, session_id=s.id, tags=("assistant",)) as t:
            outcome = await self._run()
            t.update(output={"outcome": outcome})

    async def _run(self) -> str:
        from google.genai import types

        s, tr = self.s, self.tracing
        await s.emit({"type": "mode", "mode": "agent"})
        decls = [types.FunctionDeclaration(name=d["name"], description=d["description"], parameters_json_schema=d["parameters"])
                 for d in build_declarations(s)]
        playbook = "\nPlaybook from the companion:\n" + s.companion.playbook if s.companion.playbook else ""
        p = await tr.prompt(TV_ASSISTANT, playbook=playbook, description=s.description)
        config = types.GenerateContentConfig(
            system_instruction=p.system,
            tools=[types.Tool(function_declarations=decls)],
            temperature=p.temperature,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents: list = [types.Content(role="user", parts=[types.Part(text=p.user)])]

        for step in range(MAX_STEPS):
            sent = [c.model_dump(mode="json", exclude_none=True) for c in contents]
            self.payloads.append(json.dumps(sent[-2:], default=str))
            with tr.generation(
                "tv-assistant-step",
                model=self.model,
                prompt=p,
                input=[{"role": "system", "content": p.system}, *sent],
                metadata={"step": step, "tools": len(decls)},
            ) as gen:
                resp = await self.client.aio.models.generate_content(model=self.model, contents=contents, config=config)
                cand = resp.candidates[0].content if resp.candidates else None
                gen.update(
                    output=cand.model_dump(mode="json", exclude_none=True) if cand is not None else None,
                    usage_details=gemini_usage(resp),
                )
            if cand is None:
                raise RuntimeError("empty response from Gemini")
            contents.append(cand)
            calls = [p_.function_call for p_ in (cand.parts or []) if p_.function_call]
            texts = [p_.text for p_ in (cand.parts or []) if p_.text and not p_.thought]
            text = s.redactor.unredact(" ".join(texts)).strip()
            if not calls:
                reply = await s.ask(text or "Anything else?")
                contents.append(types.Content(role="user", parts=[types.Part(text=s.redactor.redact(reply))]))
                continue
            if text:
                await s.say(text)
            parts = []
            for fc in calls:
                with tr.tool(fc.name, input=dict(fc.args or {})) as tool:  # still redacted: handles, not addresses
                    args = s.redactor.unredact(dict(fc.args or {}))
                    result = s.redactor.redact(await self._dispatch(fc.name, args))
                    tool.update(output=result)
                parts.append(types.Part.from_function_response(name=fc.name, response={"result": result}))
                if s.done:
                    return fc.name
            contents.append(types.Content(role="user", parts=parts))
        await s.finish("failed", "That took too many steps. Try the manual setup below or start again.", s.guide_from_options())
        return "too many steps"
