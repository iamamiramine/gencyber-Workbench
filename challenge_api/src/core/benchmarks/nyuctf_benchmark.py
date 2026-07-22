from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from core.benchmarks.base_benchmark import BaseBenchmark, Split
from domain.models.benchmark.challenge_model import (
    Challenge,
    Server,
    HelpfulHints,
)
from infrastructure.benchmarks.nyuctf_repository import NYUCTFRepository


class NYUCTFBenchmark(BaseBenchmark):
    """
    NYU CTF Bench loader.

    Adds NYU-specific extraction rules (canonical name, server fields, file locations),
    but returns a payload upstream.
    """

    benchmark_id = "nyuctf"

    def __init__(self) -> None:
        self.repo = NYUCTFRepository()

    def load_challenge(self, split: Split, challenge_id: str) -> Challenge:
        chal = self.repo.load_challenge(split=split, challenge_id=challenge_id)

        server = None
        if chal.get("server_type") == "nc":
            server = Server(
                type="nc",
                host=chal.get("server_name"),
                port=chal.get("port"),
                url=None,
                access_hint=f"nc {chal.get('server_name')} {chal.get('port')}",
            )
        elif chal.get("server_type") == "web":
            host = chal.get("server_name")
            port = chal.get("port")
            server = Server(
                type="web",
                host=host,
                port=port,
                url=f"http://{host}:{port}" if host and port else None,
                access_hint=f"curl http://{host}:{port}" if host and port else None,
            )

        files = chal.get("files") or []
        if isinstance(files, str):
            files = [files]

        hints = HelpfulHints(
            files_path_in_container="/root/ctf_files",
            suggested_first_commands=[
                "ls -la /root/ctf_files",
                "file /root/ctf_files/* 2>/dev/null || true",
            ],
        )
        if server and server.access_hint:
            hints.suggested_first_commands.insert(0, server.access_hint)

        return Challenge(
            benchmark=self.benchmark_id,
            split=split,
            challenge_id=chal.get("canonical_name") or challenge_id,
            name=chal.get("name") or challenge_id,
            category=chal.get("category"),
            points=chal.get("points"),
            description=chal.get("description") or "",
            flag_format=chal.get("flag_format"),
            server=server,
            files=files,
            helpful_hints=hints,
            raw=chal,
        )

    def challenge_files_b64(self, split: Split, challenge_id: str) -> List[Dict[str, Any]]:
        return self.repo.read_challenge_files_b64(split=split, challenge_id=challenge_id)

    def provision_files_b64(self, split: Split, challenge_id: str) -> List[Dict[str, Any]]:
        """Agent-facing inputs only: the ``challenge.json`` manifest files (allow-list).

        Previously this returned the *full* challenge tree, which leaked the flag through
        README/Dockerfile/setup-script/source files. NYU CTF servers run from a prebuilt
        ``image:`` (no ``build:`` context on disk), so only the declared inputs are needed
        by the agent; the compose file for server start-up is materialized separately by
        :meth:`NYUCTFRepository.copy_challenge_tree_to_workspace`.
        """
        return self.challenge_files_b64(split=split, challenge_id=challenge_id)

    def challenge_docker_compose(self, split: Split, challenge_id: str) -> Optional[str]:
        return self.repo.read_docker_compose_text(split=split, challenge_id=challenge_id)

    def challenge_catalog(self, split: Split) -> List[Dict[str, Any]]:
        return self.repo.list_challenge_index(split=split)
