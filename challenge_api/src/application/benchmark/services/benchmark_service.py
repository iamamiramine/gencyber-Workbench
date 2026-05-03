from __future__ import annotations

import base64
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from application.benchmark.helpers import benchmark_helper as bh
from domain.models.benchmark.challenge_model import Challenge, TaskPayload

logger = logging.getLogger(__name__)

Split = bh.Split


def _materialize_files(workspace: Path, files: List[Dict[str, Any]]) -> int:
    count = 0
    for entry in files:
        if not entry or not entry.get("name"):
            continue
        rel = str(entry["name"]).lstrip("/")
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(entry.get("base64"), str):
            target.write_bytes(base64.b64decode(entry["base64"]))
            count += 1
        elif isinstance(entry.get("content"), str):
            target.write_text(entry["content"], encoding="utf-8")
            count += 1
        elif entry.get("url"):
            url = str(entry["url"])
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=120) as resp:
                target.write_bytes(resp.read())
            count += 1
        else:
            raise ValueError(f"File entry '{entry.get('name')}' must include base64, content, or url")
    return count


class BenchmarkService:
    """
    Challenge loading and writing files to ``CHALLENGE_ROOT`` (workbench).
    No Docker sandbox, no agent calls.
    """

    def get_challenge(self, benchmark: str, split: Split, challenge_id: str) -> Dict[str, Any]:
        bench = bh.instantiate_benchmark(benchmark)
        challenge: Challenge = bench.load_challenge(split=split, challenge_id=challenge_id)
        return challenge.model_dump()

    def get_challenge_file_payloads(
        self, benchmark: str, split: Split, challenge_id: str
    ) -> Dict[str, Any]:
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

    def materialize_challenge(
        self,
        *,
        benchmark: str,
        split: Split,
        challenge_id: str,
        relative_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        root = (os.environ.get("CHALLENGE_ROOT") or "/app/challenge_data").strip() or "/app/challenge_data"
        bench = bh.instantiate_benchmark(benchmark)
        ch_model = bench.load_challenge(split=split, challenge_id=challenge_id)
        files = bench.provision_files_b64(split=split, challenge_id=challenge_id)
        if not isinstance(files, list):
            files = []

        if relative_path and str(relative_path).strip():
            rel = str(relative_path).strip().lstrip("/")
        else:
            rel = f"{bench.benchmark_id}/{split}/{ch_model.challenge_id}"

        target_base = Path(root) / rel
        target_base.mkdir(parents=True, exist_ok=True)
        count = _materialize_files(target_base, files)

        challenge_dict = ch_model.model_dump()
        hints = dict(challenge_dict.get("helpful_hints") or {})
        hints["files_path_in_container"] = str(target_base)
        prev_sugg = [str(x) for x in (hints.get("suggested_first_commands") or []) if x]
        hints["suggested_first_commands"] = [
            f"ls -la {target_base}",
            f"file {target_base}/* 2>/dev/null || true",
        ] + [x for x in prev_sugg if x != f"ls -la {target_base}"][:8]
        challenge_dict["helpful_hints"] = hints

        seed = bh.build_agent_seed_prompt(challenge_dict)
        seed = (
            f"{seed}\n\n### Workbench\n"
            f"Challenge files were written under **`{target_base}`** on the shared workbench volume. "
            "The terminal session defaults to `/workspace`; use `cd` as needed. "
            "Start any challenge Docker services yourself if the task includes a compose file.\n"
        )

        return {
            "benchmark": ch_model.benchmark,
            "split": split,
            "challenge_id": ch_model.challenge_id,
            "written_root": str(target_base),
            "files_count": count,
            "challenge": challenge_dict,
            "seed_prompt": seed,
        }
