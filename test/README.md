# Docker-Manager simulation harness

A load/integration test driver for the Docker-Manager CTFd plugin. Stands up
N synthetic users against a running CTFd instance, has each one start a
configurable number of Docker challenges, then optionally hammers the
resulting HTTP containers with a fixed total request count spread across
them.

Useful for:

- exercising the per-team container scheduling path end-to-end
- measuring container startup latency, nginx routing readiness, and
  steady-state HTTP latency under load
- regression-testing the plugin after non-trivial changes (worker SSH,
  port pools, nginx reload, suspension/lifetime timers)

## What it does, in order

1. Authenticates as admin (token or username+password) against CTFd.
2. Creates the requested number of users and teams via the CTFd admin API
   (or skips creation with `--existing`).
3. Auto-discovers all per-team Docker challenges (or uses the explicit list
   from `[challenges].ids`). Global/shared challenges are skipped because
   simulating multiple users starting them isn't meaningful.
4. Schedules each user's challenge starts at a random offset within the
   configured `duration`, then runs them concurrently in threads.
5. For each successfully-started challenge, polls the container's HTTP URLs
   until they respond (the readiness probe), and records startup +
   first-response timings.
6. If `--probes N` is set, runs a load-probe phase: sends N total HTTP GETs
   round-robin (then shuffled) across every container that came up, using
   `--probe-concurrency` worker threads.
7. Prints aggregated stats: container startup time, readiness HTTP time,
   load-probe time, and any per-action errors.

## Setup

### 1. Install dependencies

```sh
pip install requests
```

`tomllib` is in the Python 3.11+ standard library. On older Pythons,
`pip install tomli` and the script will fall back to it.

### 2. Create a local config

```sh
cp test/config.example.toml test/config.toml
```

Then edit [config.toml](config.toml) and fill in:

- `[ctfd].url` — base URL of the CTFd instance (no trailing slash).
- `[ctfd.admin]` — either `api_token` (preferred) **or** `username` +
  `password`. The token wins if both are set.
- `[teams].join_password` — used when creating teams in team-mode CTFd.
- Any sim defaults you want different from the example.

`config.toml` is gitignored. `config.example.toml` is the only file safe
to commit, so keep credentials out of it.

### 3. Define your user pool (optional)

The `[[users.pool]]` array is a list of named accounts the script picks
from when creating users. **Only `name` (and optionally `team`) is
required** — missing `email` is auto-filled with a random 16-hex
`<token>@test.local` address, and missing `password` falls back to
`[users].generated_password`:

```toml
[users]
generated_password = "SimPass123!"

[[users.pool]]
name = "Maximus Conner"
team = "Team Alpha"

[[users.pool]]
name = "Zaid Russell"
team = "Team Neutron"
```

If `--users` exceeds the pool size, the rest are auto-generated as
`Sim User K` / `Team K` with random emails.

> **Note on randomized emails**: every run creates *new* CTFd accounts.
> If you'd rather reuse the same accounts across runs (e.g. to keep the
> CTFd DB clean), set explicit `email = "..."` values in the pool — the
> script only auto-fills when the field is absent.

## Running

The minimal invocation reads everything from `config.toml`:

```sh
python test/simulation.py
```

All defaults can be overridden on the command line:

| Flag | Effect | Default source |
|------|--------|----------------|
| `--config PATH` | Use a different TOML file | `test/config.toml` |
| `--users N` | Number of users to create | `[simulation].default_users` |
| `--challenges M` | Challenges each user starts | `[simulation].starts_per_user` |
| `--probes P` | Total HTTP probes spread across containers (`0` = readiness only) | `[simulation].default_total_probes` |
| `--probe-concurrency N` | Concurrent probe workers | `[simulation].default_probe_concurrency` |
| `--duration SEC` | Window over which user actions are spread | `[simulation].duration` |
| `--existing` | Use `[[users.existing]]` instead of creating new accounts | — |

### Examples

Quick smoke test — 5 users, 1 challenge each, no load phase:

```sh
python test/simulation.py --users 5 --challenges 1
```

Heavy load — 50 users, 2 challenges each over 10 minutes, then 5000
HTTP requests across the resulting containers with 25 worker threads:

```sh
python test/simulation.py --users 50 --challenges 2 --duration 600 \
    --probes 5000 --probe-concurrency 25
```

Re-run against pre-created accounts:

```sh
python test/simulation.py --existing
```

## Reading the output

At the end of a run, a summary block looks like:

```
========================================================================
  SIMULATION COMPLETE  (sim 612.3s, total 645.1s)
  Started      : 100
  Failed       : 0
  HTTP failed  : 0
  Load failed  : 2
------------------------------------------------------------------------
  Container startup : avg= 1842.1ms  median= 1610.3ms  min=  920.4ms  max= 4231.5ms  n=100
  Readiness HTTP    : avg=  142.7ms  median=  118.0ms  min=   41.2ms  max=  890.4ms  n=100
  Load probes       : avg=   18.4ms  median=   12.1ms  min=    3.5ms  max=  413.2ms  n=4998
========================================================================
```

- **Started / Failed** — challenge-start API calls.
- **HTTP failed** — readiness probe never got a non-5xx response within
  `[probe].timeout`. Usually means the container started but nginx didn't
  pick up the route, or the app inside the container didn't bind in time.
- **Load failed** — an HTTP error during the load-probe phase.
- **Container startup** — wall time of the `/docker/api/challenge/.../start`
  call (includes SSH-to-worker, image pull if needed, container create,
  nginx reload).
- **Readiness HTTP** — first successful response time after the container
  reports running.
- **Load probes** — per-request time during the load phase, averaged
  across all participating containers.

## Troubleshooting

- **`ERROR: config file not found`** — you skipped step 2 of setup. Copy
  `config.example.toml` to `config.toml`.
- **`Admin login failed`** — wrong `[ctfd.admin]` username/password, or the
  account isn't an admin. An API token from CTFd → Settings → Access
  Tokens is more reliable.
- **No challenges discovered** — set `[challenges].ids` explicitly, or
  make sure your Docker challenges aren't all marked global.
- **Many `HTTP failed`** — bump `[probe].timeout`. Containers cold-pulling
  large images can exceed the default 30s.
- **Hangs at the end** — the join timeout on the user-thread pool is
  `duration + 120s`; a stuck container start can hold a thread that
  long. Check the worker node and CTFd logs.
