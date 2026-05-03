from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from application.benchmark.helpers import leaderboard_helpers as lh
from domain.models.benchmark.leaderboard_model import LeaderboardRunRecord, LeaderboardSubmissionMetadata

logger = logging.getLogger(__name__)


def _leaderboard_root() -> Path:
    root = os.getenv("LEADERBOARD_DIR", "/app/leaderboard").strip()
    return Path(root).expanduser().resolve()


def _nyuctf_flag_and_category(split: str, challenge_id: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (flag, category) from local nyuctf dataset; None if unavailable."""
    try:
        from nyuctf.dataset import CTFDataset
        from nyuctf.challenge import CTFChallenge
    except ImportError:
        return None, None

    try:
        ds = CTFDataset(split=split)
        ch = CTFChallenge(ds.get(challenge_id), ds.basedir)
        cat = getattr(ch, "category", None) or ch.challenge_info.get("category")
        return getattr(ch, "flag", None), cat
    except Exception as e:
        logger.warning("Could not load nyuctf challenge %s: %s", challenge_id, e)
        return None, None


def ground_truth_flag_for_challenge(benchmark: str, split: str, challenge_id: str) -> Optional[str]:
    """Dataset flag for verification (nyuctf only today)."""
    if (benchmark or "").strip().lower() != "nyuctf":
        return None
    flag, _ = _nyuctf_flag_and_category(str(split), str(challenge_id))
    return flag


def _nyuctf_dataset_index(split: str) -> Tuple[Dict[str, str], int]:
    """challenge_id -> category, and total count."""
    try:
        from nyuctf.dataset import CTFDataset
    except ImportError:
        return {}, 0

    ds = CTFDataset(split=split)
    categories: Dict[str, str] = {}
    for cid in ds.dataset.keys():
        entry = ds.dataset[cid]
        if isinstance(entry, dict):
            categories[cid] = str(entry.get("category") or "")
    return categories, len(ds.dataset)


class LeaderboardService:
    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root or _leaderboard_root()

    def submission_dir(self, submission_id: str) -> Path:
        sid = lh.submission_id_slug(submission_id)
        d = self._root / "transcripts" / sid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _summary_path(self, submission_id: str) -> Path:
        return self.submission_dir(submission_id) / "summary.json"

    def _readme_path(self, submission_id: str) -> Path:
        return self.submission_dir(submission_id) / "README.md"

    def _ensure_readme(self, submission_id: str) -> None:
        path = self._readme_path(submission_id)
        if path.exists():
            return
        path.write_text(
            "# Gencyber leaderboard submission\n\n"
            "- **summary.json** — NYU CTF leaderboard format (`metadata` + `results`).\n"
            "- **Per-challenge JSON** — `{challenge_id}.gencyber.json` transcripts.\n"
            "- **scores** — computed field in summary (`scores`) after each record.\n\n"
            "Contact: see submission metadata `link` field.\n",
            encoding="utf-8",
        )

    def _load_summary(self, submission_id: str) -> Dict[str, Any]:
        p = self._summary_path(submission_id)
        if not p.exists():
            return {
                "metadata": {},
                "results": {},
                "scores": {},
            }
        return json.loads(p.read_text(encoding="utf-8"))

    def _save_summary(self, submission_id: str, data: Dict[str, Any]) -> None:
        p = self._summary_path(submission_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _attach_scores(self, summary: Dict[str, Any], split: str) -> None:
        results = summary.get("results") or {}
        if not isinstance(results, dict):
            results = {}
        categories, total = _nyuctf_dataset_index(split)
        summary["scores"] = lh.compute_scores_from_results(
            results,
            dataset_categories=categories,
            dataset_total=total,
        )

    def init_submission(self, submission_id: str, metadata: LeaderboardSubmissionMetadata) -> Dict[str, Any]:
        sid = lh.submission_id_slug(submission_id)
        self._ensure_readme(sid)
        summary = self._load_summary(sid)
        summary["metadata"] = metadata.model_dump()
        summary.setdefault("results", {})
        self._attach_scores(summary, os.getenv("LEADERBOARD_SCORE_SPLIT", "test"))
        self._save_summary(sid, summary)
        return {"submission_id": sid, "summary": summary}

    def record_run(self, record: LeaderboardRunRecord) -> Dict[str, Any]:
        sid = lh.submission_id_slug(record.submission_id)
        self._ensure_readme(sid)
        summary = self._load_summary(sid)

        if record.metadata:
            existing_meta = summary.get("metadata") or {}
            if not existing_meta or not isinstance(existing_meta, dict):
                summary["metadata"] = record.metadata.model_dump()
            else:
                merged = {**existing_meta, **record.metadata.model_dump()}
                summary["metadata"] = merged
        elif not summary.get("metadata"):
            raise ValueError(
                "Call POST /leaderboards/init with metadata first, or include `metadata` on this record."
            )

        success: bool
        expected_flag: Optional[str] = None
        if record.success_override is not None:
            success = bool(record.success_override)
        elif record.benchmark == "nyuctf":
            expected_flag, _cat = _nyuctf_flag_and_category(record.split, record.challenge_id)
            if expected_flag is None:
                success = False
            else:
                success = lh.flag_match_in_outputs(
                    expected_flag,
                    record.submitted_goal,
                    record.auxiliary_outputs,
                )
        else:
            raise ValueError(
                "Non-nyuctf benchmarks require `success_override` until flag backend is added."
            )

        results = summary.get("results") or {}
        if not isinstance(results, dict):
            results = {}
        results[record.challenge_id] = success
        summary["results"] = results

        self._attach_scores(summary, record.split)

        transcript_doc: Dict[str, Any] = {
            "challenge_id": record.challenge_id,
            "benchmark": record.benchmark,
            "split": record.split,
            "timestamp": lh.utc_now_iso(),
            "success": success,
            "submitted_goal": record.submitted_goal,
            "expected_flag_present": expected_flag is not None,
            "transcript": record.transcript or {},
        }
        tpath = self.submission_dir(sid) / f"{record.challenge_id}.gencyber.json"
        tpath.write_text(json.dumps(transcript_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        self._save_summary(sid, summary)

        return {
            "submission_id": sid,
            "challenge_id": record.challenge_id,
            "success": success,
            "summary_path": str(self._summary_path(sid)),
            "transcript_path": str(tpath),
            "scores": summary.get("scores", {}),
        }

    def finalize_submission(
        self,
        submission_id: str,
        *,
        split: str = "test",
    ) -> Dict[str, Any]:
        """
        Fill ``results`` with all challenge ids from the split; missing ids → false
        (NYU expects all 200 test challenges listed).
        """
        sid = lh.submission_id_slug(submission_id)
        summary = self._load_summary(sid)
        if not summary.get("metadata"):
            raise ValueError("No summary yet — record at least one run or create metadata.")

        try:
            from nyuctf.dataset import CTFDataset
        except ImportError as e:
            raise RuntimeError("nyuctf package required to finalize") from e

        ds = CTFDataset(split=split)
        all_ids = sorted(ds.dataset.keys())
        merged = lh.merge_results_with_dataset(
            {str(k): bool(v) for k, v in (summary.get("results") or {}).items()},
            all_ids,
        )
        summary["results"] = merged
        summary["finalize"] = {"split": split, "at": lh.utc_now_iso()}
        self._attach_scores(summary, split)
        self._save_summary(sid, summary)
        return {"submission_id": sid, "summary": summary}

    def get_submission(self, submission_id: str, *, split_for_scores: str = "test") -> Dict[str, Any]:
        sid = lh.submission_id_slug(submission_id)
        summary = self._load_summary(sid)
        self._attach_scores(summary, split_for_scores)
        return {"submission_id": sid, "summary": summary}
