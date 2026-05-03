from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from application.benchmark.helpers import benchmark_helper as bh
from application.benchmark.helpers.docker_compose_patch import patch_compose_for_gencyber_network
from application.sandbox.runtime import provision as sandbox_provision
from domain.models.benchmark.challenge_model import Challenge, TaskPayload

logger = logging.getLogger(__name__)

Split = bh.Split


def _provision_project_name(session_id: str, challenge_id: str) -> str:
    """Docker Compose project name safe for ``docker compose -p``."""

    def seg(s: str) -> str:
        t = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(s)).strip("._-")
        return (t[:48] or "x") if t else "x"

    return f"gencyber-{seg(session_id)}-{seg(challenge_id)}"


class BenchmarkService:
    """
    Benchmark loading, file payloads, task bundles, agent seed prompts, and Docker
    sandbox provisioning (workspace + compose + persistent shell in this service).
    Does not call gencyber-Agent or LangGraph.
    """

    def get_challenge(self, benchmark: str, split: Split, challenge_id: str) -> Dict[str, Any]:
        bench = bh.instantiate_benchmark(benchmark)
        challenge: Challenge = bench.load_challenge(split=split, challenge_id=challenge_id)
        return challenge.model_dump()

    def get_challenge_file_payloads(
        self, benchmark: str, split: Split, challenge_id: str
    ) -> Dict[str, Any]:
        """Return base64-encoded challenge files for external provisioning (benchmark data only)."""
        bench = bh.instantiate_benchmark(benchmark)
        payloads = bench.challenge_files_b64(split=split, challenge_id=challenge_id)
        return {"benchmark": bench.benchmark_id, "split": split, "challenge_id": challenge_id, "files": payloads}

    def get_task_payload(self, benchmark: str, split: Split, challenge_id: str, task: str) -> Dict[str, Any]:
        bench = bh.instantiate_benchmark(benchmark)
        challenge: Challenge = bench.load_challenge(split=split, challenge_id=challenge_id)
        payload = TaskPayload(
            benchmark=bench.benchmark_id,
            split=split,
            challenge=challenge,
            task=task,
        )
        return payload.model_dump()

    def get_benchmark_metadata(self, benchmark: str, split: Optional[Split] = None) -> Dict[str, Any]:
        """
        Return supported splits and per-split challenge index (ids + dataset-json fields).
        Does not load ``challenge.json`` or flags; safe for discovery before ``get_challenge``.
        """
        bench = bh.instantiate_benchmark(benchmark)
        supported = list(bench.supported_splits())
        if split is not None:
            if split not in supported:
                raise ValueError(
                    f"Unsupported split '{split}' for benchmark '{benchmark}'. Supported: {supported}"
                )
            splits: List[Split] = [split]
        else:
            splits = supported

        out: Dict[str, Any] = {
            "benchmark": bench.benchmark_id,
            "supported_splits": supported,
            "splits": {},
        }
        for sp in splits:
            challenges = bench.challenge_catalog(sp)
            out["splits"][sp] = {
                "challenge_count": len(challenges),
                "challenges": challenges,
            }
        return out

    def build_seed_prompt(self, benchmark: str, split: Split, challenge_id: str) -> str:
        challenge = self.get_challenge(benchmark, split, challenge_id)
        return bh.build_agent_seed_prompt(challenge)

    def prepare_execution_environment(
        self,
        *,
        benchmark: str,
        split: Split,
        challenge_id: str,
        session_id: str,
        mount_path: Optional[str] = None,
        timeout_seconds: float = 600.0,
    ) -> Dict[str, Any]:
        """
        Load challenge and file blobs, optional docker compose from the benchmark tree,
        provision the local Docker sandbox (files + ``docker compose up`` + shell container),
        return challenge + seed prompt.
        """
        bench = bh.instantiate_benchmark(benchmark)
        ch_model = bench.load_challenge(split=split, challenge_id=challenge_id)
        challenge = ch_model.model_dump()
        files = bench.provision_files_b64(split=split, challenge_id=challenge_id)
        if not isinstance(files, list):
            files = []

        hints = challenge.get("helpful_hints") if isinstance(challenge.get("helpful_hints"), dict) else {}
        resolved_mount = mount_path if mount_path else str(hints.get("files_path_in_container") or "~/ctf_files")

        host_aliases: List[Dict[str, Any]] = []
        server = challenge.get("server")
        if isinstance(server, dict):
            chost = server.get("host")
            if chost:
                entry: Dict[str, Any] = {"hostname": chost}
                if server.get("type") == "web":
                    # NYU-style compose often exposes the HTTP service as ``web``.
                    entry["service"] = "web"
                host_aliases.append(entry)

        compose_raw = bench.challenge_docker_compose(split, challenge_id)
        compose_yaml: Optional[str] = None
        if compose_raw and str(compose_raw).strip():
            try:
                compose_yaml = patch_compose_for_gencyber_network(compose_raw)
            except Exception as e:
                logger.warning("Compose network patch failed, using raw compose: %s", e)
                compose_yaml = compose_raw

        prov = sandbox_provision(
            session_id=session_id,
            files=files,
            mount_path=resolved_mount,
            host_aliases=host_aliases or None,
            compose_yaml=compose_yaml,
            project_name=_provision_project_name(session_id, challenge_id),
            timeout_seconds=timeout_seconds,
            benchmark=str(challenge.get("benchmark") or benchmark),
            split=str(challenge.get("split") or split),
            challenge_id=str(challenge.get("challenge_id") or challenge_id),
        )
        if not prov.get("success"):
            raise RuntimeError(f"Sandbox provision did not succeed: {prov}")

        seed = bh.build_agent_seed_prompt(challenge)
        if compose_yaml:
            seed = (
                f"{seed}\n\n### Challenge runtime (prepared for you)\n"
                "A **docker compose** project for this challenge is available under `/root/ctf_files` "
                "in your sandbox shell, and **`docker compose up -d`** was already run from the "
                "benchmark toolkit. Challenge containers are on the same Docker user-defined "
                "network as your shell, so you can reach the stated host/port (e.g. with `curl`) "
                "once services are healthy. Inspect `/root/ctf_files` for sources, "
                "`docker-compose.yaml`, and any logs if a service fails.\n"
            )
        return {
            "benchmark": challenge.get("benchmark"),
            "split": challenge.get("split"),
            "challenge_id": challenge.get("challenge_id"),
            "challenge": challenge,
            "seed_prompt": seed,
            "provision": prov,
            "files_count": len(files),
            "mount_path": resolved_mount,
            "compose_provided": bool(compose_yaml),
        }
