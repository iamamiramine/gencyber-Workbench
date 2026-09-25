from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Literal, Optional, Sequence

from domain.models.benchmark.challenge_model import Challenge


Split = Literal["development", "test", "bandit", "krypton"]


class BaseBenchmark(ABC):
    """
    Base class for all benchmarks.

    Implementations load a challenge using native identifiers and return a
    standardized Challenge model. Optional: provide file blobs via
    ``challenge_files_b64`` for **external** provisioning (this package does not upload them).
    """

    benchmark_id: str

    def supported_splits(self) -> Sequence[Split]:
        """Split names accepted by ``load_challenge`` / ``challenge_files_b64``."""
        return ("development", "test")

    @abstractmethod
    def load_challenge(self, split: Split, challenge_id: str) -> Challenge:
        raise NotImplementedError

    @abstractmethod
    def challenge_catalog(self, split: Split) -> List[Dict[str, Any]]:
        """
        Lightweight public index for a split (challenge ids and non-secret dataset fields).
        Used by orchestrators before calling ``load_challenge``.
        """
        raise NotImplementedError

    def challenge_files_b64(self, split: Split, challenge_id: str) -> List[Dict[str, Any]]:
        """Return [{"name", "base64"}, ...] for use by an external orchestrator. Default: none."""
        return []

    def provision_files_b64(self, split: Split, challenge_id: str) -> List[Dict[str, Any]]:
        """
        Files to send to the virtual environment on **prepare** (may be a superset of
        ``challenge_files_b64`` so docker compose volume paths exist on disk).
        """
        return self.challenge_files_b64(split=split, challenge_id=challenge_id)

    def challenge_docker_compose(self, split: Split, challenge_id: str) -> Optional[str]:
        """Optional ``docker-compose`` YAML for server-side challenges; default none."""
        return None
