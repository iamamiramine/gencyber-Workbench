from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

_SKIP_TOP_FILES = frozenset({"challenge.json"})
_SKIP_DIR_PARTS = frozenset({".git", "__pycache__", ".venv", "node_modules"})
# Avoid provisioning multi-hundred-MB blobs by default (tune via env if needed).
_MAX_PROVISION_FILE_BYTES = 80 * 1024 * 1024


Split = Literal["development", "test"]


class NYUCTFRepository:
    """
    Infrastructure adapter around the external `nyuctf` package.
    Keeps benchmark-specific library calls out of controller/application/core logic.
    """

    def load_challenge(self, split: Split, challenge_id: str) -> Dict[str, Any]:
        from nyuctf.dataset import CTFDataset
        from nyuctf.challenge import CTFChallenge

        ds = CTFDataset(split=split)
        chal_obj = CTFChallenge(ds.get(challenge_id), ds.basedir)

        # Best-effort standardized extraction. Keep extra fields in `raw`.
        # NOTE: `canonical_name` exists on CTFChallenge and is used by their logging scripts.
        files = list(getattr(chal_obj, "files", []) or [])

        return {
            "canonical_name": getattr(chal_obj, "canonical_name", None) or challenge_id,
            "name": getattr(chal_obj, "name", None) or challenge_id,
            "category": getattr(chal_obj, "category", None),
            "points": getattr(chal_obj, "points", None),
            "description": getattr(chal_obj, "description", None),
            "flag_format": getattr(chal_obj, "flag_format", None),
            "server_type": getattr(chal_obj, "server_type", None),
            "server_name": getattr(chal_obj, "server_name", None),
            "port": getattr(chal_obj, "port", None),
            "files": files,
        }

    def list_challenge_index(self, split: Split) -> List[Dict[str, Any]]:
        """
        Rows from the split's ``*_dataset.json`` only (no ``challenge.json`` / flags).
        ``challenge_id`` keys match ``CTFDataset.get`` / ``ChallengeRequest.challenge_id``.
        """
        from nyuctf.dataset import CTFDataset

        ds = CTFDataset(split=split)
        rows: List[Dict[str, Any]] = []
        for challenge_id, entry in ds.dataset.items():
            if not isinstance(entry, dict):
                continue
            rows.append(
                {
                    "challenge_id": challenge_id,
                    "year": entry.get("year"),
                    "event": entry.get("event"),
                    "category": entry.get("category"),
                    "challenge": entry.get("challenge"),
                    "path": entry.get("path"),
                }
            )
        rows.sort(key=lambda r: str(r.get("challenge_id", "")))
        return rows

    _COMPOSE_FILENAMES = (
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    )

    def resolve_challenge_directory(self, split: Split, challenge_id: str) -> Optional[Path]:
        """On-disk challenge folder (contains challenge.json, compose, sources)."""
        from nyuctf.dataset import CTFDataset
        from nyuctf.challenge import CTFChallenge

        ds = CTFDataset(split=split)
        chal_obj = CTFChallenge(ds.get(challenge_id), ds.basedir)

        for attr in ("chaldir", "challenge_dir", "directory", "path"):
            value = getattr(chal_obj, attr, None)
            if value:
                return Path(str(value))
        entry = ds.get(challenge_id)
        if isinstance(entry, dict):
            for key in ("path", "chaldir", "directory"):
                if entry.get(key):
                    return Path(str(entry[key]))
        return None

    def read_docker_compose_text(self, split: Split, challenge_id: str) -> Optional[str]:
        """Return docker compose YAML if present (NYU CTF server challenges)."""
        chal_dir = self.resolve_challenge_directory(split, challenge_id)
        if chal_dir is None or not chal_dir.is_dir():
            return None
        for name in self._COMPOSE_FILENAMES:
            candidate = chal_dir / name
            if candidate.is_file():
                try:
                    return candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return None
        return None

    def read_challenge_files_b64(self, split: Split, challenge_id: str) -> List[Dict[str, Any]]:
        """Read on-disk challenge files as base64 payloads for sandbox provisioning."""
        from nyuctf.dataset import CTFDataset
        from nyuctf.challenge import CTFChallenge

        ds = CTFDataset(split=split)
        chal_obj = CTFChallenge(ds.get(challenge_id), ds.basedir)

        challenge_dir = self.resolve_challenge_directory(split, challenge_id)

        out: List[Dict[str, Any]] = []
        if challenge_dir is None:
            return out

        for raw_name in list(getattr(chal_obj, "files", []) or []):
            name = str(raw_name)
            candidate = challenge_dir / name
            if not candidate.exists():
                continue
            try:
                content = candidate.read_bytes()
            except Exception:
                continue
            out.append({
                "name": name,
                "base64": base64.b64encode(content).decode("ascii"),
            })
        return out

    def read_full_challenge_tree_for_provision(
        self,
        split: Split,
        challenge_id: str,
        *,
        max_bytes_per_file: int = _MAX_PROVISION_FILE_BYTES,
    ) -> List[Dict[str, Any]]:
        """
        Recursively package the challenge directory for the sandbox workspace so
        ``docker-compose`` bind mounts and relative paths match the NYU layout.

        Omits ``challenge.json`` (ground-truth flag). Skips bulky / dev dirs.
        """
        import os

        limit = max_bytes_per_file
        try:
            limit = int(os.environ.get("GENCYBER_PROVISION_MAX_FILE_BYTES", str(max_bytes_per_file)))
        except ValueError:
            pass

        chal_dir = self.resolve_challenge_directory(split, challenge_id)
        if chal_dir is None or not chal_dir.is_dir():
            return []

        out: List[Dict[str, Any]] = []
        try:
            paths = sorted(chal_dir.rglob("*"))
        except OSError:
            return out

        for path in paths:
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(chal_dir)
            except ValueError:
                continue
            parts = rel.parts
            if any(p in _SKIP_DIR_PARTS for p in parts):
                continue
            if rel.name in _SKIP_TOP_FILES:
                continue
            rel_posix = rel.as_posix()
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_size > limit:
                logger.warning(
                    "Skipping large file for provision (>%s bytes): %s",
                    limit,
                    rel_posix,
                )
                continue
            try:
                content = path.read_bytes()
            except OSError:
                continue
            out.append({
                "name": rel_posix,
                "base64": base64.b64encode(content).decode("ascii"),
            })
        return out
