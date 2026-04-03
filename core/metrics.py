"""
Background metrics collector for Docker-Manager.

MetricsStore owns its OWN Docker clients and SSH clients — completely
separate from the shared clients in DockerManager / SSHPool.  This means
the background polling thread never touches the same connections as the
main Flask request thread, eliminating the SSH race condition that caused
the previous background-thread implementation to drop nodes.

Container stats (blocking per-container Docker API call) are collected
concurrently inside each per-node worker using a ThreadPoolExecutor.
For a localhost node docker-py uses the Unix socket (concurrent-safe).
For SSH nodes docker-py multiplexes over its own private paramiko
transport, so concurrent calls within the same client are fine there too.

The API endpoint reads from the ring-buffer without any Docker call,
so the monitoring page is never blocked by container count.
"""

import os
import time
import threading
import logging
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import paramiko
from docker import DockerClient
from paramiko.client import SSHClient

from .config import RuntimeConfig

log = logging.getLogger(__name__)

LABEL_TOKEN     = "ctfd-token"
LABEL_CHALLENGE = "ctfd-challenge-id"
LABEL_TEAM      = "ctfd-team-id"
LABEL_CTFD      = "ctfd"

_SSH_DIR       = Path("/home/ctfd/.ssh")
_SSH_KEY       = _SSH_DIR / "id_ed25519"
_KNOWN_HOSTS   = _SSH_DIR / "known_hosts"


# ------------------------------------------------------------------ #
# Data classes                                                         #
# ------------------------------------------------------------------ #

@dataclass
class ContainerMetric:
    token: str
    challenge: str
    team: str
    image: str
    node: str
    status: str
    cpu_percent: float
    mem_usage_mb: float
    mem_limit_mb: float

    def to_dict(self) -> dict:
        return {
            "token":         self.token,
            "challenge":     self.challenge,
            "team":          self.team,
            "image":         self.image,
            "node":          self.node,
            "status":        self.status,
            "cpu_percent":   self.cpu_percent,
            "mem_usage_mb":  self.mem_usage_mb,
            "mem_limit_mb":  self.mem_limit_mb,
        }


@dataclass
class NodeMetric:
    address: str
    name: str
    mem_total_mb: float
    used_mem_mb: float
    free_mem_mb: float
    running_count: int
    exited_count: int
    cpu_total_percent: float = 0.0   # sum of all container CPU % on this node

    def to_dict(self) -> dict:
        return {
            "address":           self.address,
            "name":              self.name,
            "mem_total_mb":      self.mem_total_mb,
            "used_mem_mb":       self.used_mem_mb,
            "free_mem_mb":       self.free_mem_mb,
            "running_count":     self.running_count,
            "exited_count":      self.exited_count,
            "cpu_total_percent": self.cpu_total_percent,
        }


@dataclass
class MetricsSnapshot:
    timestamp: float
    nodes:      List[NodeMetric]      = field(default_factory=list)
    containers: List[ContainerMetric] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp":  self.timestamp,
            "nodes":      [n.to_dict() for n in self.nodes],
            "containers": [c.to_dict() for c in self.containers],
        }


@dataclass
class LogEvent:
    timestamp: float
    level: str      # "info" | "warning" | "error"
    message: str

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "level":     self.level,
            "message":   self.message,
        }


# ------------------------------------------------------------------ #
# CPU helper                                                           #
# ------------------------------------------------------------------ #

def _calc_cpu_percent(stats: dict) -> float:
    """
    Derive CPU usage % from a single Docker stats response.

    Docker includes both the current and previous cpu_stats sample in one
    response, so a single call is sufficient for a meaningful delta.
    """
    try:
        cpu_delta = (
            stats["cpu_stats"]["cpu_usage"]["total_usage"]
            - stats["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        system_delta = (
            stats["cpu_stats"]["system_cpu_usage"]
            - stats["precpu_stats"].get("system_cpu_usage", 0)
        )
        num_cpus = stats["cpu_stats"].get("online_cpus") or len(
            stats["cpu_stats"]["cpu_usage"].get("percpu_usage", [1])
        )
        if system_delta > 0 and cpu_delta >= 0:
            return round((cpu_delta / system_delta) * num_cpus * 100.0, 2)
    except (KeyError, ZeroDivisionError, TypeError):
        pass
    return 0.0


# ------------------------------------------------------------------ #
# _NodeConfig – everything the background thread needs for one node   #
# ------------------------------------------------------------------ #

@dataclass
class _NodeConfig:
    address: str
    name: str
    docker: DockerClient
    ssh: Optional[SSHClient]   # None for localhost


# ------------------------------------------------------------------ #
# MetricsStore                                                         #
# ------------------------------------------------------------------ #

class MetricsStore:
    """
    Thread-safe ring-buffer populated by a daemon thread.

    The daemon thread owns its own Docker + SSH connections; it never
    touches the shared clients in DockerManager, so there is no
    cross-thread contention.

    The API endpoint calls ``latest()`` / ``history()`` which are
    pure in-memory reads — zero Docker calls, zero blocking.
    """

    def __init__(self):
        self._lock              = threading.RLock()
        self._history: deque    = deque(maxlen=RuntimeConfig.METRICS_HISTORY_SIZE)
        self._events: deque     = deque(maxlen=500)
        self._prev_tokens: set  = set()

        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._nodes: List[_NodeConfig] = []

    # ---------------------------------------------------------------- #
    # Lifecycle                                                          #
    # ---------------------------------------------------------------- #

    def start(self, dm_nodes) -> None:
        """
        Create dedicated connections for every node and start the
        background polling thread.

        ``dm_nodes`` is ``DockerManager.nodes`` — we only read .address
        and .name; we never touch .client.
        """
        self._nodes = self._build_node_configs(dm_nodes)
        if not self._nodes:
            log.warning("[Metrics] No nodes to monitor.")
            return

        # Resize the history deque if the configured size changed since __init__
        with self._lock:
            wanted = RuntimeConfig.METRICS_HISTORY_SIZE
            if self._history.maxlen != wanted:
                self._history = deque(self._history, maxlen=wanted)

        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="docker-metrics-poller",
            daemon=True,
        )
        self._thread.start()
        log.info("[Metrics] Background poller started (%d node(s), interval=%ds)",
                 len(self._nodes), RuntimeConfig.METRICS_POLL_INTERVAL)

    def stop(self) -> None:
        self._stop_evt.set()

    # ---------------------------------------------------------------- #
    # Public read API (all non-blocking)                                 #
    # ---------------------------------------------------------------- #

    def latest(self) -> Optional[MetricsSnapshot]:
        with self._lock:
            return self._history[-1] if self._history else None

    def history(self) -> List[MetricsSnapshot]:
        with self._lock:
            return list(self._history)

    def log_event(self, level: str, message: str) -> None:
        with self._lock:
            self._events.append(LogEvent(timestamp=time.time(), level=level, message=message))

    def recent_events(self, n: int = 100) -> List[LogEvent]:
        with self._lock:
            return list(self._events)[-n:]

    # ---------------------------------------------------------------- #
    # Connection setup                                                   #
    # ---------------------------------------------------------------- #

    def _build_node_configs(self, dm_nodes) -> List[_NodeConfig]:
        configs = []
        for node in dm_nodes:
            docker_client = self._make_docker_client(node)
            if docker_client is None:
                continue
            ssh_client = None
            if node.address != "localhost":
                ssh_client = self._make_ssh_client(node)
            configs.append(_NodeConfig(
                address=node.address,
                name=node.name,
                docker=docker_client,
                ssh=ssh_client,
            ))
        return configs

    def _make_docker_client(self, node) -> Optional[DockerClient]:
        try:
            if node.address == "localhost":
                return DockerClient.from_env()
            return DockerClient(base_url=f"ssh://{node.name}@{node.address}")
        except Exception as e:
            log.error("[Metrics] Cannot create Docker client for %s: %s", node.address, e)
            return None

    def _make_ssh_client(self, node) -> Optional[SSHClient]:
        """Create a fresh paramiko client used only for /proc/meminfo reads."""
        try:
            client = SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=node.address,
                username=node.name,
                key_filename=str(_SSH_KEY),
                allow_agent=True,
                look_for_keys=True,
                compress=True,
            )
            client.get_transport().set_keepalive(30)
            return client
        except Exception as e:
            log.warning("[Metrics] SSH meminfo client unavailable for %s: %s", node.address, e)
            return None

    # ---------------------------------------------------------------- #
    # Background polling                                                 #
    # ---------------------------------------------------------------- #

    def _poll_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                snapshot = self._collect_all()
                with self._lock:
                    self._history.append(snapshot)
                self._detect_changes(snapshot)
            except Exception as e:
                log.error("[Metrics] Poll error: %s", e, exc_info=True)
            self._stop_evt.wait(RuntimeConfig.METRICS_POLL_INTERVAL)

    def _collect_all(self) -> MetricsSnapshot:
        ts = time.time()
        all_nodes:      List[NodeMetric]      = []
        all_containers: List[ContainerMetric] = []

        for cfg in self._nodes:
            n_metric, c_metrics = self._collect_node(cfg)
            if n_metric:
                all_nodes.append(n_metric)
                all_containers.extend(c_metrics)

        return MetricsSnapshot(timestamp=ts, nodes=all_nodes, containers=all_containers)

    def _collect_node(self, cfg: _NodeConfig):
        """Collect one node's metrics.  Returns (NodeMetric | None, [ContainerMetric])."""
        try:
            info      = cfg.docker.info()
            mem_total = int(info.get("MemTotal", 0))
            free_mem  = self._node_free_mem(cfg)
            used_mem  = mem_total - free_mem

            raw_containers = cfg.docker.containers.list(
                all=True,
                filters={"label": [f"{LABEL_CTFD}=true"]},
            )

            # Collect stats for all running containers in parallel
            running_ctrs = [c for c in raw_containers if c.status == "running"]
            exited_ctrs  = [c for c in raw_containers if c.status != "running"]

            stats_map: Dict[str, dict] = {}
            if running_ctrs:
                workers = min(RuntimeConfig.METRICS_STATS_WORKERS, len(running_ctrs))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(c.stats, stream=False): c for c in running_ctrs}
                    for fut in as_completed(futures):
                        c = futures[fut]
                        try:
                            stats_map[c.id] = fut.result()
                        except Exception as e:
                            log.debug("[Metrics] stats() failed for %s: %s", c.id[:12], e)

            container_metrics: List[ContainerMetric] = []
            for c in raw_containers:
                labels = c.labels or {}
                try:
                    image_tag = c.image.tags[0] if c.image.tags else c.image.short_id
                except Exception:
                    image_tag = "?"

                if c.status == "running" and c.id in stats_map:
                    raw  = stats_map[c.id]
                    cpu  = _calc_cpu_percent(raw)
                    mems = raw.get("memory_stats", {})
                    mu   = mems.get("usage", 0) / (1024 ** 2)
                    ml   = mems.get("limit", 0) / (1024 ** 2)
                else:
                    cpu = mu = ml = 0.0

                container_metrics.append(ContainerMetric(
                    token        = labels.get(LABEL_TOKEN,     c.id[:12]),
                    challenge    = labels.get(LABEL_CHALLENGE, "?"),
                    team         = labels.get(LABEL_TEAM,      "?"),
                    image        = image_tag,
                    node         = cfg.address,
                    status       = c.status,
                    cpu_percent  = cpu,
                    mem_usage_mb = round(mu, 1),
                    mem_limit_mb = round(ml, 1),
                ))

            cpu_total = round(sum(c.cpu_percent for c in container_metrics), 2)

            node_metric = NodeMetric(
                address           = cfg.address,
                name              = cfg.name,
                mem_total_mb      = round(mem_total / (1024 ** 2), 1),
                used_mem_mb       = round(used_mem  / (1024 ** 2), 1),
                free_mem_mb       = round(free_mem  / (1024 ** 2), 1),
                running_count     = len(running_ctrs),
                exited_count      = len(exited_ctrs),
                cpu_total_percent = cpu_total,
            )
            return node_metric, container_metrics

        except Exception as e:
            log.error("[Metrics] Node %s collection failed: %s", cfg.address, e)
            return None, []

    def _node_free_mem(self, cfg: _NodeConfig) -> int:
        """Read MemAvailable from /proc/meminfo using this store's own connections."""
        if cfg.address == "localhost":
            with open("/proc/meminfo") as f:
                for line in f:
                    if "MemAvailable" in line:
                        return int(line.split()[1]) * 1024
            return 0

        ssh = cfg.ssh
        if ssh is None:
            return 0
        try:
            transport = ssh.get_transport()
            if transport is None or not transport.is_active():
                # Reconnect the dedicated SSH client for this node
                node_stub = type("N", (), {"address": cfg.address, "name": cfg.name})()
                cfg.ssh = self._make_ssh_client(node_stub)
                ssh = cfg.ssh
                if ssh is None:
                    return 0
            _, stdout, _ = ssh.exec_command("grep MemAvailable /proc/meminfo")
            return int(stdout.read().decode().split()[1]) * 1024
        except Exception as e:
            log.warning("[Metrics] meminfo failed for %s: %s", cfg.address, e)
            return 0

    # ---------------------------------------------------------------- #
    # Change detection                                                   #
    # ---------------------------------------------------------------- #

    def _detect_changes(self, snapshot: MetricsSnapshot) -> None:
        current = {c.token for c in snapshot.containers}

        for token in current - self._prev_tokens:
            c = next((x for x in snapshot.containers if x.token == token), None)
            if c:
                self.log_event("info",
                    f"Container detected: {c.challenge} (team {c.team}) on {c.node}")

        for token in self._prev_tokens - current:
            self.log_event("warning", f"Container removed: token {token}")

        self._prev_tokens = current
