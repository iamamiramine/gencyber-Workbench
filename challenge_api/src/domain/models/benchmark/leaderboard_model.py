from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from domain.models.benchmark.challenge_model import Split


class LeaderboardSubmissionMetadata(BaseModel):
    """Metadata block for ``summary.json`` (NYU leaderboard guidelines)."""

    agent: str = Field(description="Agent name")
    comment: str = Field(default="", description='e.g. "pass@1"')
    model: str = Field(description="Exact model string, e.g. gpt-4o-2024-11-20")
    link: str = Field(default="", description="Repository or documentation URL")
    date: str = Field(description='Submission date "YYYY/MM/DD"')


class LeaderboardInitRequest(BaseModel):
    """Create ``summary.json`` skeleton with metadata before the first challenge run."""

    submission_id: str = Field(min_length=1, max_length=120)
    metadata: LeaderboardSubmissionMetadata


class LeaderboardRunRecord(BaseModel):
    """
    One challenge run from the gencyber-Agent workflow (or any client).
    Flag verification uses nyuctf ground truth when ``benchmark`` is ``nyuctf`` and
    ``success_override`` is not set.
    """

    submission_id: str = Field(
        description="Folder name under transcripts/, e.g. gencyber_gpt4o_may2026",
        min_length=1,
        max_length=120,
    )
    benchmark: str = Field(default="nyuctf")
    split: Split = Field(description="Dataset split used for this challenge (matches CTFDataset)")
    challenge_id: str = Field(min_length=1)
    submitted_goal: Optional[str] = Field(
        default=None,
        description="Agent submission (e.g. graph ``submitted_goal``)",
    )
    auxiliary_outputs: List[str] = Field(
        default_factory=list,
        description="Optional extra strings to scan for the flag (e.g. llm_output snippets)",
    )
    transcript: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional full transcript / checkpoint payload (stored per challenge file)",
    )
    metadata: Optional[LeaderboardSubmissionMetadata] = Field(
        default=None,
        description="If set, merged into summary.json on first write for this submission_id",
    )
    success_override: Optional[bool] = Field(
        default=None,
        description="If set, skips automatic flag verification (other benchmarks)",
    )
