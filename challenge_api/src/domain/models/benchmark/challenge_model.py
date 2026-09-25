from __future__ import annotations

from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel, Field


ServerType = Literal["nc", "web"]
# "development"/"test" = NYU CTF splits; "bandit"/"krypton" = OverTheWire games.
Split = Literal["development", "test", "bandit", "krypton"]


class Server(BaseModel):
    type: ServerType
    host: Optional[str] = None
    port: Optional[int] = None
    url: Optional[str] = None
    access_hint: Optional[str] = None


class HelpfulHints(BaseModel):
    files_path_in_container: str = Field(default="~/ctf_files")
    suggested_first_commands: List[str] = Field(default_factory=list)


class Challenge(BaseModel):
    """
    Benchmark challenge payload returned by the API.
    """

    benchmark: str
    split: Split

    challenge_id: str
    name: str
    category: Optional[str] = None
    points: Optional[int] = None

    description: str
    flag_format: Optional[str] = None

    server: Optional[Server] = None
    files: List[str] = Field(default_factory=list)

    helpful_hints: HelpfulHints = Field(default_factory=HelpfulHints)

    raw: Dict[str, Any] = Field(default_factory=dict, description="Raw benchmark-native metadata (best effort)")


class TaskPayload(BaseModel):
    benchmark: str
    split: Split
    challenge: Challenge
    task: str


# --- API request bodies (challenge loading) ---


class ChallengeRequest(BaseModel):
    benchmark: str = Field(default="nyuctf", description="Benchmark identifier (e.g. nyuctf)")
    split: Split = Field(default="development")
    challenge_id: str = Field(description="Benchmark-specific challenge id")


class TaskRequest(ChallengeRequest):
    task: str = Field(description="Task text to bundle with the challenge (defined by the external orchestrator)")


class MetadataRequest(BaseModel):
    """Discover challenge ids and index fields before calling ``/challenge`` or ``/task``."""

    benchmark: str = Field(default="nyuctf", description="Benchmark identifier (e.g. nyuctf)")
    split: Optional[Split] = Field(
        default=None,
        description="If set, only this split; if omitted, all supported splits for the benchmark",
    )


class MaterializeRequest(ChallengeRequest):
    """Write challenge files under ``CHALLENGE_ROOT`` for the shared workbench volume."""

    relative_path: Optional[str] = Field(
        default=None,
        description="Optional path under CHALLENGE_ROOT; default benchmark/split/challenge_id",
    )
    force: bool = Field(
        default=False,
        description="Re-copy files even if this challenge was already materialized",
    )
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "Agent run/session id to bind this materialized challenge to. When set, the "
            "workbench records a session→challenge binding so it can later validate a "
            "submitted flag for this session via /validate-submission, without the agent "
            "ever holding the ground-truth flag or challenge identity."
        ),
    )


class ValidateSubmissionRequest(BaseModel):
    """Validate a submitted flag against the challenge bound to ``session_id``.

    The agent holds no ground-truth flag and no challenge identity; it POSTs the
    candidate plus its session id, and the workbench (which owns challenge
    materialization and therefore the ground truth) returns the verdict.
    """

    session_id: str = Field(description="Agent run/session id used at materialize time")
    candidate: str = Field(description="The submitted flag to check")


class ChallengeServicesRequest(ChallengeRequest):
    written_root: Optional[str] = Field(
        default=None,
        description="Materialized challenge directory (default under CHALLENGE_ROOT)",
    )
    project_name: Optional[str] = Field(
        default=None,
        description="Docker compose project name (for stop)",
    )


class ResolveFlagRequest(ChallengeRequest):
    """Server-only ground-truth flag resolution (agent service, not browser UI)."""

