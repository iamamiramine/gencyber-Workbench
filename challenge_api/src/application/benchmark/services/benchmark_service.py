from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from application.benchmark.helpers import benchmark_helper as bh
from domain.models.benchmark.challenge_model import Challenge, TaskPayload

logger = logging.getLogger(__name__)

Split = bh.Split

_MATERIALIZE_MARKER = ".gencyber-materialized.json"
# Session→challenge bindings registered at materialize time so the workbench can
# later validate a submitted flag for a given agent session without the agent ever
# holding the ground-truth flag or challenge identity. One JSON file per session
# under CHALLENGE_ROOT.
_SESSIONS_DIRNAME = ".gencyber-sessions"


def _challenge_root() -> Path:
    raw = (os.environ.get("CHALLENGE_ROOT") or "/workspace/challenges").strip()
    return Path(raw or "/workspace/challenges")


def _safe_session_key(session_id: str) -> str:
    """Reduce a session id to a safe single-path-segment filename stem."""
    return "".join(c for c in str(session_id) if c.isalnum() or c in ("-", "_")).strip(
        "-_"
    ) or "session"


def _sessions_dir() -> Path:
    return _challenge_root() / _SESSIONS_DIRNAME


def _reset_workspace_scratch() -> None:
    """Delete prior-run agent scratch from the /workspace volume, keeping only the
    challenge tree.

    The agent works out of the /workspace volume ROOT (its persistent terminal PTY
    starts there) and litters it with scratch: compiled binaries, ``decrypted_*.txt``,
    keystreams, and its ``write_script`` output under ``.gencyber-agent-scripts/``.
    That volume survives across runs, so a later challenge boots staring at the
    previous run's artifacts (e.g. a stale ``decrypted_flag.txt``) and gets misled into
    "finishing" work it never did. We wipe everything under the workspace root EXCEPT
    the challenge tree (which holds the freshly materialized inputs and the session
    bindings) at the start of every materialize — mirroring the per-challenge dir reset
    in :meth:`materialize_challenge`.

    The workspace root is the PARENT of ``CHALLENGE_ROOT``. Heavily guarded: it refuses
    to operate on a filesystem root or a single-segment path, and never touches the
    challenge dir itself. Best-effort — an un-removable entry is logged, never fatal.
    Disable with ``GENCYBER_WORKSPACE_RESET=0``.
    """
    flag = (os.environ.get("GENCYBER_WORKSPACE_RESET", "1") or "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return
    challenge_root = _challenge_root().resolve()
    workspace = challenge_root.parent
    # Never let a misconfigured CHALLENGE_ROOT turn this into ``rmtree('/')``.
    if workspace == workspace.parent or len(workspace.parts) < 2:
        logger.warning("workspace reset skipped: unsafe workspace root %s", workspace)
        return
    if not workspace.is_dir():
        return
    removed = 0
    for entry in workspace.iterdir():
        if entry.resolve() == challenge_root:
            continue  # keep the (just-materialized) challenge tree + session bindings
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError as e:  # pragma: no cover - best effort
            logger.warning("workspace reset could not remove %s: %s", entry, e)
    logger.info(
        "Reset workspace scratch: removed %s entries under %s (kept %s/)",
        removed,
        workspace,
        challenge_root.name,
    )


def _write_session_binding(
    session_id: str,
    *,
    benchmark: str,
    split: str,
    challenge_id: str,
) -> None:
    if not session_id or not str(session_id).strip():
        return
    sdir = _sessions_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{_safe_session_key(session_id)}.json"
    path.write_text(
        json.dumps(
            {
                "session_id": str(session_id),
                "benchmark": benchmark,
                "split": split,
                "challenge_id": challenge_id,
            }
        ),
        encoding="utf-8",
    )


def _read_session_binding(session_id: str) -> Optional[Dict[str, Any]]:
    if not session_id or not str(session_id).strip():
        return None
    path = _sessions_dir() / f"{_safe_session_key(session_id)}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_materialize_marker(
    target_base: Path,
    *,
    benchmark: str,
    split: str,
    challenge_id: str,
    files_count: int,
) -> None:
    marker = target_base / _MATERIALIZE_MARKER
    marker.write_text(
        json.dumps(
            {
                "benchmark": benchmark,
                "split": split,
                "challenge_id": challenge_id,
                "files_count": files_count,
            }
        ),
        encoding="utf-8",
    )


def _build_materialize_response(
    *,
    ch_model: Challenge,
    split: Split,
    target_base: Path,
    files_count: int,
    skipped: bool = False,
) -> Dict[str, Any]:
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
        "For workflow runs, challenge Docker services are started automatically when a compose file exists.\n"
    )
    if skipped:
        seed += "\n*(Files were already materialized; skipped re-copy.)*\n"

    return {
        "benchmark": ch_model.benchmark,
        "split": split,
        "challenge_id": ch_model.challenge_id,
        "written_root": str(target_base),
        "files_count": files_count,
        "challenge": challenge_dict,
        "seed_prompt": seed,
        "materialize_skipped": skipped,
    }


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


# NYU CTF challenge READMEs ship the ground-truth flag and a full walkthrough in
# plaintext (## Flag / ## Solution) right next to the real artifacts. Materializing
# them lets an agent short-circuit the task by reading the flag instead of recovering
# it, which silently inflates benchmark success. We strip those sections at copy time.
_README_LEAK_SECTIONS = frozenset({"flag", "solution"})
_MD_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")


def _strip_readme_leak_sections(readme_path: Path) -> bool:
    """Remove ``## Flag`` / ``## Solution`` sections from a markdown README in place.

    Drops each leaking section's header and its body up to the next markdown header,
    leaving the legitimate briefing (e.g. ``## Description``) and every other section
    intact. Matching is on the section *title* only — never on a challenge or file
    name — so it stays generic across the benchmark. Returns True if the file changed.
    """
    try:
        text = readme_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    kept: List[str] = []
    skipping = False
    changed = False
    for line in text.splitlines(keepends=True):
        header = _MD_HEADER_RE.match(line)
        if header:
            title = header.group(1).strip().lower()
            if title in _README_LEAK_SECTIONS:
                skipping = True
                changed = True
                continue
            skipping = False  # a non-leaking header closes any open skip
        if skipping:
            changed = True
            continue
        kept.append(line)

    if changed:
        try:
            readme_path.write_text("".join(kept), encoding="utf-8")
        except OSError:
            return False
    return changed


def _strip_nyuctf_readme_leaks(target_base: Path) -> None:
    """Strip flag/solution leaks from every README.md under a materialized nyuctf dir."""
    if not target_base.is_dir():
        return
    for readme in target_base.rglob("*"):
        if readme.is_file() and readme.name.lower() == "readme.md":
            if _strip_readme_leak_sections(readme):
                logger.info("Stripped Flag/Solution sections from NYU CTF README %s", readme)


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

    def resolve_ground_truth_flag(
        self, benchmark: str, split: Split, challenge_id: str
    ) -> Optional[str]:
        """Load canonical flag from benchmark dataset (never exposed in materialize payloads)."""
        if benchmark != "nyuctf":
            return None
        from nyuctf.dataset import CTFDataset
        from nyuctf.challenge import CTFChallenge

        ds = CTFDataset(split=split)
        chal_obj = CTFChallenge(ds.get(challenge_id), ds.basedir)
        flag = getattr(chal_obj, "flag", None)
        return str(flag) if flag else None

    def append_services_to_seed(
        self, seed: str, endpoints: List[Dict[str, Any]], *, status: str
    ) -> str:
        if status != "started" or not endpoints:
            return seed
        lines = ["\n\n### Challenge services (running)", ""]
        for ep in endpoints:
            url = ep.get("url")
            host = ep.get("host")
            if url:
                lines.append(f"- {url}")
            elif host:
                lines.append(f"- host: `{host}` (Docker network DNS)")
        lines.append("")
        lines.append(
            "Reach these hosts from the agent shell on the shared Docker network "
            "(e.g. `curl http://web.chal.csaw.io:80`)."
        )
        return seed + "\n".join(lines)

    def validate_submission(
        self, *, session_id: str, candidate: str
    ) -> Dict[str, Any]:
        """Check ``candidate`` against the challenge bound to ``session_id``.

        Mirrors the nyuctf_agents ``CheckFlag`` tool — an exact comparison against
        the challenge's ground-truth flag (with surrounding whitespace stripped) —
        but the comparison lives here, where the challenge (and therefore the flag)
        does. Returns ``{accepted: bool, reason: Optional[str]}`` and never raises
        for an ordinary miss; the caller always gets a verdict.
        """
        binding = _read_session_binding(session_id)
        if not binding:
            return {
                "accepted": False,
                "reason": "no challenge bound to this session",
            }

        benchmark = str(binding.get("benchmark") or "")
        split = str(binding.get("split") or "")
        challenge_id = str(binding.get("challenge_id") or "")
        if not (benchmark and split and challenge_id):
            return {"accepted": False, "reason": "incomplete session binding"}

        try:
            expected = self.resolve_ground_truth_flag(
                benchmark=benchmark, split=split, challenge_id=challenge_id
            )
        except Exception:
            logger.exception(
                "Failed to resolve ground-truth flag for session %s", session_id
            )
            expected = None

        if not expected:
            return {
                "accepted": False,
                "reason": "no ground-truth flag available for this challenge",
            }

        if str(candidate or "").strip() == str(expected).strip():
            return {"accepted": True, "reason": None}
        return {"accepted": False, "reason": "incorrect flag"}

    def materialize_challenge(
        self,
        *,
        benchmark: str,
        split: Split,
        challenge_id: str,
        relative_path: Optional[str] = None,
        force: bool = False,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        root = _challenge_root()
        bench = bh.instantiate_benchmark(benchmark)
        ch_model = bench.load_challenge(split=split, challenge_id=challenge_id)

        if relative_path and str(relative_path).strip():
            rel = str(relative_path).strip().lstrip("/")
        else:
            rel = f"{bench.benchmark_id}/{split}/{ch_model.challenge_id}"

        # Bind this session to the challenge so /validate-submission can later check a
        # submitted flag for it. Done regardless of the materialize-skip fast path so a
        # re-run with a fresh session still gets a binding.
        if session_id:
            _write_session_binding(
                session_id,
                benchmark=benchmark,
                split=split,
                challenge_id=ch_model.challenge_id,
            )

        # Wipe the agent's prior-run scratch from the /workspace volume root (compiled
        # binaries, decrypted_*.txt, keystreams, .gencyber-agent-scripts/, …). The
        # challenge tree itself — including the session bindings written just above —
        # is preserved. This stops a new run from booting into a previous run's litter.
        _reset_workspace_scratch()

        # Isolate the challenge root so the agent only ever sees the ONE challenge it is
        # currently solving. Earlier materializes (this or other splits) left their
        # challenge trees behind under CHALLENGE_ROOT, so `ls challenges/<benchmark>/...`
        # leaked the entire dataset — the agent would wander into sibling challenges and
        # even submit their flags. We remove every entry under CHALLENGE_ROOT except the
        # session-bindings dir; the current challenge is (re)copied pristine just below.
        # Guarded to only ever delete children strictly under CHALLENGE_ROOT.
        resolved_root = root.resolve()
        if resolved_root != Path("/") and resolved_root.is_dir():
            for child in resolved_root.iterdir():
                if child.name == _SESSIONS_DIRNAME:
                    continue
                try:
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                except Exception:
                    logger.warning(
                        "challenge isolation: could not purge %s", child, exc_info=True
                    )

        target_base = root / rel
        # Reset the challenge directory to a pristine copy on every materialize so a
        # prior run's agent-generated files (decrypted_flag.txt, scratch outputs, …)
        # can never contaminate the inputs of a later run. We deliberately drop the
        # old marker-based "skip re-copy" fast path: skipping reused whatever junk the
        # previous run left behind. Challenge trees are small, so re-copying is cheap.
        # rmtree is guarded to fire only on a directory strictly *under* the challenge
        # root, never the root itself.
        resolved_root = root.resolve()
        resolved_target = target_base.resolve()
        if (
            resolved_target != resolved_root
            and resolved_root in resolved_target.parents
            and target_base.exists()
        ):
            shutil.rmtree(target_base, ignore_errors=True)
            logger.info(
                "Reset challenge dir to pristine for %s/%s/%s (%s)",
                benchmark,
                split,
                ch_model.challenge_id,
                target_base,
            )

        count = 0
        if benchmark == "nyuctf" and hasattr(bench, "repo"):
            from infrastructure.benchmarks.nyuctf_repository import NYUCTFRepository

            repo = getattr(bench, "repo", None) or NYUCTFRepository()
            count = repo.copy_challenge_tree_to_workspace(split, challenge_id, target_base)
        if count == 0:
            files = bench.provision_files_b64(split=split, challenge_id=challenge_id)
            if not isinstance(files, list):
                files = []
            target_base.mkdir(parents=True, exist_ok=True)
            count = _materialize_files(target_base, files)

        # NYU CTF is the only benchmark whose challenge tree ships the flag and a full
        # walkthrough in README.md; remove those so the agent must actually recover it.
        if benchmark == "nyuctf":
            _strip_nyuctf_readme_leaks(target_base)

        _write_materialize_marker(
            target_base,
            benchmark=benchmark,
            split=split,
            challenge_id=ch_model.challenge_id,
            files_count=count,
        )
        logger.info(
            "Materialized %s files for %s/%s/%s -> %s",
            count,
            benchmark,
            split,
            ch_model.challenge_id,
            target_base,
        )
        return _build_materialize_response(
            ch_model=ch_model,
            split=split,
            target_base=target_base,
            files_count=count,
            skipped=False,
        )
