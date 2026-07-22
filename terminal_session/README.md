# Remote terminal

This is a node server that uses websockets implemented via socket.io to
pass input and output from a terminal session.
This is intended to be run using Docker.

# Customize

Place your files under "files" folder. You can edit the README there.
Then build the image:

```
docker build -t remote-terminal .
```

Run it

```
docker run --rm -d -p 8090:3000 remote-terminal
``` 

Now visit localhost:8090 !

# HTTP API

Sessions wrap a persistent `/bin/bash` PTY. State (cwd, env, background jobs)
is shared across the websocket UI and the HTTP API.

- `POST /api/sessions` `{ sessionId, cwd? }` - create/get a session.
- `GET  /api/sessions` - list sessions.
- `DELETE /api/sessions/:id` - destroy a (non-default) session.
- `POST /api/execute` `{ sessionId, command, timeout }` - run a command.
  Completion is detected via a hidden exit-code sentinel, so `exit_code` is the
  real shell exit status. If the command exceeds `timeout` it is NOT abandoned:
  it keeps running in the PTY and the response returns `{ running: true,
  job_id, timeout: true }` plus partial `stdout`.
- `GET  /api/sessions/:id/output?job=<id>&since=<offset>` - poll a running or
  finished job. Returns `stdout`, `running`, `exit_code`, and `next_offset`
  for incremental reads.
- `POST /api/sessions/:id/input` `{ data, append_newline?, settle_ms? }` -
  send keystrokes/input to the PTY (interactive prompts, REPL, ssh password,
  pager keys). Returns the captured output window.
- `POST /api/sessions/:id/signal` `{ signal, settle_ms? }` - send a control
  signal to the foreground command (`SIGINT`=Ctrl-C, `SIGQUIT`, `SIGTSTP`,
  `EOF`) without killing the shell.
- `POST /api/sessions/:id/write-file` `{ path, content | content_base64,
  mode? }` - write a file directly onto the volume (no shell quoting).
- `GET  /api/sessions/:id/info` - shell pid, current cwd (via /proc), and
  running/background job status.

Note: stdout and stderr share one PTY stream (as in a real terminal); the
`stderr` field carries transport-level errors only.
