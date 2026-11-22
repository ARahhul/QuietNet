"""A stand-in OpenAI-compatible model, so the AI parts of the dashboard can be clicked through offline.

Answers QuietNet's prompts with fixed, plausible replies: an empty batch for domain explanations,
"don't know" for provider / router guides, and a TP-Link DHCP page for a screenshot (so Setup's
"Show QuietNet your screen" panel shows a full answer, with QuietNet's placeholders filled in).
It never looks at the picture; it only checks one was attached.

Run:  python -m devtools.fake_llm --port 3200
Then: LLM_BASE_URL=http://127.0.0.1:3200/v1  LLM_API_KEY=dev  LLM_MODEL=fake-vision
"""

import argparse
import json

from fastapi import FastAPI, Request

SCREEN = {
    "screen": "TP-Link's DHCP Server settings page",
    "reply": "Here: type <DNS_SERVER> in the Primary DNS box on this page, then Save.",
    "on_track": True,
    "steps": [
        'Leave "DHCP Server" on Enable.',
        'In "Primary DNS", replace 0.0.0.0 with <DNS_SERVER>.',
        'Clear "Secondary DNS" (leave it empty).',
        'Click "Save", then restart the router from System Tools → Reboot.',
    ],
    "fill": [{"field": "Primary DNS", "value": "<DNS_SERVER>"}, {"field": "Secondary DNS", "value": "(leave empty)"}],
    "warning": "",
    "next": "this page after you click Save, if the DNS boxes go back to 0.0.0.0",
    "done": False,
}


def _has_image(messages: list) -> bool:
    return any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"]) for m in messages)


def create_app() -> FastAPI:
    app = FastAPI(title="fake LLM")

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        msgs = body.get("messages") or []
        system = next((m["content"] for m in msgs if m.get("role") == "system" and isinstance(m.get("content"), str)), "")
        if "sent a screenshot" in system:
            answer = SCREEN if _has_image(msgs) else {"screen": "No picture came through.", "steps": []}
        elif "Explain these domains" in json.dumps(msgs):
            answer = {"domains": []}
        else:
            answer = {"known": False}
        return {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(answer)}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020},
        }

    return app


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=3200)
    uvicorn.run(create_app(), host="127.0.0.1", port=p.parse_args().port, log_level="warning")


if __name__ == "__main__":
    main()
