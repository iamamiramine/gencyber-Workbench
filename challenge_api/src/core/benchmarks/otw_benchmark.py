"""OverTheWire wargame loader (Bandit + Krypton).

Unlike NYU CTF, OTW has no local challenge files and no ground-truth flag file: each
"challenge" is one wargame level, the level's answer is the SSH password for the next
account, and validation is a live SSH login (see
``application/benchmark/services/otw_support.py``). This loader exposes the two games as
splits of a single ``otw`` benchmark, reading a static level catalog from
``otw_levels.json``.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.benchmarks.base_benchmark import BaseBenchmark, Split
from domain.models.benchmark.challenge_model import Challenge, HelpfulHints

# First playable level per game (bandit0 is the seeded start; krypton level 0 is the
# website base64 hand-off that gives krypton1's password — not an SSH-login challenge).
_START_LEVEL = {"bandit": 0, "krypton": 1}
# Route each game to the most fitting specialist (krypton is pure cryptography; bandit
# is mixed Linux/CLI, closest to the misc specialist).
_CATEGORY = {"bandit": "misc", "krypton": "crypto"}


def _dataset_path() -> Path:
    env = (os.environ.get("OTW_DATASET_PATH") or "").strip()
    if env:
        return Path(env)
    # Bundled next to this module by default.
    here = Path(__file__).resolve().parent
    for cand in (here / "otw_levels.json", Path("/workspace/otw_levels.json")):
        if cand.exists():
            return cand
    return here / "otw_levels.json"


@lru_cache(maxsize=1)
def _load_dataset() -> Dict[str, List[Dict[str, Any]]]:
    path = _dataset_path()
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for game in ("bandit", "krypton"):
        levels = sorted(data.get(game) or [], key=lambda x: int(x.get("level", 0)))
        for lv in levels:
            lv.setdefault("game", game)
        out[game] = levels
    return out


class OTWBenchmark(BaseBenchmark):
    """OverTheWire Bandit/Krypton loader. Splits are the two games."""

    benchmark_id = "otw"

    def supported_splits(self):  # type: ignore[override]
        return ("bandit", "krypton")

    def _levels(self, split: Split) -> List[Dict[str, Any]]:
        game = str(split)
        levels = _load_dataset().get(game)
        if not levels:
            raise ValueError(f"Unknown OTW split '{split}' (expected bandit|krypton)")
        return levels

    def get_level(self, split: Split, challenge_id: str) -> Dict[str, Any]:
        for lv in self._levels(split):
            if str(lv.get("user")) == str(challenge_id):
                return lv
        raise ValueError(f"Unknown OTW level '{challenge_id}' in split '{split}'")

    def _runnable_levels(self, split: Split) -> List[Dict[str, Any]]:
        """Levels an agent can actually attempt: at/after the seeded start level and not
        a synthetic terminal entry (``next_user == user``)."""
        start = _START_LEVEL.get(str(split), 0)
        out = []
        for lv in self._levels(split):
            n = int(lv.get("level", 0))
            if n < start:
                continue
            if lv.get("next_user") == lv.get("user"):
                continue
            out.append(lv)
        return out

    def challenge_catalog(self, split: Split) -> List[Dict[str, Any]]:
        cat = _CATEGORY.get(str(split), str(split))
        out: List[Dict[str, Any]] = []
        for lv in self._runnable_levels(split):
            user = lv.get("user")
            nxt = lv.get("next_user")
            out.append(
                {
                    "challenge_id": user,
                    "challenge": f"{split} {lv.get('level')} ({user} → {nxt})",
                    "category": cat,
                    "level": lv.get("level"),
                    "next_user": nxt,
                    "host": lv.get("host"),
                    "port": lv.get("port"),
                }
            )
        return out

    def load_challenge(self, split: Split, challenge_id: str) -> Challenge:
        lv = self.get_level(split, challenge_id)
        user = lv.get("user") or challenge_id
        nxt = lv.get("next_user") or ""
        host = lv.get("host") or ""
        port = lv.get("port")
        conn = f"sshpass -p '<password>' ssh -o StrictHostKeyChecking=no -p {port} {user}@{host}"
        hints = HelpfulHints(
            files_path_in_container="(remote OTW server via SSH — no local files)",
            suggested_first_commands=[f"{conn} 'ls -la; cat ./* 2>/dev/null | head'"],
        )
        return Challenge(
            benchmark=self.benchmark_id,
            split=split,
            challenge_id=user,
            name=f"OverTheWire {split} level {lv.get('level')} ({user} → {nxt})",
            category=_CATEGORY.get(str(split), str(split)),
            points=None,
            description=(lv.get("goal") or "").strip()
            or f"Recover the SSH password for {nxt}.",
            flag_format=f"the SSH password for {nxt} (raw string)",
            server=None,
            files=[],
            helpful_hints=hints,
            raw=lv,
        )
