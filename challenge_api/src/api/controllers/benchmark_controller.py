from fastapi import APIRouter, HTTPException, Query
from typing import Any, Dict, Optional
import logging

from application.benchmark.helpers import benchmark_helper as bh
from application.benchmark.services.benchmark_service import BenchmarkService
from domain.models.benchmark.challenge_model import (
    ChallengeRequest,
    ChallengeServicesRequest,
    MaterializeRequest,
    ResolveFlagRequest,
    Split,
    TaskRequest,
    ValidateSubmissionRequest,
)
from application.benchmark.services.challenge_runtime_service import ChallengeRuntimeService

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
            force=req.force,
            session_id=req.session_id,
        )
    except Exception as e:
        logger.exception("Failed to materialize challenge")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/validate-submission")
def post_validate_submission(req: ValidateSubmissionRequest) -> Dict[str, Any]:
    """Validate a submitted flag against the challenge bound to ``session_id``.

    Always returns 200 with ``{accepted: bool, reason}`` — an ordinary wrong flag,
    an unbound session, or a challenge without a ground-truth oracle are all just a
    ``False`` verdict (not an HTTP error), so the agent can treat the response
    uniformly as accept/reject.
    """
    try:
        service = BenchmarkService()
        return service.validate_submission(
            session_id=req.session_id,
            candidate=req.candidate,
        )
    except Exception as e:
        logger.exception("Failed to validate submission")
        return {"accepted": False, "reason": f"validation error: {e}"}


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


@router.post("/start-challenge-services")
def post_start_challenge_services(req: ChallengeServicesRequest) -> Dict[str, Any]:
    try:
        runtime = ChallengeRuntimeService()
        return runtime.start_challenge_services(
            benchmark=req.benchmark,
            split=req.split,
            challenge_id=req.challenge_id,
            written_root=req.written_root,
        )
    except Exception as e:
        logger.exception("Failed to start challenge services")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/stop-challenge-services")
def post_stop_challenge_services(req: ChallengeServicesRequest) -> Dict[str, Any]:
    try:
        runtime = ChallengeRuntimeService()
        return runtime.stop_challenge_services(
            benchmark=req.benchmark,
            split=req.split,
            challenge_id=req.challenge_id,
            written_root=req.written_root,
            project_name=req.project_name,
        )
    except Exception as e:
        logger.exception("Failed to stop challenge services")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/resolve-flag")
def post_resolve_flag(req: ResolveFlagRequest) -> Dict[str, Any]:
    """Ground-truth flag for workflow validation (trusted backend callers only)."""
    try:
        service = BenchmarkService()
        flag = service.resolve_ground_truth_flag(
            benchmark=req.benchmark,
            split=req.split,
            challenge_id=req.challenge_id,
        )
        if flag is None:
            raise HTTPException(status_code=404, detail="Flag not available for this challenge")
        return {"benchmark": req.benchmark, "split": req.split, "challenge_id": req.challenge_id, "flag": flag}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to resolve flag")
        raise HTTPException(status_code=400, detail=str(e))
