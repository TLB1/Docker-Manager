import json
import logging
import os
import random
import subprocess
import tarfile
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import docker
import requests.exceptions
from docker import DockerClient
from docker.errors import APIError, NotFound
from docker.models.containers import Container
from docker.types import IPAMPool
from paramiko.ssh_exception import ChannelException, SSHException

_TRANSPORT_ERRORS = (
    ChannelException,
    SSHException,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    ConnectionResetError,
    BrokenPipeError,
    EOFError,
)

from .labels import DockerLabels
from .ssh import SSHPool
from .config import RuntimeConfig
from .ports import PortsManager
from .registry import RegistryManager
from .timer import RunnableTimer
from .cache import ContainerCache, CachedContainer
from ..models.node import Node
from ..models.container import ContainerDetails

log = logging.getLogger(__name__)


# ── Container spec ──────────────────────────────────────────────────────────

@dataclass
class ContainerSpec:
    """
    Describes one container within a multi-container challenge.

    Attributes:
        image           Docker image to run.
        network_alias   Hostname other containers in the challenge use to reach
                        this one (e.g. "internal", "db"). Set as a Docker
                        network alias so `http://internal/` just works.
        expose_port     When True (default) the manager allocates a host port
                        and the caller can give players the URL.
                        When False the container runs on the shared challenge
                        network only — no host port is bound or allocated.
        container_port  Primary container port to publish when expose_port=True.
        port_mappings   Rich multi-port config (overrides container_port).
                        List of {"container_port": int, "http": bool, "label": str}.
    """
    image: str
    network_alias: str
    expose_port: bool = True
    container_port: int = None
    port_mappings: list = field(default_factory=list)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_docker_client_over_ssh(ssh_pool: SSHPool, node: Node) -> DockerClient:
    """
    Build a DockerClient that tunnels through the SSHPool's existing paramiko
    transport instead of opening a second independent SSH connection.
    """
    return DockerClient(base_url=f"ssh://{node.name}@{node.address}")


def _build_docker_client(node: Node) -> Optional[DockerClient]:
    """
    Build a fresh DockerClient for *node* and enable SSH keepalive on the
    underlying paramiko transport.

    docker-py's SSH backend creates its own paramiko client but does NOT set
    TCP keepalive, so an idle or slow transport can be silently killed by
    sshd's ClientAliveInterval or an NAT middlebox. The next Docker call then
    sees the half-open connection close mid-request and raises
    ``RemoteDisconnected``. Setting the keepalive interval here keeps the
    transport healthy across the quieter windows between polling cycles.

    Returns None if the SSH transport cannot be established. Plugin load must
    survive an unreachable worker so admins can fix WORKER_NODES from the UI.
    """
    try:
        client = DockerClient(base_url=f"ssh://{node.name}@{node.address}")
    except Exception as e:
        log.warning(f"[DockerManager] Could not connect to {node}: {e}")
        return None
    try:
        adapter = client.api.get_adapter(client.api.base_url)
        ssh_client = getattr(adapter, "ssh_client", None)
        transport = ssh_client.get_transport() if ssh_client else None
        if transport is not None:
            transport.set_keepalive(30)
    except Exception as e:
        log.debug(f"[DockerManager] Could not set keepalive on {node}: {e}")
    return client


# ── Manager ─────────────────────────────────────────────────────────────────

class DockerManager:

    def __init__(self, base_urls: Optional[Iterable[str]] = None):
        self.nodes: list[Node] = []

        if base_urls:
            for url in base_urls:
                host = url.split("@")[-1]
                name = url.split("@")[0]
                self.nodes.append(Node(name, host))
        else:
            self.nodes.append(Node("localhost", "localhost", client=DockerClient.from_env()))

        self.ssh_pool = SSHPool(nodes=self.nodes)

        if base_urls:
            for node in self.nodes:
                if node.client is None:
                    node.client = _build_docker_client(node)

        self.ports_manager = PortsManager()
        self.registry = RegistryManager()
        self.timer_timeout = RunnableTimer()
        self.timer_kill = RunnableTimer()
        self._node_index = 0
        self._sync_events: dict[str, threading.Event] = {}
        self._sync_events_lock = threading.Lock()
        self._container_cache = ContainerCache(RuntimeConfig.CONTAINER_CACHE_TTL)
        # Per-(node, network-name) locks serialize concurrent check-and-create
        # calls so the same network can't be created twice in parallel.
        self._network_locks: dict[tuple, threading.Lock] = {}
        self._network_locks_lock = threading.Lock()

        # Rearm timers for any ctfd-labelled containers already present. This
        # is a no-op in the common case because load() calls delete_all right
        # after __init__, but if delete_all fails on a specific container
        # (e.g. transient SSH error on one node) the survivor would otherwise
        # be left without a kill timer and linger forever. set_timers uses
        # absolute durations, so each survivor gets a fresh suspension + kill
        # cycle rather than inheriting stale timestamps.
        self._rearm_existing_timers()

    def _rearm_existing_timers(self):
        for node in self.nodes:
            try:
                containers = self._node_call(
                    node,
                    lambda c: c.containers.list(
                        all=True,
                        filters={"label": [f"{DockerLabels.CTFD}=true"]},
                    ),
                )
            except Exception as e:
                log.warning(f"[DockerManager] rearm: list failed on {node}: {e}")
                continue
            for container in containers:
                token = container.labels.get(DockerLabels.TOKEN)
                if token:
                    self.set_timers(token)

    # ------------------------------------------------------------------ #
    # Nginx                                                                #
    # ------------------------------------------------------------------ #

    def update_nginx_data(self):
        config_lines = [
            "map $host $ctfd_host {",
            f"    default {RuntimeConfig.CTFD_DOMAIN_NAME};",
            "}",
        ]
        with open("/opt/CTFd/CTFd/plugins/Docker-Manager/nginx/data/data_map.conf", "w") as f:
            f.write("\n".join(config_lines))
        with open("/opt/CTFd/CTFd/plugins/Docker-Manager/nginx/data/server_name.conf", "w") as f:
            f.write(f"server_name *.{RuntimeConfig.CTFD_DOMAIN_NAME};\n")

        client = docker.from_env()
        container = client.containers.get("ctfd-nginx-proxy")
        container.exec_run("nginx -s reload")

    # ------------------------------------------------------------------ #
    # SSH / Docker client reconnect                                        #
    # ------------------------------------------------------------------ #

    def _reconnect_node(self, node: Node):
        """Tear down and rebuild the Docker client for a node.

        Sets node.client = None on failure rather than raising, so subsequent
        callers can detect the unreachable state and surface a clean error
        instead of crashing on AttributeError.
        """
        try:
            if node.client is not None:
                node.client.close()
        except Exception:
            pass
        node.client = _build_docker_client(node)
        if node.client is None:
            raise RuntimeError(f"Failed to reconnect Docker client for {node}")

    def _node_call(self, node: Node, op, retries: Optional[int] = None):
        """
        Execute *op(node.client)* with SSH-session throttling and retry.

        The operation is invoked as ``op(client)`` where *client* is the
        node's CURRENT DockerClient. This matters because on a transport
        failure we rebuild ``node.client``; re-invoking the lambda picks up
        the fresh client instead of calling into the dead one.

        Retries on transport-level errors only:
        - ChannelException / SSHException (paramiko): channel exhaustion or
          transport protocol error. Channel errors usually mean sshd's
          MaxSessions was momentarily saturated — transport is still healthy.
        - requests.exceptions.ConnectionError wrapping RemoteDisconnected:
          docker-py's HTTP-over-SSH transport died. Reconnect immediately.

        Higher-level Docker errors (container name conflict, 404, etc.) are
        NOT retried — those are caller bugs, not infrastructure.
        """
        if retries is None:
            retries = RuntimeConfig.NODE_SSH_RETRIES

        last_exc = None
        for attempt in range(retries + 1):
            try:
                if node.client is None:
                    # Node was unreachable at startup (or last reconnect failed).
                    # Try once per attempt to bring it back; if it still won't
                    # connect, surface a clean transport error so callers don't
                    # explode on AttributeError.
                    try:
                        self._reconnect_node(node)
                    except Exception as re:
                        raise requests.exceptions.ConnectionError(
                            f"Node {node} is not connected: {re}"
                        )
                with node.ssh_semaphore:
                    return op(node.client)
            except _TRANSPORT_ERRORS as e:
                last_exc = e
                if attempt >= retries:
                    break
                # RemoteDisconnected / ConnectionError mean the transport is
                # dead - rebuild the client before the next attempt.
                # ChannelException usually means MaxSessions was momentarily
                # exhausted; the transport is still fine, so only rebuild
                # after the first retry fails.
                transport_dead = isinstance(e, (
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                    ConnectionResetError,
                    BrokenPipeError,
                    EOFError,
                ))
                if transport_dead or attempt >= 1:
                    log.warning(
                        f"[DockerManager] Transport error on {node} "
                        f"(attempt {attempt + 1}/{retries + 1}), reconnecting: {e}"
                    )
                    try:
                        self._reconnect_node(node)
                    except Exception as re:
                        log.warning(f"[DockerManager] Reconnect failed on {node}: {re}")
                backoff = min(0.5 * (2 ** attempt), 4.0) + random.uniform(0, 0.25)
                time.sleep(backoff)
        raise last_exc

    # ------------------------------------------------------------------ #
    # Container cache                                                      #
    # ------------------------------------------------------------------ #

    def _refresh_cache(self):
        """Query every node and rebuild the container metadata cache."""
        containers_by_node: Dict[str, list] = {}
        for node in self.nodes:
            try:
                containers = self._node_call(
                    node,
                    lambda c: c.containers.list(
                        all=True,
                        filters={"label": [f"{DockerLabels.CTFD}=true"]},
                    ),
                )
                containers_by_node[node.address] = containers
            except Exception as e:
                log.warning(f"[DockerManager] Cache refresh failed for {node}: {e}")
                containers_by_node[node.address] = []
        self._container_cache.rebuild(containers_by_node)

    def _ensure_cache_fresh(self):
        """Refresh the cache if it has exceeded its TTL."""
        if self._container_cache.is_stale():
            self._refresh_cache()

    # ------------------------------------------------------------------ #
    # Timers                                                               #
    # ------------------------------------------------------------------ #

    def set_timers(self, token: str):
        self.timer_timeout.startOrRenew(
            token,
            RuntimeConfig.CONTAINER_SUSPENSION_INTERVAL,
            lambda: self.suspend_container(token),
        )
        self.timer_kill.startOrRenew(
            token,
            RuntimeConfig.DOCKER_CONTAINER_LIFETIME,
            lambda: self.remove_container(token),
        )

    # ------------------------------------------------------------------ #
    # Container queries                                                    #
    # ------------------------------------------------------------------ #

    def _query_containers(self, **kwargs) -> List[Container]:
        results: List[Container] = []
        for node in self.nodes:
            try:
                containers = self._node_call(node, lambda c: c.containers.list(**kwargs))
                results.extend(containers)
            except Exception as e:
                log.error(f"[DockerManager] Failed to query containers on {node}: {e}")
        return results

    def running_containers_for_team(self, team_id) -> List[Container]:
        return self._query_containers(
            all=True,
            filters={"label": [f"{DockerLabels.TEAM}={team_id}"]},
        )

    def get_container_for_team_challenge(
        self,
        team_id: int,
        challenge_id: int,
        container_index: Optional[int] = None,
    ) -> Optional[Container]:
        """
        Look up a container by team + challenge, optionally narrowed to a
        specific container_index within a multi-container challenge.
        Returns the first match, or None.
        """
        labels = [
            f"{DockerLabels.TEAM}={team_id}",
            f"{DockerLabels.CHALLENGE}={challenge_id}",
        ]
        if container_index is not None:
            labels.append(f"{DockerLabels.CONTAINER_INDEX}={container_index}")

        containers = self._query_containers(all=True, filters={"label": labels})
        return containers[0] if containers else None

    def get_containers_for_team_challenge(
        self,
        team_id: int,
        challenge_id: int,
    ) -> List[Container]:
        """Return ALL containers for a team+challenge (one per container_index)."""
        return self._query_containers(
            all=True,
            filters={"label": [
                f"{DockerLabels.TEAM}={team_id}",
                f"{DockerLabels.CHALLENGE}={challenge_id}",
            ]},
        )

    def running_containers(self, client: DockerClient) -> List[Container]:
        return client.containers.list(
            filters={"label": [f"{DockerLabels.CTFD}=true"]},
        )

    def get_container_by_token(self, token: str) -> Optional[Container]:
        containers = self._query_containers(
            all=True,
            filters={"label": [f"{DockerLabels.TOKEN}={token}"]},
        )
        return containers[0] if containers else None

    def _find_node_for_container(self, container: Container) -> Optional[Node]:
        """Return the Node that owns *container*, or None."""
        for node in self.nodes:
            try:
                containers = self._node_call(
                    node,
                    lambda c: c.containers.list(all=True, filters={"id": container.id}),
                )
                if any(c.id == container.id for c in containers):
                    return node
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------ #
    # Container quota                                                      #
    # ------------------------------------------------------------------ #

    def can_create_container(self, team_id) -> bool:
        """
        Check whether the team is within the per-group container quota.
        Counts distinct challenges rather than raw containers so that
        multi-container challenges consume one quota slot.
        """
        self._ensure_cache_fresh()
        cached = self._container_cache.get_by_team(str(team_id))
        challenge_ids = {c.challenge_id for c in cached if c.challenge_id}
        return len(challenge_ids) < RuntimeConfig.MAX_ACTIVE_CONTAINERS_PER_GROUP

    # ------------------------------------------------------------------ #
    # Network management                                                   #
    # ------------------------------------------------------------------ #

    def _challenge_network_name(self, challenge_id, team_id) -> str:
        hex_hash = hex(hash(str(team_id) + str(challenge_id)))
        return f"ctfd-challenge-network-{hex_hash}"

    def _network_lock(self, node: Node, network_name: str) -> threading.Lock:
        """Return (lazily-creating) the per-(node, network) mutex."""
        key = (node.address, network_name)
        with self._network_locks_lock:
            lock = self._network_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._network_locks[key] = lock
            return lock

    def _get_or_create_network(self, node: Node, challenge_id, team_id) -> str:
        """
        Ensure the per-challenge Docker bridge network exists on *node*
        before any container is started on it.  Returns the network name.

        Raises on failure — callers must not silently proceed without a network.

        Serialized per (node, network_name) so concurrent calls for containers
        in the same multi-container challenge can't both race into create().
        A 409 conflict is still tolerated as a safety net in case another
        process (e.g. a parallel request handler on a different worker) beats
        us to it.
        """
        network_name = self._challenge_network_name(challenge_id, team_id)
        with self._network_lock(node, network_name):
            existing = self._node_call(node, lambda c: c.networks.list(names=[network_name]))
            if existing:
                return network_name
            try:
                self._node_call(node, lambda c: c.networks.create(network_name, driver="bridge"))
                log.debug(f"[DockerManager] Created network {network_name} on {node}")
            except APIError as e:
                if e.status_code == 409 or "already exists" in str(e).lower():
                    log.debug(
                        f"[DockerManager] Network {network_name} already exists on {node} (raced)"
                    )
                else:
                    raise
        return network_name

    def _purge_challenge_networks(self):
        """
        Remove every ctfd-challenge-network-* network on every node that has
        no containers attached.  Called from delete_all() so orphaned networks
        from previous runs don't accumulate and exhaust Docker's subnet pool.
        """
        prefix = "ctfd-challenge-network-"
        for node in self.nodes:
            try:
                networks = self._node_call(node, lambda c: c.networks.list(names=[f"{prefix}*"]))
                for net in networks:
                    try:
                        # net.reload/remove are bound to the Network's original
                        # client. If the transport died, reload() will fail and
                        # we skip this net — next purge cycle catches it.
                        self._node_call(node, lambda _c, n=net: n.reload())
                        if not net.attrs.get("Containers"):
                            self._node_call(node, lambda _c, n=net: n.remove())
                            log.debug(f"[DockerManager] Purged network {net.name} on {node}")
                    except Exception as e:
                        log.debug(f"[DockerManager] Could not purge network {net.name} on {node}: {e}")
            except Exception as e:
                log.warning(f"[DockerManager] Network purge failed on {node}: {e}")

    def _cleanup_challenge_network(self, node: Node, challenge_id, team_id):
        """Remove the per-challenge network only when no containers reference it.

        docker-py's network.containers only lists *running* containers.
        network.attrs["Containers"] (populated after reload) includes stopped
        and paused containers too — we must check that to avoid deleting a
        network that suspended containers still need to re-attach to on resume.
        """
        network_name = self._challenge_network_name(challenge_id, team_id)
        try:
            networks = self._node_call(node, lambda c: c.networks.list(names=[network_name]))
            if not networks:
                return
            network = networks[0]
            self._node_call(node, lambda _c, n=network: n.reload())
            # attrs["Containers"] is a dict keyed by container-id; it includes
            # every container connected to the network regardless of state.
            connected = network.attrs.get("Containers", {})
            if not connected:
                self._node_call(node, lambda _c, n=network: n.remove())
                log.debug(f"[DockerManager] Removed empty network {network_name} on {node}")
            else:
                log.debug(
                    f"[DockerManager] Network {network_name} still has "
                    f"{len(connected)} container(s), skipping removal"
                )
        except Exception as e:
            log.debug(f"[DockerManager] Network cleanup {network_name} on {node}: {e}")

    def _connect_with_alias(self, node: Node, network_name: str, container: Container, alias: str):
        """
        Connect *container* to *network_name* with a DNS alias so other
        containers in the network can reach it by hostname.
        """
        try:
            network = self._node_call(node, lambda c: c.networks.get(network_name))
            self._node_call(node, lambda _c, n=network: n.connect(container, aliases=[alias]))
        except Exception as e:
            log.warning(
                f"[DockerManager] Could not connect {container.name} "
                f"to {network_name} as '{alias}': {e}"
            )

    # ------------------------------------------------------------------ #
    # Single-container creation (internal helper)                         #
    # ------------------------------------------------------------------ #

    def _create_one_container(
        self,
        node: Node,
        team_id,
        challenge_id,
        image: str,
        network_name: Optional[str],
        network_alias: str,
        expose_port: bool,
        port_mappings: list,
        container_port: int,
        container_index: int,
    ) -> str:
        """
        Low-level: spin up a single container and return its token.

        When *network_name* is provided the container joins that network with
        *network_alias* so peer containers can reach it by hostname.
        When *network_name* is None the container starts on Docker's default
        bridge - useful for single-container challenges that don't need
        inter-container communication.

        When expose_port=False no host port is allocated or bound -
        the container is only reachable from within the challenge network.
        """
        token = f"{secrets.randbits(48):08x}"

        # ── Build port bindings ──────────────────────────────────────────
        docker_ports: Dict[str, Optional[int]] = {}

        if expose_port:
            if port_mappings:
                ports_to_bind = [
                    {"container_port": int(pm["container_port"]), "http": pm.get("http", True)}
                    for pm in port_mappings
                    if pm.get("container_port")
                ]
            else:
                ports_to_bind = [{"container_port": container_port or 80, "http": True}]

            if not ports_to_bind:
                ports_to_bind = [{"container_port": 80, "http": True}]

            # Primary port
            primary_cp = ports_to_bind[0]["container_port"]
            primary_hp = self.ports_manager.allocate_port(token, node.address)
            docker_ports[f"{primary_cp}/tcp"] = primary_hp

            # Additional ports
            for pm in ports_to_bind[1:]:
                cp = pm["container_port"]
                hp = self.ports_manager.allocate_extra_node_port(token, cp, node.address)
                docker_ports[f"{cp}/tcp"] = hp
                if not pm.get("http", True):
                    self.ports_manager.allocate_tcp_port(token, cp, node.address, hp)

            if not ports_to_bind[0].get("http", True):
                self.ports_manager.allocate_tcp_port(
                    token, primary_cp, node.address, primary_hp
                )

        log.info(
            f"[DockerManager] Starting {image} [{container_index}] on {node.address} "
            + (f"network={network_name!r} alias={network_alias!r} " if network_name else "no-network ")
            + f"ports={list(docker_ports.values()) or 'none (internal)'}"
        )

        # ── Build run kwargs ─────────────────────────────────────────────
        labels = {
            DockerLabels.CTFD: "true",
            DockerLabels.TEAM: str(team_id),
            DockerLabels.CHALLENGE: str(challenge_id),
            DockerLabels.TOKEN: token,
            DockerLabels.CONTAINER_INDEX: str(container_index),
        }

        # Deterministic name so a transport-error retry of containers.create()
        # collides with the prior partial success (Docker returns 409 instead of
        # silently creating a second container with the same labels). Without
        # this, _node_call's retry loop can spawn duplicates that the kill
        # timer's containers[0]-only removal won't catch — they linger forever.
        run_kwargs: Dict = dict(
            image=image,
            detach=True,
            name=f"ctfd-{token}",
            mem_limit=str(RuntimeConfig.MEM_LIMIT_PER_CONTAINER),
            cpu_quota=RuntimeConfig.DOCKER_CONTAINER_CPU_QUOTA,
            ports=docker_ports if docker_ports else None,
            labels=labels,
        )

        if network_name:
            labels[DockerLabels.NETWORK_ALIAS] = network_alias
            # Do NOT pass network/networking_config to create; docker-py's
            # handling of those kwargs differs across versions and may silently
            # discard our aliases or set an unexpected NetworkMode.
            # We connect explicitly after creation (see below).

        # ── Step 1: create (do not start yet) ───────────────────────────
        try:
            container = self._node_call(node, lambda c: c.containers.create(**run_kwargs))
        except _TRANSPORT_ERRORS as e:
            log.warning(
                f"[DockerManager] Transport error after retries during containers.create(), "
                f"checking if container was created: {e}"
            )
            existing = self.get_container_by_token(token)
            if existing:
                self._container_cache.add(CachedContainer.from_docker(existing, node.address))
                return token
            if expose_port:
                self.ports_manager.release_port(token)
            raise
        except APIError as e:
            # 409 here means a previous _node_call retry already created the
            # container server-side (response was lost in transit). The
            # deterministic name made the retry collide instead of producing a
            # silent duplicate; claim the existing one.
            if e.status_code == 409 or "already in use" in str(e).lower():
                log.warning(
                    f"[DockerManager] Container name ctfd-{token} already exists, "
                    f"claiming the prior partial create: {e}"
                )
                existing = self.get_container_by_token(token)
                if existing:
                    self._container_cache.add(CachedContainer.from_docker(existing, node.address))
                    return token
            if expose_port:
                self.ports_manager.release_port(token)
            raise
        except Exception:
            if expose_port:
                self.ports_manager.release_port(token)
            raise

        # After create we re-fetch the container by token when retrying
        # destructive operations; the Container object returned by create()
        # binds to the old client and won't survive a reconnect.
        container_id = container.id

        def _fetch_container(c):
            try:
                return c.containers.get(container_id)
            except Exception:
                return container

        # Step 2: connect to the challenge network with alias
        # containers.create() with no `network` kwarg attaches the container
        # to Docker's default `bridge` network. Calling connect() then ADDS
        # the challenge network on top;  it does not replace the default
        # attachment, so without the disconnect below every container would
        # sit on both networks and challenges could reach each other via
        # the shared default bridge.
        if network_name:
            try:
                self._node_call(
                    node,
                    lambda c: c.networks.get(network_name).connect(
                        _fetch_container(c), aliases=[network_alias]
                    ),
                )
            except Exception as e:
                log.error(
                    f"[DockerManager] Failed to connect {token} to {network_name}: {e}"
                )
                try:
                    self._node_call(node, lambda c: _fetch_container(c).remove(force=True))
                except Exception:
                    pass
                if expose_port:
                    self.ports_manager.release_port(token)
                raise RuntimeError(
                    f"Could not connect container to network {network_name}: {e}"
                ) from e

            try:
                self._node_call(
                    node,
                    lambda c: c.networks.get("bridge").disconnect(_fetch_container(c)),
                )
            except NotFound:
                pass
            except APIError as e:
                # 403 = container not connected to bridge (already isolated).
                if e.status_code != 403:
                    log.warning(
                        f"[DockerManager] Could not detach {token} from default bridge: {e}"
                    )
            except Exception as e:
                log.warning(
                    f"[DockerManager] Could not detach {token} from default bridge: {e}"
                )

        # ── Step 3: start ────────────────────────────────────────────────
        try:
            self._node_call(node, lambda c: _fetch_container(c).start())
        except _TRANSPORT_ERRORS as e:
            log.warning(f"[DockerManager] Transport error after retries during container.start(): {e}")
            existing = self.get_container_by_token(token)
            if existing:
                self._container_cache.add(CachedContainer.from_docker(existing, node.address))
                return token
            try:
                self._node_call(node, lambda c: _fetch_container(c).remove(force=True))
            except Exception:
                pass
            if expose_port:
                self.ports_manager.release_port(token)
            raise
        except Exception:
            try:
                self._node_call(node, lambda c: _fetch_container(c).remove(force=True))
            except Exception:
                pass
            if expose_port:
                self.ports_manager.release_port(token)
            raise

        try:
            container = self._node_call(node, lambda c: c.containers.get(container_id))
        except Exception:
            pass
        self._container_cache.add(CachedContainer.from_docker(container, node.address))
        return token

    # ------------------------------------------------------------------ #
    # Public: create containers                                            #
    # ------------------------------------------------------------------ #

    def create_challenge_containers(
        self,
        team_id,
        challenge_id,
        specs: List[ContainerSpec],
        use_network: bool = True,
    ) -> List[str]:
        """
        Spin up all containers for a multi-container challenge in one call.

        Containers are started in the order given so dependencies (e.g. an
        internal service that a gateway probes on startup) come first.

        Returns a list of tokens in the same order as *specs*.
        The token for a spec with expose_port=False is still valid for
        suspend/resume/remove - it just has no host port allocated.
        """
        if not self.can_create_container(team_id):
            raise Exception("Team container quota exceeded")

        node = self._node_for_team_challenge(team_id, challenge_id)

        # Create the shared network BEFORE starting any container so the
        # first container's alias is resolvable the moment it starts.
        network_name = self._get_or_create_network(node, challenge_id, team_id) if use_network else None

        tokens: List[str] = []
        for index, spec in enumerate(specs):
            token = self._create_one_container(
                node=node,
                team_id=team_id,
                challenge_id=challenge_id,
                image=spec.image,
                network_name=network_name,
                network_alias=spec.network_alias,
                expose_port=spec.expose_port,
                port_mappings=spec.port_mappings,
                container_port=spec.container_port,
                container_index=index,
            )
            tokens.append(token)

        # Start timers keyed on each token individually so suspend/kill
        # fire per-container (matching the existing single-container behaviour).
        for token in tokens:
            self.set_timers(token)

        return tokens

    def create_container(
        self,
        team_id,
        challenge_id,
        image,
        port_mappings: list = None,
        container_port: int = 80,
        container_index: int = 0,
        network_alias: str = "",
        expose_port: bool = True,
    ) -> str:
        """
        Spin up a single container and return its token.

        This is the original single-container entrypoint, now delegating to
        the shared _create_one_container helper so the two paths stay in sync.

        For multi-container challenges prefer create_challenge_containers()
        which handles ordering, quota, and network creation in one call.
        """
        if not self.can_create_container(team_id):
            raise Exception("Team container quota exceeded")

        node = self._node_for_team_challenge(team_id, challenge_id)

        # Network must exist before the container starts.
        network_name = self._get_or_create_network(node, challenge_id, team_id)

        # Default alias to image name (last path component, no tag) when
        # the caller doesn't specify one.
        alias = network_alias or image.split("/")[-1].split(":")[0]

        token = self._create_one_container(
            node=node,
            team_id=team_id,
            challenge_id=challenge_id,
            image=image,
            network_name=network_name,
            network_alias=alias,
            expose_port=expose_port,
            port_mappings=port_mappings or [],
            container_port=container_port,
            container_index=container_index,
        )

        self.set_timers(token)
        return token

    # ------------------------------------------------------------------ #
    # Container lifecycle                                                  #
    # ------------------------------------------------------------------ #

    def suspend_container(self, token: str) -> bool:
        container = self.get_container_by_token(token)
        if not container:
            return False
        node = self._find_node_for_container(container) or self.nodes[0]
        cid = container.id
        try:
            self._node_call(node, lambda c: c.containers.get(cid).stop())
            self._container_cache.update_status(token, "exited")
            return True
        except Exception:
            return False

    def resume_container(self, token: str) -> bool:
        container = self.get_container_by_token(token)
        if not container:
            return False

        # The per-challenge network is removed by _cleanup_challenge_network
        # when a container is suspended (no running containers remain attached).
        # Re-create it on the correct node before starting so Docker can
        # re-attach the container to its configured network.
        challenge_id = container.labels.get(DockerLabels.CHALLENGE)
        team_id      = container.labels.get(DockerLabels.TEAM)
        node         = self._find_node_for_container(container)
        if challenge_id and team_id and node:
            self._get_or_create_network(node, challenge_id, team_id)

        cid = container.id
        try:
            self._node_call(node or self.nodes[0], lambda c: c.containers.get(cid).start())
        except Exception as e:
            log.error(f"[DockerManager] Failed to start container {token}: {e}")
            return False

        self._container_cache.update_status(token, "running")
        self.set_timers(token)
        return True

    def remove_container(self, token: str) -> bool:
        # Inline the lookup so we can tell "container is genuinely gone" apart
        # from "at least one node failed to answer". The old get_container_by_token
        # path swallowed per-node transport errors and returned None, so a
        # transient SSH blip at kill-time would drop the timer and leak the
        # container forever.
        #
        # We collect EVERY container matching the token across every node.
        # A retry of containers.create() across a flaky SSH
        # transport could in principle leave duplicates with the same labels
        # (the deterministic-name guard now prevents this for new containers,
        # but pre-existing duplicates still need to be swept).
        matches: list[tuple[Node, Container]] = []
        any_query_failed = False
        for node in self.nodes:
            try:
                containers = self._node_call(
                    node,
                    lambda c: c.containers.list(
                        all=True,
                        filters={"label": [f"{DockerLabels.TOKEN}={token}"]},
                    ),
                )
                for c in containers:
                    matches.append((node, c))
            except Exception as e:
                log.warning(f"[DockerManager] remove_container lookup on {node} failed: {e}")
                any_query_failed = True

        if not matches:
            if any_query_failed:
                log.warning(
                    f"[DockerManager] remove_container({token}): lookup inconclusive; "
                    f"rescheduling kill in 60s."
                )
                self.timer_kill.startOrRenew(
                    token, 60, lambda: self.remove_container(token)
                )
            return False

        # Only cancel timers once we've confirmed at least one container exists.
        # If we cancelled on an inconclusive lookup we'd throw away the kill
        # timer for a container we couldn't actually verify was gone.
        self.timer_timeout.cancel(token)
        self.timer_kill.cancel(token)

        challenge_id = matches[0][1].labels.get(DockerLabels.CHALLENGE)
        team_id = matches[0][1].labels.get(DockerLabels.TEAM)

        all_removed = True
        last_error: Optional[Exception] = None
        removed_nodes: set = set()
        for node, c in matches:
            cid = c.id
            try:
                self._node_call(
                    node,
                    lambda dc, _cid=cid: dc.containers.get(_cid).remove(force=True),
                )
                removed_nodes.add(node)
            except NotFound:
                # Already gone on this node
                continue
            except Exception as e:
                last_error = e
                all_removed = False

        if not all_removed:
            log.warning(
                f"[DockerManager] Failed to remove all containers for token {token} "
                f"(last error: {last_error}); rescheduling kill in 60s."
            )
            self.timer_kill.startOrRenew(
                token, 60, lambda: self.remove_container(token)
            )
            return False

        self._container_cache.remove(token)
        self.ports_manager.release_port(token)
        if challenge_id:
            for node in removed_nodes:
                self._cleanup_challenge_network(node, challenge_id, team_id)
        return True

    def delete_all(self) -> int:
        """
        Remove every CTFd-labelled container on every node.

        Uses a per-node mapping so each container.remove() goes through that
        node's ssh_semaphore — without this, concurrent calls across many
        containers on a single node would blow past sshd's MaxSessions.
        """
        removed = 0

        for node in self.nodes:
            try:
                containers = self._node_call(
                    node,
                    lambda c: c.containers.list(
                        all=True,
                        filters={"label": [f"{DockerLabels.CTFD}=true"]},
                    ),
                )
            except Exception as e:
                log.error(f"[DockerManager] delete_all: list failed on {node}: {e}")
                continue

            for container in containers:
                cid = container.id
                token = container.labels.get(DockerLabels.TOKEN)
                try:
                    self._node_call(
                        node,
                        lambda c, _cid=cid: c.containers.get(_cid).remove(force=True),
                    )
                except NotFound:
                    # Already gone; drop any lingering timers and move on.
                    if token:
                        self.timer_timeout.cancel(token)
                        self.timer_kill.cancel(token)
                    continue
                except Exception as e:
                    # Remove failed: leave the existing timers in place, and if
                    # the container has none (e.g. lost across a restart),
                    # re-arm a short kill so we retry shortly. Cancelling here
                    # would orphan the container forever.
                    log.error(f"Failed removing container {cid[:12]}: {e}")
                    if token:
                        self.timer_kill.startOrRenew(
                            token, 60, lambda t=token: self.remove_container(t)
                        )
                    continue

                # Removed successfully; now it's safe to cancel timers and
                # release the host port.
                if token:
                    self.timer_timeout.cancel(token)
                    self.timer_kill.cancel(token)
                    self.ports_manager.release_port(token)
                removed += 1

        self._container_cache.clear()
        self._purge_challenge_networks()
        return removed

    # ------------------------------------------------------------------ #
    # Node scheduling                                                      #
    # ------------------------------------------------------------------ #

    def _next_node(self) -> Node:
        required_mem = RuntimeConfig.MAX_SPARE_RAM
        num_nodes = len(self.nodes)
        for _ in range(num_nodes):
            node = self.nodes[self._node_index]
            self._node_index = (self._node_index + 1) % num_nodes
            if self.node_free_mem(node) >= required_mem:
                return node
        raise Exception("No node has enough available memory")

    def _node_for_team_challenge(self, team_id, challenge_id) -> Node:
        """
        Return the node already hosting containers for this team+challenge so
        that all containers in a multi-container challenge land on the same
        node (required for the shared Docker network to work).
        Falls back to _next_node() when no containers exist yet.
        """
        self._ensure_cache_fresh()
        cached = self._container_cache.get_by_team_challenge(str(team_id), str(challenge_id))
        if cached:
            address = cached[0].node_address
            for node in self.nodes:
                if node.address == address:
                    return node
        return self._next_node()

    # ------------------------------------------------------------------ #
    # Node stats / memory                                                  #
    # ------------------------------------------------------------------ #

    def node_free_mem(self, node: Node):
        if node.address == "localhost":
            with open("/proc/meminfo") as f:
                for line in f:
                    if "MemAvailable" in line:
                        return int(line.split()[1]) * 1024

        ssh = self.ssh_pool.get(node)
        _, stdout, _ = ssh.exec_command("grep MemAvailable /proc/meminfo")
        return int(stdout.read().decode().split()[1]) * 1024

    def update_nodes_details(self):
        self._ensure_cache_fresh()
        for node in self.nodes:
            node.containers = []
            log.info(f"Updating stats for {node}...")
            try:
                info = self._node_call(node, lambda c: c.info())
            except Exception as e:
                log.error(f"[DockerManager] update_nodes_details failed for {node}: {e}")
                continue

            cached = self._container_cache.get_by_node(node.address)
            for c in cached:
                node.containers.append(ContainerDetails(
                    challenge=c.challenge_id,
                    team=c.team_id,
                    token=c.token,
                    container_index=c.container_index,
                    url=f"http://{c.token}.{RuntimeConfig.CTFD_DOMAIN_NAME}:8008/",
                    image=c.image_name,
                    status=c.status,
                ))

            node.stats.running_count = sum(1 for c in cached if c.status == "running")
            node.stats.exited_count  = sum(1 for c in cached if c.status != "running")
            node.stats.mem_total = int(info.get("MemTotal", 0))
            node.stats.free_mem  = self.node_free_mem(node)
            node.stats.used_mem  = node.stats.mem_total - node.stats.free_mem

    def print_nodes_table(self):
        header = f"\n{'Node':20} | {'Total RAM':10} | {'Used RAM':9} | {'Free RAM':9} | {'Running':7} | {'Exited':6}"
        sep = "-" * len(header)
        print(f"\n{sep}\n{header}\n{sep}")
        totals = [0, 0, 0, 0, 0]
        for node in self.nodes:
            try:
                info = self._node_call(node, lambda c: c.info())
                containers = self._node_call(
                    node,
                    lambda c: c.containers.list(
                        all=True,
                        filters={"label": [f"{DockerLabels.CTFD}=true"]},
                    ),
                )
            except Exception as e:
                log.warning(f"[DockerManager] print_nodes_table: skipping {node}: {e}")
                print(f"{node.address:20} | UNREACHABLE")
                continue
            mem_total = int(info.get("MemTotal", 0))
            running = sum(1 for c in containers if c.status == "running")
            exited  = sum(1 for c in containers if c.status != "running")
            free_mem = self.node_free_mem(node)
            used_mem = mem_total - free_mem
            totals = [t + v for t, v in zip(totals, [mem_total, used_mem, free_mem, running, exited])]
            print(
                f"{node.address:20} | {mem_total//(1024**2):7} MB | "
                f"{used_mem//(1024**2):6} MB | {free_mem//(1024**2):6} MB | "
                f"{running:7} | {exited:5}"
            )
        print(sep)
        print(
            f"{'TOTAL':20} | {totals[0]//(1024**2):7} MB | "
            f"{totals[1]//(1024**2):6} MB | {totals[2]//(1024**2):6} MB | "
            f"{totals[3]:7} | {totals[4]:5}"
        )
        print(sep)

    # ------------------------------------------------------------------ #
    # Image sync                                                           #
    # ------------------------------------------------------------------ #

    def _acquire_sync(self, key: str) -> tuple[bool, threading.Event]:
        with self._sync_events_lock:
            if key in self._sync_events:
                return False, self._sync_events[key]
            event = threading.Event()
            self._sync_events[key] = event
            return True, event

    def _release_sync(self, key: str):
        with self._sync_events_lock:
            event = self._sync_events.pop(key, None)
        if event:
            event.set()

    def sync_image(self, image: str):
        proceed, event = self._acquire_sync(image)
        if not proceed:
            log.info(f"[DockerManager] sync_image({image}): already in progress, waiting…")
            event.wait()
            return
        try:
            for node in self.nodes:
                log.info(f"Syncing {image} → {node.address}")
                subprocess.run(
                    f"docker save {image} | ssh {node.address} docker load",
                    shell=True, check=True, stdout=subprocess.DEVNULL,
                )
            log.info(f"{image} synced to all nodes.")
        finally:
            self._release_sync(image)

    def sync_registry_image(self, image: str):
        """
        Ensure every node has pulled *image* from the private registry.
        Strips any http:// / https:// prefix before use.
        Runs nodes in parallel. Raises RuntimeError if all nodes fail.
        """
        registry = self.registry

        for scheme in ("https://", "http://"):
            if image.startswith(scheme):
                image = image[len(scheme):]
                break

        def pull_on_node(node: Node) -> bool:
            ssh_prefix = f"ssh {node.name}@{node.address}"

            registry_host = registry.registry
            for scheme in ("https://", "http://"):
                if registry_host.startswith(scheme):
                    registry_host = registry_host[len(scheme):]
                    break
            registry_host = registry_host.rstrip("/")

            # Install CA cert
            cert_path = getattr(RuntimeConfig, "REGISTRY_CERT_PATH", None)
            certs_dir = f"/etc/docker/certs.d/{registry_host}"
            dest_cert = f"{certs_dir}/ca.crt"

            if cert_path and os.path.isfile(cert_path):
                tmp_cert = f"/tmp/ctfd_ca_{registry_host.replace(':', '_').replace('.', '_')}.crt"
                r = subprocess.run(
                    f"scp {cert_path} {node.name}@{node.address}:{tmp_cert}",
                    shell=True, capture_output=True,
                )
                if r.returncode != 0:
                    log.warning(f"[DockerManager] scp cert to {node.address} failed: {r.stderr.decode().strip()}")
                else:
                    cmd = f"mkdir -p {certs_dir} && cp {tmp_cert} {dest_cert} && chmod 644 {dest_cert}"
                    r = subprocess.run(f"{ssh_prefix} '{cmd}'", shell=True, capture_output=True)
                    if r.returncode != 0:
                        sudo_cmd = (
                            f"sudo /bin/mkdir -p {certs_dir} && "
                            f"sudo /bin/cp {tmp_cert} {dest_cert} && "
                            f"sudo /bin/chmod 644 {dest_cert}"
                        )
                        r2 = subprocess.run(f"{ssh_prefix} '{sudo_cmd}'", shell=True, capture_output=True)
                        if r2.returncode != 0:
                            log.error(f"[DockerManager] Cannot install cert on {node.address}.")
                            return False

            # Login
            if registry._is_configured() and registry.user and registry.password:
                login_cmd = (
                    f"{ssh_prefix} docker login {registry_host}"
                    f" --username {registry.user} --password-stdin"
                )
                try:
                    subprocess.run(
                        login_cmd,
                        input=registry.password.encode(),
                        shell=True, capture_output=True,
                    )
                except Exception as e:
                    log.warning(f"[DockerManager] Registry login error on {node.address}: {e}")

            # Check if already present
            check = subprocess.run(
                f"{ssh_prefix} docker image inspect {image}",
                shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if check.returncode == 0:
                log.info(f"{node.address:20} → ALREADY PRESENT")
                return True

            # Pull
            log.info(f"Pulling {image} on {node.address}…")
            r = subprocess.run(f"{ssh_prefix} docker pull {image}", shell=True, capture_output=True)
            if r.returncode == 0:
                log.info(f"{node.address:20} → PULLED")
                return True
            log.warning(f"[DockerManager] Pull failed on {node.address}: {r.stderr.decode().strip()}")
            return False

        proceed, event = self._acquire_sync(image)
        if not proceed:
            log.info(f"[DockerManager] sync_registry_image({image}): already in progress, waiting…")
            event.wait()
            return

        try:
            log.info(f"Syncing registry image: {image}")
            with ThreadPoolExecutor(max_workers=len(self.nodes)) as executor:
                results = [f.result() for f in as_completed(executor.submit(pull_on_node, n) for n in self.nodes)]

            if not any(results):
                raise RuntimeError(f"Failed to pull {image} on any node")
        finally:
            self._release_sync(image)

    def sync_tar_image(self, tar_path: str):
        if not os.path.isfile(tar_path):
            raise FileNotFoundError(tar_path)

        image = self._get_image_from_tar(tar_path)
        proceed, event = self._acquire_sync(image)
        if not proceed:
            log.info(f"[DockerManager] sync_tar_image({image}): already in progress, waiting…")
            event.wait()
            return

        tar_name = os.path.basename(tar_path)

        def sync_host(node: Node) -> bool:
            try:
                result = subprocess.run(
                    f"ssh {node.name}@{node.address} docker image inspect {image}",
                    shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if result.returncode == 0:
                    log.info(f"{node.address:20} → ALREADY PRESENT")
                    return True
                log.info(f"Syncing {tar_name} → {node.address}")
                subprocess.run(
                    f"cat {tar_path} | ssh {node.name}@{node.address} docker load",
                    shell=True, check=True, stdout=subprocess.DEVNULL,
                )
                log.info(f"{node.address:20} → LOADED")
                return True
            except subprocess.CalledProcessError:
                log.warning(f"{node.address:20} → FAILED")
                return False

        try:
            log.info(f"\nChecking image: {image}\n")
            with ThreadPoolExecutor(max_workers=len(self.nodes)) as executor:
                results = [f.result() for f in as_completed(executor.submit(sync_host, n) for n in self.nodes)]
            status = "synced to all nodes." if all(results) else "sync completed with errors."
            log.info(f"{image} {status}")
        finally:
            self._release_sync(image)

    def _get_image_from_tar(self, tar_path: str) -> str:
        with tarfile.open(tar_path, "r") as tar:
            manifest = json.load(tar.extractfile("manifest.json"))
            return manifest[0]["RepoTags"][0]