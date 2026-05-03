"""Ensure NYU-style compose files attach services to the gencyber shared Docker network."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_NETWORK = os.environ.get("GENCYBER_DOCKER_NETWORK", "generative-cybersecurity-network")


def patch_compose_for_gencyber_network(
    compose_yaml: str,
    *,
    network_name: Optional[str] = None,
) -> str:
    """
    Parse compose YAML and make challenge containers reachable from the sandbox shell:

    - Remove **other** top-level networks marked ``external: true`` (e.g. NYU's ``ctfnet``)
      that do not exist on the host, and remap service references to ``network_name``.
    - Keep non-external (bridge/internal) networks so multi-network stacks still work.
    - Ensure every service (without ``network_mode``) is also attached to the shared
      external network so the agent shell can connect.

    Set ``GENCYBER_SKIP_COMPOSE_NETWORK_PATCH=1`` to pass YAML through unchanged.
    """
    net = network_name or DEFAULT_NETWORK
    if os.environ.get("GENCYBER_SKIP_COMPOSE_NETWORK_PATCH", "").lower() in ("1", "true", "yes"):
        return compose_yaml

    try:
        data = yaml.safe_load(compose_yaml)
    except yaml.YAMLError as e:
        logger.warning("docker compose YAML parse failed, using raw: %s", e)
        return compose_yaml

    if not isinstance(data, dict):
        return compose_yaml

    # Compose spec v2+ ignores this; docker warns. Drop to avoid noisy logs.
    if "version" in data:
        del data["version"]

    services = data.get("services")
    if not isinstance(services, dict) or not services:
        return compose_yaml

    networks_block: Dict[str, Any] = data.get("networks") if isinstance(data.get("networks"), dict) else {}
    data["networks"] = networks_block

    # Map removed external networks (ctfnet, etc.) -> shared gencyber network.
    remap: Dict[str, str] = {}
    for name, spec in list(networks_block.items()):
        if not isinstance(spec, dict):
            continue
        if spec.get("external") is True and name != net:
            remap[name] = net
            del networks_block[name]
            logger.info("Remapping external compose network %r -> %r", name, net)

    networks_block[net] = {"external": True}

    def _rewrite_service_networks(snets: Any) -> Any:
        if isinstance(snets, list):
            out: list[str] = []
            for item in snets:
                if isinstance(item, str):
                    out.append(remap.get(item, item))
            seen: set[str] = set()
            deduped: list[str] = []
            for x in out:
                if x not in seen:
                    seen.add(x)
                    deduped.append(x)
            return deduped if deduped else [net]
        if isinstance(snets, dict):
            new_d: Dict[str, Any] = {}
            for k, v in snets.items():
                if not isinstance(k, str):
                    continue
                nk = remap.get(k, k)
                if nk in new_d and isinstance(new_d[nk], dict) and isinstance(v, dict):
                    new_d[nk] = {**new_d[nk], **v}
                elif nk in new_d:
                    new_d[nk] = v
                else:
                    new_d[nk] = v
            return new_d if new_d else {net: {}}
        return [net]

    for _svc_name, spec in list(services.items()):
        if not isinstance(spec, dict):
            continue
        if spec.get("network_mode"):
            continue
        snets = spec.get("networks")
        if snets is None:
            spec["networks"] = [net]
        else:
            spec["networks"] = _rewrite_service_networks(snets)

        # Guarantee attachment to shared Docker network for agent reachability.
        sn = spec.get("networks")
        if isinstance(sn, list):
            if net not in sn:
                sn.append(net)
        elif isinstance(sn, dict) and net not in sn:
            sn[net] = {}

    try:
        return yaml.safe_dump(
            data,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
    except Exception as e:
        logger.warning("docker compose YAML dump failed, using raw: %s", e)
        return compose_yaml
