"""Unit tests for compose endpoint discovery (no docker required)."""

from application.benchmark.helpers.docker_compose_patch import patch_compose_for_gencyber_network
from application.benchmark.services.challenge_runtime_service import _discover_endpoints

import yaml


def test_patch_remaps_ctfnet():
    raw = """
services:
  server:
    image: test
    networks:
      ctfnet:
        aliases: [web.chal.csaw.io]
networks:
  ctfnet:
    external: true
"""
    patched = patch_compose_for_gencyber_network(raw, network_name="generative-cybersecurity-network")
    data = yaml.safe_load(patched)
    assert "ctfnet" not in (data.get("networks") or {})
    eps = _discover_endpoints(data)
    assert any(e.get("host") == "web.chal.csaw.io" for e in eps)
