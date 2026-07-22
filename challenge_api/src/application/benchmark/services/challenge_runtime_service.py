"""Start/stop NYU-style challenge docker compose stacks on the workbench host."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from application.benchmark.helpers import benchmark_helper as bh
from application.benchmark.helpers.docker_compose_patch import patch_compose_for_gencyber_network

logger = logging.getLogger(__name__)

_COMPOSE_NAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)
_PATCHED_COMPOSE = ".gencyber-compose.yml"
_PROJECT_PREFIX = "gencyber"


def _challenge_root() -> Path:
    raw = (os.environ.get("CHALLENGE_ROOT") or "/workspace/challenges").strip()
    return Path(raw)


def _resolve_written_root(
    benchmark: str,
    split: str,
    challenge_id: str,
    written_root: Optional[str],
) -> Path:
    if written_root and str(written_root).strip():
        return Path(str(written_root).strip())
    return _challenge_root() / benchmark / split / challenge_id


def _find_compose_file(chal_dir: Path) -> Optional[Path]:
    for name in _COMPOSE_NAMES:
        candidate = chal_dir / name
        if candidate.is_file():
            return candidate
    return None


def _project_name(benchmark: str, split: str, challenge_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", f"{benchmark}_{split}_{challenge_id}")[:48]
    return f"{_PROJECT_PREFIX}_{safe}"


def _discover_endpoints(compose_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Best-effort endpoint list from patched compose YAML."""
    out: List[Dict[str, Any]] = []
    services = compose_data.get("services")
    if not isinstance(services, dict):
        return out

    for svc_name, spec in services.items():
        if not isinstance(spec, dict):
            continue
        ports = spec.get("ports") or []
        aliases: List[str] = []
        networks = spec.get("networks")
        if isinstance(networks, dict):
            for net_spec in networks.values():
                if isinstance(net_spec, dict):
                    for a in net_spec.get("aliases") or []:
                        if isinstance(a, str):
                            aliases.append(a)
        host_aliases = aliases or [str(svc_name)]
        published: List[int] = []
        for p in ports:
            if isinstance(p, int):
                published.append(p)
            elif isinstance(p, str) and ":" in p:
                try:
                    published.append(int(p.split(":")[0]))
                except ValueError:
                    pass

        for host in host_aliases:
            port = published[0] if published else None
            url = None
            if port:
                url = f"http://{host}:{port}"
            out.append(
                {
                    "host": host,
                    "port": port,
                    "url": url,
                    "service": svc_name,
                    "source": "compose_alias" if host in aliases else "compose_service",
                }
            )
    return out


class ChallengeRuntimeService:
    def start_challenge_services(
        self,
        *,
        benchmark: str,
        split: bh.Split,
        challenge_id: str,
        written_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        chal_dir = _resolve_written_root(benchmark, split, challenge_id, written_root)
        compose_src = _find_compose_file(chal_dir)

        bench = bh.instantiate_benchmark(benchmark)
        if compose_src is None:
            raw = None
            if hasattr(bench, "challenge_docker_compose"):
                raw = bench.challenge_docker_compose(split=split, challenge_id=challenge_id)
            if raw:
                chal_dir.mkdir(parents=True, exist_ok=True)
                compose_src = chal_dir / "docker-compose.yml"
                compose_src.write_text(raw, encoding="utf-8")

        if compose_src is None or not compose_src.is_file():
            return {
                "status": "no_compose",
                "written_root": str(chal_dir),
                "endpoints": [],
            }

        raw_yaml = compose_src.read_text(encoding="utf-8", errors="replace")
        patched = patch_compose_for_gencyber_network(raw_yaml)
        patched_path = chal_dir / _PATCHED_COMPOSE
        patched_path.write_text(patched, encoding="utf-8")

        project = _project_name(benchmark, split, challenge_id)
        cmd = [
            "docker",
            "compose",
            "-f",
            str(patched_path),
            "-p",
            project,
            "up",
            "-d",
            "--force-recreate",
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(chal_dir),
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("GENCYBER_COMPOSE_UP_TIMEOUT", "600")),
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "error": "docker compose up timed out",
                "written_root": str(chal_dir),
                "project_name": project,
                "endpoints": [],
            }

        if proc.returncode != 0:
            return {
                "status": "error",
                "error": (proc.stderr or proc.stdout or "compose up failed")[:4000],
                "written_root": str(chal_dir),
                "project_name": project,
                "endpoints": [],
            }

        endpoints: List[Dict[str, Any]] = []
        try:
            data = yaml.safe_load(patched)
            if isinstance(data, dict):
                endpoints = _discover_endpoints(data)
        except yaml.YAMLError:
            pass

        return {
            "status": "started",
            "written_root": str(chal_dir),
            "project_name": project,
            "compose_file": str(patched_path),
            "endpoints": endpoints,
            "logs": (proc.stdout or "")[-2000:],
        }

    def stop_challenge_services(
        self,
        *,
        benchmark: str,
        split: bh.Split,
        challenge_id: str,
        written_root: Optional[str] = None,
        project_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        chal_dir = _resolve_written_root(benchmark, split, challenge_id, written_root)
        project = project_name or _project_name(benchmark, split, challenge_id)
        patched_path = chal_dir / _PATCHED_COMPOSE
        compose_file = patched_path if patched_path.is_file() else _find_compose_file(chal_dir)
        if compose_file is None:
            return {"status": "no_compose", "project_name": project}

        cmd = [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "-p",
            project,
            "down",
            "--volumes",
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(chal_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": "docker compose down timed out", "project_name": project}

        return {
            "status": "stopped" if proc.returncode == 0 else "error",
            "project_name": project,
            "error": None if proc.returncode == 0 else (proc.stderr or "")[:2000],
        }
