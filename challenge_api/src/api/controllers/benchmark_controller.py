from fastapi import APIRouter, HTTPException, Query
from typing import Any, Dict, Optional
import logging

from application.benchmark.helpers import benchmark_helper as bh
from application.benchmark.services.benchmark_service import BenchmarkService
from domain.models.benchmark.challenge_model import (
    ChallengeRequest,
    MaterializeRequest,
    Split,
    TaskRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/supported")
def supported_benchmarks() -> dict:
    return {"benchmarks": sorted(bh.BENCHMARK_REGISTRY.keys())}


@router.get("/metadata")
def get_benchmark_metadata_get(
    benchmark: str = Query(default="nyuctf"),
    split: Optional[Split] = Query(default=None),
) -> Dict[str, Any]:
    try:
        service = BenchmarkService()
        return service.get_benchmark_metadata(benchmark=benchmark, split=split)
    except Exception as e:
        logger.exception("Failed to load benchmark metadata")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/challenge")
def get_challenge(req: ChallengeRequest) -> Dict[str, Any]:
    try:
        service = BenchmarkService()
        return service.get_challenge(
            benchmark=req.benchmark,
            split=req.split,
            challenge_id=req.challenge_id,
        )
    except Exception as e:
        logger.exception("Failed to load challenge")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/seed-prompt")
def post_seed_prompt(req: ChallengeRequest) -> Dict[str, Any]:
    try:
        service = BenchmarkService()
        seed = service.build_seed_prompt(
            benchmark=req.benchmark,
            split=req.split,
            challenge_id=req.challenge_id,
        )
        return {"seed_prompt": seed}
    except Exception as e:
        logger.exception("Failed to build seed prompt")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/materialize")
def post_materialize(req: MaterializeRequest) -> Dict[str, Any]:
    """Decode/write challenge files under ``CHALLENGE_ROOT`` (shared volume)."""
    try:
        service = BenchmarkService()
        return service.materialize_challenge(
            benchmark=req.benchmark,
            split=req.split,
            challenge_id=req.challenge_id,
            relative_path=req.relative_path,
        )
    except Exception as e:
        logger.exception("Failed to materialize challenge")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/challenge/files")
def get_challenge_files(req: ChallengeRequest) -> Dict[str, Any]:
    try:
        service = BenchmarkService()
        return service.get_challenge_file_payloads(
            benchmark=req.benchmark,
            split=req.split,
            challenge_id=req.challenge_id,
        )
    except Exception as e:
        logger.exception("Failed to load challenge files")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/task")
def get_task_with_challenge(req: TaskRequest) -> Dict[str, Any]:
    try:
        service = BenchmarkService()
        return service.get_task_payload(
            benchmark=req.benchmark,
            split=req.split,
            challenge_id=req.challenge_id,
            task=req.task,
        )
    except Exception as e:
        logger.exception("Failed to build task payload")
        raise HTTPException(status_code=400, detail=str(e))
