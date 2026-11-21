"""FastAPI entrypoint:  uvicorn pahra.main:create_app --factory --port 8000"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import router
from .config import Settings, get_settings
from .engine import Engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pahra")


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    background: bool = True,
    assistant_factory=None,
) -> FastAPI:
    """`assistant_factory(engine) -> AssistantService` (tests inject an in-process companion)."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        eng = engine or Engine(settings)
        app.state.engine = eng
        await eng.start(background=background)
        hooks = app.state.extra_lifespan
        for start, _ in hooks:
            await start(eng)
        try:
            yield
        finally:
            for _, stop in reversed(hooks):
                await stop(eng)
            await eng.stop()

    app = FastAPI(title="QuietNet", version="0.1.0", lifespan=lifespan)
    app.state.extra_lifespan = []
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    from .assistant.api import router as assistant_router
    from .assistant.service import AssistantService

    app.include_router(assistant_router)
    app.state.assistant = None
    if settings.companion_enabled or assistant_factory:
        factory = assistant_factory or AssistantService

        async def start_assistant(eng: Engine) -> None:
            app.state.assistant = factory(eng)
            await app.state.assistant.start()

        async def stop_assistant(eng: Engine) -> None:
            if app.state.assistant:
                await app.state.assistant.stop()

        app.state.extra_lifespan.append((start_assistant, stop_assistant))
    return app
