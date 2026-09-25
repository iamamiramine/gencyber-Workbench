# Workbench CTF sandbox

The workbench image (`GENCYBER_SANDBOX_PROFILE=kali-ctf`) includes a curated CTF tool set: `file`, `dig`, `ping`, `nmap`, `netcat`, common Python crypto libraries, and Docker CLI for challenge compose lifecycle.

## Challenge services

Workflow runs should call:

- `POST /benchmarks/materialize`
- `POST /benchmarks/start-challenge-services`
- `POST /benchmarks/stop-challenge-services` (on teardown)

Requires Docker socket mount (`/var/run/docker.sock`) and external network `generative-cybersecurity-network`.

## Ground-truth flags

`POST /benchmarks/resolve-flag` is for trusted agent backends only (not browser UI).
