#!/usr/bin/env python3
"""
CTFd Docker Manager - Challenge Simulation Script

Creates N users via the CTFd admin API, has each start M challenges over a
configurable timespan, then fires a total of P HTTP probe requests spread
round-robin across every started HTTP container.

Settings (URLs, credentials, user pool, probe timings, …) are loaded from
config.toml in this directory. Copy config.example.toml to config.toml and
edit it. The real config.toml is gitignored.

Usage:
  python test_simulation.py [--config PATH] [--users N] [--challenges M]
                            [--probes P] [--duration SEC]
                            [--probe-concurrency N] [--existing]

Requirements:
  pip install requests        # tomllib is stdlib on Python 3.11+
"""

import re
import sys
import time
import random
import secrets
import logging
import argparse
import itertools
from pathlib import Path
from threading import Thread, Lock
from dataclasses import dataclass, field
from typing import Optional, Any

import requests

try:
    import tomllib                                  # Python 3.11+
except ModuleNotFoundError:                          # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        sys.stderr.write(
            "ERROR: TOML support requires Python 3.11+ or `pip install tomli`.\n"
        )
        raise

# ──────────────────────────────────────────────────────────────────────────────
# Configuration — populated by _load_config() from test_simulation.toml
# ──────────────────────────────────────────────────────────────────────────────

# Safe placeholders. Real values come from the TOML file.
CTFD_URL = ""
ADMIN_USERNAME = ""
ADMIN_PASSWORD = ""
ADMIN_API_TOKEN = ""

SIMULATION_DURATION = 600
DEFAULT_USERS = 10
STARTS_PER_USER = 1
DEFAULT_TOTAL_PROBES = 0
DEFAULT_PROBE_CONCURRENCY = 10
PAUSE_BETWEEN_STARTS: tuple[float, float] = (2.0, 5.0)
STOP_AFTER_START = False
RANDOM_SEED: Optional[int] = None

EXISTING_USERS: list[dict] = []
USERS_TO_CREATE: list[dict] = []
GENERATED_PASSWORD = "SimPass123!"
TEAM_JOIN_PASSWORD = ""

CHALLENGE_IDS: list[int] = []

HTTP_PROBE_TIMEOUT = 30.0
HTTP_PROBE_INTERVAL = 0.5

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"


def _load_config(path: Path) -> None:
    """Populate module-level config globals from a TOML file."""
    global CTFD_URL, ADMIN_USERNAME, ADMIN_PASSWORD, ADMIN_API_TOKEN
    global SIMULATION_DURATION, DEFAULT_USERS, STARTS_PER_USER
    global DEFAULT_TOTAL_PROBES, DEFAULT_PROBE_CONCURRENCY
    global PAUSE_BETWEEN_STARTS, STOP_AFTER_START, RANDOM_SEED
    global EXISTING_USERS, USERS_TO_CREATE, GENERATED_PASSWORD, TEAM_JOIN_PASSWORD
    global CHALLENGE_IDS, HTTP_PROBE_TIMEOUT, HTTP_PROBE_INTERVAL

    if not path.exists():
        sys.stderr.write(
            f"ERROR: config file not found: {path}\n"
            f"  Copy {path.with_suffix('.example.toml').name} to {path.name} and edit it.\n"
        )
        sys.exit(2)

    with path.open("rb") as f:
        cfg: dict[str, Any] = tomllib.load(f)

    ctfd = cfg.get("ctfd", {})
    CTFD_URL = ctfd.get("url", CTFD_URL)
    admin = ctfd.get("admin", {})
    ADMIN_USERNAME = admin.get("username", "")
    ADMIN_PASSWORD = admin.get("password", "")
    ADMIN_API_TOKEN = admin.get("api_token", "")

    sim = cfg.get("simulation", {})
    SIMULATION_DURATION = int(sim.get("duration", SIMULATION_DURATION))
    DEFAULT_USERS = int(sim.get("default_users", DEFAULT_USERS))
    STARTS_PER_USER = int(sim.get("starts_per_user", STARTS_PER_USER))
    DEFAULT_TOTAL_PROBES = int(sim.get("default_total_probes", DEFAULT_TOTAL_PROBES))
    DEFAULT_PROBE_CONCURRENCY = int(sim.get("default_probe_concurrency", DEFAULT_PROBE_CONCURRENCY))
    pbs = sim.get("pause_between_starts", list(PAUSE_BETWEEN_STARTS))
    PAUSE_BETWEEN_STARTS = (float(pbs[0]), float(pbs[1]))
    STOP_AFTER_START = bool(sim.get("stop_after_start", STOP_AFTER_START))
    if "random_seed" in sim:
        RANDOM_SEED = int(sim["random_seed"])

    teams = cfg.get("teams", {})
    TEAM_JOIN_PASSWORD = teams.get("join_password", "")

    chals = cfg.get("challenges", {})
    CHALLENGE_IDS = [int(x) for x in chals.get("ids", [])]

    probe = cfg.get("probe", {})
    HTTP_PROBE_TIMEOUT = float(probe.get("timeout", HTTP_PROBE_TIMEOUT))
    HTTP_PROBE_INTERVAL = float(probe.get("interval", HTTP_PROBE_INTERVAL))

    users = cfg.get("users", {})
    GENERATED_PASSWORD = users.get("generated_password", GENERATED_PASSWORD)
    USERS_TO_CREATE = [_fill_user_defaults(u) for u in users.get("pool", [])]
    EXISTING_USERS = list(users.get("existing", []))

    if not CTFD_URL:
        sys.stderr.write("ERROR: ctfd.url is not set in config.\n")
        sys.exit(2)
    if not ADMIN_API_TOKEN and not (ADMIN_USERNAME and ADMIN_PASSWORD):
        sys.stderr.write(
            "ERROR: set [ctfd.admin].api_token OR username+password in config.\n"
        )
        sys.exit(2)


def _fill_user_defaults(entry: dict) -> dict:
    """Fill in missing email/password on a [[users.pool]] entry."""
    out = dict(entry)
    if not out.get("email"):
        out["email"] = f"{secrets.token_hex(8)}@test.local"
    if not out.get("password"):
        out["password"] = GENERATED_PASSWORD
    return out

# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sim")

_results_lock = Lock()
_results: dict = {
    "started": 0,
    "failed": 0,
    "errors": [],
    "startup_times_ms": [],
    "http_times_ms": [],          # readiness probe times
    "http_failed": 0,
    "load_times_ms": [],          # per-request times during the load-probe phase
    "load_failed": 0,
}

# URLs of every container that came up successfully — populated by the
# simulation phase, drained round-robin by the load-probe phase.
_started_urls: list[str] = []
_started_urls_lock = Lock()

# HTTP probe settings
HTTP_PROBE_TIMEOUT = 30.0        # total seconds to wait for a URL to respond
HTTP_PROBE_INTERVAL = 0.5        # seconds between retries while waiting


# ──────────────────────────────────────────────────────────────────────────────
# HTML / CSRF helpers
# ──────────────────────────────────────────────────────────────────────────────

def _extract_nonce(html: str) -> str:
    """Pull the hidden nonce value from a CTFd login/register page."""
    for pattern in (
        r'name=["\']nonce["\'][^>]+value=["\']([a-f0-9]+)["\']',
        r'value=["\']([a-f0-9]+)["\'][^>]+name=["\']nonce["\']',
        r"'csrfNonce':\s*['\"]([a-f0-9]+)['\"]",
        r'"csrfNonce":\s*"([a-f0-9]+)"',
    ):
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    raise ValueError("Could not find nonce in page HTML. Check CTFD_URL.")


def _extract_csrf(html: str) -> Optional[str]:
    """Pull the CSRF nonce embedded in authenticated CTFd pages."""
    for pattern in (
        r"csrfNonce['\"]:\s*['\"]([a-f0-9]+)['\"]",
        r"'csrf_nonce':\s*'([a-f0-9]+)'",
    ):
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# User session
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class UserSession:
    username: str
    password: str
    session: requests.Session = field(default_factory=requests.Session)
    csrf: Optional[str] = None

    def login(self) -> bool:
        try:
            r = self.session.get(f"{CTFD_URL}/login", timeout=15)
            r.raise_for_status()
            nonce = _extract_nonce(r.text)

            r = self.session.post(
                f"{CTFD_URL}/login",
                data={"name": self.username, "password": self.password, "nonce": nonce},
                timeout=15,
                allow_redirects=True,
            )

            # CTFd redirects to /challenges on success; error page keeps /login
            if "/login" in r.url and "incorrect" in r.text.lower():
                log.error(f"[{self.username}] Bad credentials")
                return False

            # Grab CSRF token from the landing page
            self.csrf = _extract_csrf(r.text)
            if not self.csrf:
                # Try fetching /challenges to get it
                r2 = self.session.get(f"{CTFD_URL}/challenges", timeout=15)
                self.csrf = _extract_csrf(r2.text)

            if not self.csrf:
                log.warning(f"[{self.username}] Logged in but could not find CSRF token")

            log.info(f"[{self.username}] Logged in (csrf={'ok' if self.csrf else 'missing'})")
            return True

        except Exception as exc:
            log.error(f"[{self.username}] Login exception: {exc}")
            return False

    def _docker_post(self, path: str) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.csrf:
            headers["CSRF-Token"] = self.csrf
        try:
            r = self.session.post(
                f"{CTFD_URL}{path}",
                headers=headers,
                json={},
                timeout=60,
            )
            return r.json()
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def start_challenge(self, challenge_id: int) -> dict:
        return self._docker_post(f"/docker/api/challenge/{challenge_id}/start")

    def stop_challenge(self, challenge_id: int) -> dict:
        return self._docker_post(f"/docker/api/challenge/{challenge_id}/stop")

    def challenge_status(self, challenge_id: int) -> dict:
        try:
            r = self.session.get(
                f"{CTFD_URL}/docker/api/challenge/{challenge_id}/status",
                timeout=15,
            )
            return r.json()
        except Exception as exc:
            return {"success": False, "error": str(exc)}


# ──────────────────────────────────────────────────────────────────────────────
# Admin helpers
# ──────────────────────────────────────────────────────────────────────────────

def _admin_session() -> requests.Session:
    """Return a requests.Session authenticated as admin."""
    s = requests.Session()
    if ADMIN_API_TOKEN:
        s.headers["Authorization"] = f"Token {ADMIN_API_TOKEN}"
        s.headers["Content-Type"] = "application/json"
        return s

    # Fall back to session login
    r = s.get(f"{CTFD_URL}/login", timeout=15)
    r.raise_for_status()
    nonce = _extract_nonce(r.text)
    r = s.post(
        f"{CTFD_URL}/login",
        data={"name": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "nonce": nonce},
        timeout=15,
        allow_redirects=True,
    )
    if "/login" in r.url:
        raise RuntimeError("Admin login failed — check ADMIN_USERNAME / ADMIN_PASSWORD")

    csrf = _extract_csrf(r.text)
    if csrf:
        s.headers["CSRF-Token"] = csrf
    s.headers["Content-Type"] = "application/json"
    log.info("Admin session established")
    return s


def create_user(admin: requests.Session, name: str, email: str, password: str) -> Optional[int]:
    r = admin.post(f"{CTFD_URL}/api/v1/users", json={
        "name": name, "email": email, "password": password,
        "type": "user", "verified": True, "hidden": False,
    }, timeout=15)
    data = r.json()
    if data.get("success"):
        uid = data["data"]["id"]
        log.info(f"  Created user '{name}' (id={uid})")
        return uid
    log.warning(f"  Could not create user '{name}': {data.get('errors', data)}")
    return None


def create_team(admin: requests.Session, name: str) -> Optional[int]:
    r = admin.post(f"{CTFD_URL}/api/v1/teams", json={
        "name": name, "password": TEAM_JOIN_PASSWORD, "hidden": False,
    }, timeout=15)
    data = r.json()
    if data.get("success"):
        tid = data["data"]["id"]
        log.info(f"  Created team '{name}' (id={tid})")
        return tid
    log.warning(f"  Could not create team '{name}': {data.get('errors', data)}")
    return None


def add_to_team(admin: requests.Session, team_id: int, user_id: int):
    r = admin.post(f"{CTFD_URL}/api/v1/teams/{team_id}/members",
                   json={"user_id": user_id}, timeout=15)
    data = r.json()
    if data.get("success"):
        log.info(f"  User {user_id} → team {team_id}")
    else:
        log.warning(f"  Could not add user {user_id} to team {team_id}: {data.get('errors', data)}")


def discover_challenges(admin: requests.Session) -> list[int]:
    """Return IDs of all Docker-type challenges."""
    r = admin.get(f"{CTFD_URL}/api/v1/challenges?view=admin", timeout=15)
    data = r.json()
    if not data.get("success"):
        log.error("Could not fetch challenge list")
        return []

    docker_ids = []
    for c in data.get("data", []):
        ctype = c.get("type", "")
        # CTFd challenge type name varies by how the plugin registers it
        if "docker" in ctype.lower():
            docker_ids.append(c["id"])

    if not docker_ids:
        # Fallback: probe each challenge for container configs
        log.info("No 'docker' type challenges found by name — probing all challenges for containers...")
        for c in data.get("data", []):
            cid = c["id"]
            r2 = admin.get(f"{CTFD_URL}/admin/docker/challenge/{cid}/containers", timeout=10)
            if r2.status_code == 200:
                d2 = r2.json()
                if d2.get("success") and d2.get("containers"):
                    docker_ids.append(cid)
                    log.info(f"  Challenge {cid} ({c.get('name', '?')}) has Docker containers")

    # Drop global (shared) challenges — they aren't per-user, so simulating
    # multiple users starting them isn't meaningful.
    per_user_ids = []
    for cid in docker_ids:
        r3 = admin.get(f"{CTFD_URL}/docker/api/challenge/{cid}/status", timeout=10)
        try:
            d3 = r3.json()
        except ValueError:
            d3 = {}
        if d3.get("is_global"):
            log.info(f"  Skipping challenge {cid} (global/shared container)")
            continue
        per_user_ids.append(cid)

    log.info(f"Docker challenges found: {per_user_ids or '(none)'}")
    return per_user_ids


# ──────────────────────────────────────────────────────────────────────────────
# Simulation worker
# ──────────────────────────────────────────────────────────────────────────────

def _probe_http(url: str) -> Optional[float]:
    """
    Poll `url` until it responds with a non-5xx status (container is up).
    Returns the response time in ms of the successful request, or None if the
    URL never responded within HTTP_PROBE_TIMEOUT.
    """
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"

    deadline = time.time() + HTTP_PROBE_TIMEOUT
    while time.time() < deadline:
        try:
            t0 = time.perf_counter()
            r = requests.get(url, timeout=5, allow_redirects=False)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if r.status_code < 500:
                return elapsed_ms
        except requests.RequestException:
            pass
        time.sleep(HTTP_PROBE_INTERVAL)
    return None


def _simulate_user(user: UserSession, challenge_ids: list[int], delay: float):
    time.sleep(delay)

    if not user.login():
        with _results_lock:
            _results["failed"] += 1
            _results["errors"].append(f"login failed: {user.username}")
        return

    for cid in challenge_ids:
        log.info(f"[{user.username}] → starting challenge {cid}")
        t0 = time.perf_counter()
        result = user.start_challenge(cid)
        startup_ms = (time.perf_counter() - t0) * 1000.0

        urls_to_probe: list[str] = []
        with _results_lock:
            if result.get("success"):
                _results["started"] += 1
                _results["startup_times_ms"].append(startup_ms)
                log.info(
                    f"[{user.username}] ✓ challenge {cid} started in {startup_ms:.1f}ms"
                )
            else:
                _results["failed"] += 1
                err = result.get("error", "unknown error")
                _results["errors"].append(f"{user.username}/ch{cid}: {err}")
                log.warning(f"[{user.username}] ✗ challenge {cid} ({startup_ms:.1f}ms): {err}")

        if result.get("success"):
            status = user.challenge_status(cid)
            for ct in status.get("containers", []):
                for pm in ct.get("port_mappings", []):
                    if pm.get("http") and (url := pm.get("url")):
                        log.info(f"[{user.username}]   {ct.get('label', 'container')} → {url}")
                        urls_to_probe.append(url)

        for url in urls_to_probe:
            http_ms = _probe_http(url)
            with _results_lock:
                if http_ms is not None:
                    _results["http_times_ms"].append(http_ms)
                    log.info(f"[{user.username}]   HTTP {url} → {http_ms:.1f}ms")
                else:
                    _results["http_failed"] += 1
                    _results["errors"].append(
                        f"{user.username}/ch{cid}: HTTP probe timed out for {url}"
                    )
                    log.warning(f"[{user.username}]   HTTP {url} → no response")
                    continue
            # Container responded — register for the load-probe phase
            with _started_urls_lock:
                _started_urls.append(url)

        if STOP_AFTER_START and result.get("success"):
            time.sleep(5)
            user.stop_challenge(cid)
            log.info(f"[{user.username}] stopped challenge {cid}")

        # Pause before next challenge start
        if len(challenge_ids) > 1:
            pause = random.uniform(*PAUSE_BETWEEN_STARTS)
            time.sleep(pause)


# ──────────────────────────────────────────────────────────────────────────────
# Roster + load probes
# ──────────────────────────────────────────────────────────────────────────────

def _build_roster(count: int) -> list[dict]:
    """Pick `count` accounts from USERS_TO_CREATE, generating extras if needed."""
    roster: list[dict] = []
    pool = list(USERS_TO_CREATE)
    for i in range(count):
        if i < len(pool):
            roster.append(pool[i])
        else:
            n = i + 1
            roster.append({
                "name": f"Sim User {n}",
                "email": f"{secrets.token_hex(8)}@test.local",
                "password": GENERATED_PASSWORD,
                "team": f"Team {n}",
            })
    return roster


def create_users_and_teams(admin: requests.Session, roster: list[dict]) -> list[dict]:
    log.info(f"── Creating {len(roster)} user(s) and team(s) ──")
    team_ids: dict[str, int] = {}

    for team_name in dict.fromkeys(u["team"] for u in roster if "team" in u):
        tid = create_team(admin, team_name)
        if tid:
            team_ids[team_name] = tid

    credentials = []
    for u in roster:
        uid = create_user(admin, u["name"], u["email"], u["password"])
        if uid and (team_name := u.get("team")) and team_name in team_ids:
            add_to_team(admin, team_ids[team_name], uid)
        credentials.append({"username": u["name"], "password": u["password"]})

    return credentials


def _probe_worker(jobs: list[str]):
    """Send one HTTP GET per entry in `jobs`, recording timing into results."""
    for url in jobs:
        target = url if url.startswith(("http://", "https://")) else f"http://{url}"
        try:
            t0 = time.perf_counter()
            r = requests.get(target, timeout=10, allow_redirects=False)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            ok = r.status_code < 500
        except requests.RequestException as exc:
            elapsed_ms = None
            ok = False
            err = str(exc)
        else:
            err = f"HTTP {r.status_code}" if not ok else None

        with _results_lock:
            if ok:
                _results["load_times_ms"].append(elapsed_ms)
            else:
                _results["load_failed"] += 1
                _results["errors"].append(f"load probe {target}: {err}")


def run_load_probes(total: int, concurrency: int):
    """Send `total` HTTP requests round-robin across all started container URLs."""
    with _started_urls_lock:
        urls = list(_started_urls)
    if not urls:
        log.warning("No HTTP container URLs available — skipping load probes.")
        return
    if total <= 0:
        return

    log.info(
        f"── Load-probe phase: {total} requests across {len(urls)} URL(s), "
        f"concurrency={concurrency} ──"
    )

    cycle = itertools.cycle(urls)
    schedule = [next(cycle) for _ in range(total)]
    random.shuffle(schedule)

    workers = max(1, min(concurrency, total))
    buckets: list[list[str]] = [[] for _ in range(workers)]
    for i, url in enumerate(schedule):
        buckets[i % workers].append(url)

    threads = [
        Thread(target=_probe_worker, args=(bucket,), daemon=True, name=f"probe-{i}")
        for i, bucket in enumerate(buckets)
    ]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.info(f"Load-probe phase finished in {time.time() - t0:.1f}s")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Create users, simulate Docker challenge starts, and load-probe their HTTP containers."
    )
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                   help=f"path to TOML config (default: {DEFAULT_CONFIG_PATH.name} next to this script)")
    p.add_argument("--users", type=int, default=None,
                   help="how many users to create (default: from config)")
    p.add_argument("--challenges", type=int, default=None,
                   help="challenges each user starts (default: from config)")
    p.add_argument("--probes", type=int, default=None,
                   help="total HTTP probe requests spread across started containers "
                        "(default: from config; 0 = readiness only)")
    p.add_argument("--probe-concurrency", type=int, default=None,
                   help="concurrent probe workers (default: from config)")
    p.add_argument("--duration", type=int, default=None,
                   help="timespan in seconds over which user actions are spread (default: from config)")
    p.add_argument("--existing", action="store_true",
                   help="use [[users.existing]] from config instead of creating new accounts")
    return p.parse_args()


def main():
    args = _parse_args()
    _load_config(args.config)

    # CLI flags override config defaults
    users_count = args.users if args.users is not None else DEFAULT_USERS
    challenges_per = args.challenges if args.challenges is not None else STARTS_PER_USER
    total_probes = args.probes if args.probes is not None else DEFAULT_TOTAL_PROBES
    probe_concurrency = (
        args.probe_concurrency if args.probe_concurrency is not None
        else DEFAULT_PROBE_CONCURRENCY
    )
    duration = args.duration if args.duration is not None else SIMULATION_DURATION

    if RANDOM_SEED is not None:
        random.seed(RANDOM_SEED)

    log.info(
        f"Users: {users_count}  |  Challenges/user: {challenges_per}  |  "
        f"Probes: {total_probes}  |  Duration: {duration}s"
    )

    admin = _admin_session()

    if args.existing:
        credentials = list(EXISTING_USERS)
        if not credentials:
            log.error("[[users.existing]] is empty in config — populate it or drop --existing.")
            sys.exit(1)
    else:
        roster = _build_roster(users_count)
        credentials = create_users_and_teams(admin, roster)

    if not credentials:
        log.error("No usable accounts — aborting.")
        sys.exit(1)

    challenge_ids = list(CHALLENGE_IDS) or discover_challenges(admin)
    if not challenge_ids:
        log.error(
            "No Docker challenges found.\n"
            "  • Set [challenges].ids = [1, 2, ...] in the config, or\n"
            "  • Ensure Docker challenges exist in CTFd."
        )
        sys.exit(1)

    log.info(f"Using challenges: {challenge_ids}")
    log.info(f"Simulating {len(credentials)} user(s) over {duration}s")

    starts_per_user = max(1, challenges_per)

    assignments = []
    for creds in credentials:
        chosen = random.sample(challenge_ids, k=min(starts_per_user, len(challenge_ids)))
        delay = random.uniform(0, duration)
        assignments.append((UserSession(creds["username"], creds["password"]), chosen, delay))

    assignments.sort(key=lambda x: x[2])
    log.info("Scheduled starts:")
    for user, chals, delay in assignments:
        log.info(f"  {user.username:20s}  t+{delay:6.1f}s  challenges={chals}")

    sim_start = time.time()
    threads = [
        Thread(
            target=_simulate_user,
            args=(user, chals, delay),
            daemon=True,
            name=user.username,
        )
        for user, chals, delay in assignments
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration + 120)

    sim_elapsed = time.time() - sim_start

    if total_probes > 0:
        run_load_probes(total_probes, probe_concurrency)

    elapsed = time.time() - sim_start

    def _fmt_stats(values: list[float]) -> str:
        if not values:
            return "n/a"
        s = sorted(values)
        avg = sum(s) / len(s)
        median = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
        return (
            f"avg={avg:8.1f}ms  median={median:8.1f}ms  "
            f"min={s[0]:8.1f}ms  max={s[-1]:8.1f}ms  n={len(s)}"
        )

    print()
    print("=" * 72)
    print(f"  SIMULATION COMPLETE  (sim {sim_elapsed:.1f}s, total {elapsed:.1f}s)")
    print(f"  Started      : {_results['started']}")
    print(f"  Failed       : {_results['failed']}")
    print(f"  HTTP failed  : {_results['http_failed']}")
    print(f"  Load failed  : {_results['load_failed']}")
    print("-" * 72)
    print(f"  Container startup : {_fmt_stats(_results['startup_times_ms'])}")
    print(f"  Readiness HTTP    : {_fmt_stats(_results['http_times_ms'])}")
    print(f"  Load probes       : {_fmt_stats(_results['load_times_ms'])}")
    if _results["errors"]:
        print("-" * 72)
        print(f"  Errors ({len(_results['errors'])}):")
        for e in _results["errors"][:50]:
            print(f"    • {e}")
        if len(_results["errors"]) > 50:
            print(f"    … and {len(_results['errors']) - 50} more")
    print("=" * 72)


if __name__ == "__main__":
    main()
