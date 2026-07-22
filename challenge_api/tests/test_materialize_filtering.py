"""Unit tests for per-run materialize hygiene (no docker, no nyuctf dataset).

Two independent hardening layers are covered:

  1. Solution / answer-key leak filtering in the NYU CTF repository copy paths
     (``_is_solution_leak`` + ``_load_declared_files_and_flag``): author solve
     scripts, writeups and real-flag ``flag`` files are dropped, while declared
     challenge inputs and placeholder flag files survive.

  2. Workspace-scratch reset (``_reset_workspace_scratch``): loose agent scratch in
     the /workspace volume root is wiped between runs, but the challenge tree (and the
     session bindings inside it) is preserved.

Run: PYTHONPATH=src python3 -m pytest tests/test_materialize_filtering.py
"""

import os
from pathlib import Path

from application.benchmark.services import benchmark_service as bs
from infrastructure.benchmarks.nyuctf_repository import (
    _is_solution_leak,
    _load_declared_files_and_flag,
    materialize_allowlisted_files,
)

GT = "flag{r34l_s3cr3t}"


def _make_challenge(tmp_path: Path) -> Path:
    chal = tmp_path / "chal"
    (chal / "suckerusu").mkdir(parents=True)
    (chal / "challenge.json").write_text(
        '{"name":"x","flag":"%s","files":["./suckerusu/main.c","./suckerusu/Makefile"]}' % GT
    )
    (chal / "suckerusu" / "main.c").write_text("int main(){}")
    (chal / "suckerusu" / "Makefile").write_text("all:")
    (chal / "README.md").write_text("# X\n## Description\nbrief\n")
    (chal / "solution.c").write_text("// the author solution\n")
    (chal / "solver.py").write_text("print('solve')\n")
    (chal / "flag.txt").write_text(GT + "\n")          # real answer key
    (chal / "server").mkdir()
    (chal / "server" / "flag").write_text("flag{placeholder}\n")  # local-server placeholder
    (chal / "hints").mkdir()
    (chal / "hints" / "implementation.md").write_text("run this to get the flag\n")
    return chal


def _classify(chal: Path):
    declared, gt = _load_declared_files_and_flag(chal)
    leaks, kept = set(), set()
    for p in chal.rglob("*"):
        if not p.is_file() or p.name == "challenge.json":
            continue
        rel = p.relative_to(chal)
        (leaks if _is_solution_leak(rel, chal, declared=declared, gt_flag=gt) else kept).add(
            rel.as_posix()
        )
    return declared, gt, leaks, kept


def test_declared_manifest_parsed(tmp_path):
    chal = _make_challenge(tmp_path)
    declared, gt = _load_declared_files_and_flag(chal)
    assert declared == {"suckerusu/main.c", "suckerusu/Makefile"}
    assert gt == GT


def test_solution_scripts_dropped(tmp_path):
    chal = _make_challenge(tmp_path)
    _, _, leaks, _ = _classify(chal)
    assert "solution.c" in leaks
    assert "solver.py" in leaks


def test_real_flag_answer_key_dropped_placeholder_kept(tmp_path):
    chal = _make_challenge(tmp_path)
    _, _, leaks, kept = _classify(chal)
    assert "flag.txt" in leaks                # contains the real flag
    assert "server/flag" in kept             # placeholder -> kept for a local server


def test_declared_inputs_never_dropped(tmp_path):
    chal = _make_challenge(tmp_path)
    _, _, leaks, kept = _classify(chal)
    assert {"suckerusu/main.c", "suckerusu/Makefile"} <= kept
    assert not ({"suckerusu/main.c", "suckerusu/Makefile"} & leaks)


def test_env_toggle_disables_filter(tmp_path, monkeypatch):
    chal = _make_challenge(tmp_path)
    monkeypatch.setenv("GENCYBER_STRIP_SOLUTIONS", "0")
    declared, gt = _load_declared_files_and_flag(chal)
    assert _is_solution_leak(Path("solution.c"), chal, declared=declared, gt_flag=gt) is False


def test_declared_file_named_like_solution_is_protected(tmp_path):
    """If a challenge *declares* a solve-named file as an input, keep it."""
    chal = tmp_path / "c2"
    chal.mkdir()
    (chal / "challenge.json").write_text('{"flag":"%s","files":["./solve.py"]}' % GT)
    (chal / "solve.py").write_text("vulnerable target\n")
    declared, gt = _load_declared_files_and_flag(chal)
    assert _is_solution_leak(Path("solve.py"), chal, declared=declared, gt_flag=gt) is False


# --- Allow-list materialization (only challenge.json files + compose reach the agent) --

def _make_web_challenge(tmp_path: Path) -> Path:
    """A compose-backed web challenge whose flag leaks through Dockerfile/README/setup."""
    chal = tmp_path / "web"
    (chal / "src").mkdir(parents=True)
    (chal / "challenge.json").write_text(
        '{"name":"w","flag":"%s","files":["src.tar.gz"],"compose":true}' % GT
    )
    (chal / "src.tar.gz").write_bytes(b"tarball-bytes-no-flag")
    # Every one of these previously reached the agent and leaked the flag:
    (chal / "Dockerfile").write_text('RUN echo \'$FLAG="%s"\' > flag.php\n' % GT)
    (chal / "docker-compose.yml").write_text(
        "services:\n  server:\n    image: llmctf/w\n    ports:\n      - 80:80\n"
    )
    (chal / "README.md").write_text("Flag\n====\n%s\n" % GT)
    (chal / "mysql-setup.sh").write_text("INSERT INTO t VALUES ('%s');\n" % GT)
    (chal / "src" / "index.php").write_text("<?php // app source ?>\n")
    return chal


def _materialized_names(target: Path):
    return {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}


def _contains_flag(target: Path) -> bool:
    for p in target.rglob("*"):
        if p.is_file() and GT.encode() in p.read_bytes():
            return True
    return False


def test_allowlist_copies_only_declared_and_compose(tmp_path):
    chal = _make_web_challenge(tmp_path)
    target = tmp_path / "ws"
    n = materialize_allowlisted_files(chal, target)
    names = _materialized_names(target)
    # exactly the declared input + the compose file — nothing else
    assert names == {"src.tar.gz", "docker-compose.yml"}
    assert n == 2


def test_allowlist_drops_all_flag_leak_files(tmp_path):
    chal = _make_web_challenge(tmp_path)
    target = tmp_path / "ws"
    materialize_allowlisted_files(chal, target)
    names = _materialized_names(target)
    for leaked in ("Dockerfile", "README.md", "mysql-setup.sh", "src/index.php", "challenge.json"):
        assert leaked not in names
    # and the ground-truth flag never appears in any materialized file
    assert not _contains_flag(target)


def test_allowlist_keeps_declared_binary_that_embeds_flag(tmp_path):
    """A declared input (e.g. a rev binary) is copied verbatim even if it embeds the flag."""
    chal = tmp_path / "rev"
    chal.mkdir()
    (chal / "challenge.json").write_text('{"flag":"%s","files":["paloalto"]}' % GT)
    (chal / "paloalto").write_bytes(b"ELF...prints " + GT.encode())
    (chal / "README.md").write_text("solution walkthrough %s\n" % GT)
    (chal / "test_solver").write_text("author solver\n")
    target = tmp_path / "ws"
    materialize_allowlisted_files(chal, target)
    names = _materialized_names(target)
    assert names == {"paloalto"}                # declared binary kept; README/solver dropped
    assert (target / "paloalto").read_bytes().endswith(GT.encode())


def test_allowlist_server_only_challenge_gets_compose_only(tmp_path):
    chal = tmp_path / "nc"
    chal.mkdir()
    (chal / "challenge.json").write_text('{"flag":"%s","files":[],"compose":true}' % GT)
    (chal / "docker-compose.yml").write_text("services:\n  s:\n    image: llmctf/nc\n")
    (chal / "README.md").write_text("Flag: %s\n" % GT)
    target = tmp_path / "ws"
    n = materialize_allowlisted_files(chal, target)
    assert _materialized_names(target) == {"docker-compose.yml"}
    assert n == 1


# --- Workspace-scratch reset ---------------------------------------------------------

def test_reset_workspace_scratch(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    challenges = workspace / "challenges"
    challenges.mkdir(parents=True)
    # challenge tree + a session binding live under challenges/ and must survive
    (challenges / "nyuctf" / "test" / "x").mkdir(parents=True)
    (challenges / "nyuctf" / "test" / "x" / "main.c").write_text("keep me")
    (challenges / "_sessions").mkdir()
    (challenges / "_sessions" / "s1.json").write_text("{}")
    # loose agent scratch in the workspace root must be wiped
    (workspace / "decrypted_flag.txt").write_text("stale")
    (workspace / "keystream.bin").write_text("stale")
    (workspace / ".gencyber-agent-scripts").mkdir()
    (workspace / ".gencyber-agent-scripts" / "solve.py").write_text("stale")

    monkeypatch.setenv("CHALLENGE_ROOT", str(challenges))
    monkeypatch.delenv("GENCYBER_WORKSPACE_RESET", raising=False)
    bs._reset_workspace_scratch()

    assert (challenges / "nyuctf" / "test" / "x" / "main.c").read_text() == "keep me"
    assert (challenges / "_sessions" / "s1.json").is_file()
    assert not (workspace / "decrypted_flag.txt").exists()
    assert not (workspace / "keystream.bin").exists()
    assert not (workspace / ".gencyber-agent-scripts").exists()


def test_reset_workspace_scratch_env_off(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    challenges = workspace / "challenges"
    challenges.mkdir(parents=True)
    (workspace / "scratch.txt").write_text("stale")
    monkeypatch.setenv("CHALLENGE_ROOT", str(challenges))
    monkeypatch.setenv("GENCYBER_WORKSPACE_RESET", "0")
    bs._reset_workspace_scratch()
    assert (workspace / "scratch.txt").exists()  # disabled -> nothing removed


def test_reset_workspace_scratch_guards_unsafe_root(tmp_path, monkeypatch):
    # CHALLENGE_ROOT == "/" would make the workspace root "/" (parent of "/" is "/").
    monkeypatch.setenv("CHALLENGE_ROOT", "/")
    monkeypatch.delenv("GENCYBER_WORKSPACE_RESET", raising=False)
    # Must be a no-op (no exception, no filesystem traversal of "/").
    bs._reset_workspace_scratch()
