import logging

from fastapi import FastAPI

from api.controllers import benchmark_controller, health_controller
from handlers.exception_handler import add_exception_handlers

for _name in ("nyuctf", "nyuctf.challenge", "nyuctf.dataset"):
    logging.getLogger(_name).setLevel(logging.WARNING)

tags_metadata = [
    {"name": "health", "description": "Health checks"},
    {"name": "benchmarks", "description": "Challenge catalog, payloads, and disk materialization"},
]

app = FastAPI(
    version="1.0",
    title="Gencyber Workbench API",
    description="Challenge-only API for the single-image workbench (no sandbox / leaderboard).",
    openapi_tags=tags_metadata,
)

app.include_router(
    health_controller.router,
    prefix="/health",
    tags=["health"],
    responses={404: {"description": "Not found"}},
)

app.include_router(
    benchmark_controller.router,
    prefix="/benchmarks",
    tags=["benchmarks"],
    responses={404: {"description": "Not found"}},
)

add_exception_handlers(app=app)
