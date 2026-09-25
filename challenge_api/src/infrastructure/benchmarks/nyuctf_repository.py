from __future__ import annotations

import base64
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_SKIP_TOP_FILES = frozenset({"challenge.json"})
# A ``hints`` directory in the NYU CTF tree only ever holds author walkthroughs
# (solution.md / implementation.md that literally print the flag), so it is skipped
# wholesale alongside the usual VCS / build-cache dirs.
_SKIP_DIR_PARTS = frozenset(
    {".git", "__pycache__", ".venv", "node_modules", "hints"}
)
# Avoid provisioning multi-hundred-MB blobs by default (tune via env if needed).
_MAX_PROVISION_FILE_BYTES = 80 * 1024 * 1024

# Solution / answer-key leak filtering -------------------------------------------------
#
# NYU CTF challenge folders ship the author's solution next to the real artifacts:
# solve scripts (solver.py / solve.py / solution.c / exploit.py), markdown writeups,
# and plaintext ``flag`` files holding the ground-truth flag. Materializing those lets
# an agent short-circuit the task by reading the answer instead of recovering it, which
# silently inflates benchmark success. We drop them at copy time.
#
# Matching is generic (conventional file *stems*, never a challenge or file name) and is
# always overridden by ``challenge.json``'s ``files`` list: anything the challenge
# explicitly declares as an input is a legitimate artifact and is never dropped — so a
# rev/crypto binary that embeds the flag by design still reaches the agent.
_SOLUTION_STEMS = frozenset(
    {"solution", "solutions", "solve", "solver", "exploit", "writeup", "write-up", "writeups"}
)
_FLAG_BASENAMES = frozenset({"flag", "flag.txt"})


def _strip_solutions_enabled() -> bool:
    flag = (os.environ.get("GENCYBER_STRIP_SOLUTIONS", "1") or "").strip().lower()
    return flag not in ("0", "false", "no", "off")


def _load_declared_files_and_flag(chal_dir: Path) -> Tuple[Set[str], str]:
    """Return (declared input rel-paths, ground-truth flag) from ``challenge.json``.

    The declared ``files`` list is the authoritative allow-list of challenge inputs; we
    use it to protect legitimate artifacts from the solution filter. Best-effort: a
    missing/invalid manifest yields an empty allow-list and no flag.
    """
    declared: Set[str] = set()
    gt_flag = ""
    cj = chal_dir / "challenge.json"
    try:
        data = json.loads(cj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return declared, gt_flag
    if isinstance(data, dict):
        gt_flag = str(data.get("flag") or "").strip()
        for f in data.get("files", []) or []:
            declared.add(os.path.normpath(str(f).lstrip("./")).replace("\\", "/"))
    return declared, gt_flag


def _is_solution_leak(
    rel: Path,
    chal_dir: Path,
    *,
    declared: Set[str],
    gt_flag: str,
) -> bool:
    """True if ``rel`` is solution / answer-key material that must not be materialized.

    A file explicitly declared in ``challenge.json`` is always treated as a legitimate
    input (never a leak). Otherwise a file is a leak when its stem is a conventional
    solution name, or when it is a plaintext ``flag``/``flag.txt`` whose contents carry
    the real ground-truth flag (placeholder flag files used by local servers are kept).
    """
    if not _strip_solutions_enabled():
        return False
    if rel.as_posix() in declared:
        return False  # declared challenge input — always legitimate
    name = rel.name.lower()
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if stem in _SOLUTION_STEMS:
        return True
    if name in _FLAG_BASENAMES and gt_flag:
        try:
            content = (chal_dir / rel).read_bytes()
        except OSError:
            content = b""
        if gt_flag.encode("utf-8", "ignore") in content:
            return True
    return False


# The docker-compose filenames the workbench understands. NYU CTF server challenges
# reference a prebuilt ``image:`` (verified: no dev-split compose uses ``build:`` or a
# local bind mount), so the compose YAML is all that is needed to boot the server — the
# build tree (Dockerfile / src) is never required on disk and, crucially, never reaches
# the agent workspace.
_COMPOSE_FILENAMES: Tuple[str, ...] = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)


def _redact_flag_in_text(text: str, gt_flag: str) -> str:
    """Replace the plaintext ground-truth flag with ``${GENCYBER_FLAG}`` so an agent
    reading a materialized docker-compose can't simply grep it out of its own workspace.
    Some NYU server challenges embed the flag as a plaintext ``FLAG=`` env var in the
    compose; the real value is re-injected into the service container's environment only
    at ``docker compose up`` time (see ChallengeRuntimeService.start_challenge_services).
    """
    if not gt_flag:
        return text
    return text.replace(gt_flag, "${GENCYBER_FLAG}")


def materialize_allowlisted_files(
    chal_dir: Path,
    target_base: Path,
    *,
    compose_filenames: Tuple[str, ...] = _COMPOSE_FILENAMES,
) -> int:
    """Copy ONLY the agent-facing challenge inputs from ``chal_dir`` into ``target_base``.

    NYU CTF's ``challenge.json['files']`` is the *authoritative allow-list* of what the
    solver is meant to receive. Everything else in the on-disk challenge folder is
    server-build / author infrastructure that must never reach the agent:

      * ``README.md`` — ships the flag and/or a full solution walkthrough in plaintext,
      * ``Dockerfile`` / ``src/`` — bake the flag into the prebuilt server image,
      * ``mysql-setup.sh`` and similar seeds — embed the flag in the DB,
      * author ``solve``/``solver``/``test_solver``/writeup files.

    Copying the whole tree (the previous behaviour) leaked the flag through all of the
    above. Here we copy exactly the declared files (verbatim — a rev/crypto binary that
    embeds the flag by design is a legitimate input and still reaches the agent) plus the
    docker-compose file, which the workbench needs to start the prebuilt server and which
    carries no flag. Returns the number of files copied.
    """
    if chal_dir is None or not chal_dir.is_dir():
        return 0
    declared, _gt_flag = _load_declared_files_and_flag(chal_dir)

    if target_base.exists():
        shutil.rmtree(target_base)
    target_base.mkdir(parents=True, exist_ok=True)

    count = 0
    # 1) Declared challenge inputs (the allow-list), preserving any sub-directory layout.
    for rel in sorted(declared):
        src = chal_dir / rel
        if not src.is_file():
            continue
        dest = target_base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        count += 1

    # 2) The docker-compose file so start-challenge-services can boot the prebuilt
    #    ``image:`` server. No build context is copied because none is needed. Some NYU
    #    server challenges embed the ground-truth flag as a plaintext ``FLAG=`` env var in
    #    this compose, so redact it to ``${GENCYBER_FLAG}`` before it reaches the agent
    #    workspace; the real value is injected into the container env at launch time.
    for name in compose_filenames:
        src = chal_dir / name
        if src.is_file():
            try:
                text = src.read_text(encoding="utf-8", errors="replace")
                (target_base / name).write_text(
                    _redact_flag_in_text(text, _gt_flag), encoding="utf-8"
                )
            except OSError:
                shutil.copy2(src, target_base / name)
            count += 1
            break

    return count


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

        Omits ``challenge.json`` (ground-truth flag) and author solution / answer-key
        files (see :func:`_is_solution_leak`). Skips bulky / dev dirs.
        """
        limit = max_bytes_per_file
        try:
            limit = int(os.environ.get("GENCYBER_PROVISION_MAX_FILE_BYTES", str(max_bytes_per_file)))
        except ValueError:
            pass

        chal_dir = self.resolve_challenge_directory(split, challenge_id)
        if chal_dir is None or not chal_dir.is_dir():
            return []

        declared, gt_flag = _load_declared_files_and_flag(chal_dir)

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
            if _is_solution_leak(rel, chal_dir, declared=declared, gt_flag=gt_flag):
                logger.info("Omitting solution/answer-key file from provision: %s", rel.as_posix())
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

    def copy_challenge_tree_to_workspace(
        self,
        split: Split,
        challenge_id: str,
        target_base: Path,
    ) -> int:
        """
        Fast path: materialize the agent-facing challenge inputs into ``target_base``
        without base64 round-tripping through Python.

        Copies ONLY the ``challenge.json`` allow-list (declared ``files``) plus the
        docker-compose file — never the whole on-disk tree, which leaks the flag through
        README/Dockerfile/setup-script/source files. See
        :func:`materialize_allowlisted_files` for the rationale.
        """
        chal_dir = self.resolve_challenge_directory(split, challenge_id)
        return materialize_allowlisted_files(
            chal_dir, target_base, compose_filenames=self._COMPOSE_FILENAMES
        )
