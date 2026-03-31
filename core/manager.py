import json
import logging
import os
import subprocess
import tarfile
import secrets
import threading
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import docker
from docker import DockerClient
from docker.models.containers import Container
from paramiko.ssh_exception import ChannelException, SSHException

from .labels import DockerLabels
from .ssh import SSHPool
from .config import RuntimeConfig
from .ports import PortsManager
from .registry import RegistryManager
from .timer import RunnableTimer
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
                    node.client = DockerClient(base_url=f"ssh://{node.name}@{node.address}")

        self.ports_manager = PortsManager()
        self.registry = RegistryManager()
        self.timer_timeout = RunnableTimer()
        self.timer_kill = RunnableTimer()
        self._node_index = 0
        self._sync_events: dict[str, threading.Event] = {}
        self._sync_events_lock = threading.Lock()

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
        """Tear down and rebuild the Docker client for a node."""
        try:
            node.client.close()
        except Exception:
            pass
        try:
            node.client = DockerClient(base_url=f"ssh://{node.name}@{node.address}")
        except Exception as e:
            raise RuntimeError(f"Failed to reconnect Docker client for {node}: {e}")

    def _node_call(self, node: Node, fn, *args, retries: int = 1, **kwargs):
        """
        Call fn(*args, **kwargs) against node.client.
        On ChannelException / SSHException, reconnect once and retry.
        """
        for attempt in range(retries + 1):
            try:
                return fn(*args, **kwargs)
            except (ChannelException, SSHException) as e:
                if attempt < retries:
                    log.warning(f"[DockerManager] SSH channel error on {node}, reconnecting: {e}")
                    self._reconnect_node(node)
                else:
                    raise

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
                containers = self._node_call(node, node.client.containers.list, **kwargs)
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
                ids = {
                    c.id for c in node.client.containers.list(
                        all=True,
                        filters={"id": container.id},
                    )
                }
                if container.id in ids:
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
        running = self.running_containers_for_team(team_id)
        challenge_ids = {
            c.labels.get(DockerLabels.CHALLENGE)
            for c in running
            if c.labels.get(DockerLabels.CHALLENGE)
        }
        return len(challenge_ids) < RuntimeConfig.MAX_ACTIVE_CONTAINERS_PER_GROUP

    # ------------------------------------------------------------------ #
    # Network management                                                   #
    # ------------------------------------------------------------------ #

    def _challenge_network_name(self, challenge_id, team_id) -> str:
        hex_hash = hex(hash(str(team_id) + str(challenge_id)))
        return f"ctfd-challenge-network-{hex_hash}"

    def _get_or_create_network(self, node: Node, challenge_id, team_id) -> str:
        """
        Ensure the per-challenge Docker bridge network exists on *node*
        before any container is started on it.  Returns the network name.
        """
        network_name = self._challenge_network_name(challenge_id, team_id)
        try:
            if not node.client.networks.list(names=[network_name]):
                node.client.networks.create(network_name, driver="bridge")
                log.debug(f"[DockerManager] Created network {network_name} on {node}")
        except Exception as e:
            log.warning(f"[DockerManager] Could not ensure network {network_name} on {node}: {e}")
        return network_name

    def _cleanup_challenge_network(self, node: Node, challenge_id):
        """Remove the per-challenge network if no containers are still attached."""
        network_name = self._challenge_network_name(challenge_id)
        try:
            networks = node.client.networks.list(names=[network_name])
            if not networks:
                return
            network = networks[0]
            network.reload()
            if not network.containers:
                network.remove()
                log.debug(f"[DockerManager] Removed empty network {network_name} on {node}")
            else:
                log.debug(
                    f"[DockerManager] Network {network_name} still has "
                    f"{len(network.containers)} container(s), skipping removal"
                )
        except Exception as e:
            log.debug(f"[DockerManager] Network cleanup {network_name} on {node}: {e}")

    def _connect_with_alias(self, node: Node, network_name: str, container: Container, alias: str):
        """
        Connect *container* to *network_name* with a DNS alias so other
        containers in the network can reach it by hostname.
        """
        try:
            network = node.client.networks.get(network_name)
            network.connect(container, aliases=[alias])
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
        bridge — useful for single-container challenges that don't need
        inter-container communication.

        When expose_port=False no host port is allocated or bound —
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

        run_kwargs: Dict = dict(
            image=image,
            detach=True,
            mem_limit=str(RuntimeConfig.MEM_LIMIT_PER_CONTAINER),
            cpu_quota=RuntimeConfig.DOCKER_CONTAINER_CPU_QUOTA,
            ports=docker_ports if docker_ports else None,
            labels=labels,
        )

        if network_name:
            labels[DockerLabels.NETWORK_ALIAS] = network_alias
            run_kwargs["network"] = network_name
            run_kwargs["networking_config"] = node.client.api.create_networking_config({
                network_name: node.client.api.create_endpoint_config(aliases=[network_alias])
            })

        try:
            container = node.client.containers.run(**run_kwargs)
        except (ChannelException, SSHException) as e:
            # SSH dropped mid-flight — the container may or may not exist.
            log.warning(
                f"[DockerManager] SSH dropped during containers.run(), "
                f"checking if container was created: {e}"
            )
            self._reconnect_node(node)
            existing = self.get_container_by_token(token)
            if existing:
                self.set_timers(token)
                return token
            if expose_port:
                self.ports_manager.release_port(token)
            raise
        except Exception as e:
            if expose_port:
                self.ports_manager.release_port(token)
            raise

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
        suspend/resume/remove — it just has no host port allocated.

        Example::

            tokens = manager.create_challenge_containers(team_id, challenge_id, [
                ContainerSpec(
                    image="ctf-challenge-internal:latest",
                    network_alias="internal",
                    expose_port=False,          # never reachable from outside
                ),
                ContainerSpec(
                    image="ctf-challenge-gateway:latest",
                    network_alias="gateway",
                    expose_port=True,           # players connect here
                    container_port=80,
                ),
            ])
            player_url = f"http://{tokens[1]}.{domain}/"
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
        try:
            container.stop()
            return True
        except Exception:
            return False

    def resume_container(self, token: str) -> bool:
        container = self.get_container_by_token(token)
        if not container:
            return False
        container.start()
        self.set_timers(token)
        return True

    def remove_container(self, token: str) -> bool:
        container = self.get_container_by_token(token)
        if not container:
            return False

        self.timer_timeout.cancel(token)
        self.timer_kill.cancel(token)

        challenge_id = container.labels.get(DockerLabels.CHALLENGE)
        owning_node = self._find_node_for_container(container)

        try:
            container.remove(force=True)
            self.ports_manager.release_port(token)
            if challenge_id and owning_node:
                self._cleanup_challenge_network(owning_node, challenge_id)
            return True
        except Exception as e:
            log.warning(f"[DockerManager] Failed to remove container {token}: {e}")
            return False

    def delete_all(self) -> int:
        containers = self._query_containers(
            all=True,
            filters={"label": [f"{DockerLabels.CTFD}=true"]},
        )
        removed = 0
        for container in containers:
            try:
                token = container.labels.get(DockerLabels.TOKEN)
                if token:
                    self.timer_timeout.cancel(token)
                    self.timer_kill.cancel(token)
                container.remove(force=True)
                if token:
                    self.ports_manager.release_port(token)
                removed += 1
            except Exception as e:
                log.error(f"Failed removing container {container.id[:12]}: {e}")
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
        for node in self.nodes:
            try:
                existing = node.client.containers.list(
                    all=True,
                    filters={"label": [
                        f"{DockerLabels.TEAM}={team_id}",
                        f"{DockerLabels.CHALLENGE}={challenge_id}",
                    ]},
                )
                if existing:
                    return node
            except Exception:
                pass
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
        for node in self.nodes:
            node.containers = []
            log.info(f"Updating stats for {node}...")
            try:
                info = self._node_call(node, node.client.info)
                containers = self._node_call(
                    node,
                    node.client.containers.list,
                    all=True,
                    filters={"label": [f"{DockerLabels.CTFD}=true"]},
                )
            except Exception as e:
                log.error(f"[DockerManager] update_nodes_details failed for {node}: {e}")
                continue

            for container in containers:
                node.containers.append(ContainerDetails(
                    challenge=container.labels.get(DockerLabels.CHALLENGE),
                    team=container.labels.get(DockerLabels.TEAM),
                    token=container.labels.get(DockerLabels.TOKEN),
                    container_index=container.labels.get(DockerLabels.CONTAINER_INDEX),
                    url=f"http://{container.labels.get(DockerLabels.TOKEN)}.{RuntimeConfig.CTFD_DOMAIN_NAME}:8008/",
                    image=container.image,
                    status=container.status,
                ))

            node.stats.running_count = sum(1 for c in containers if c.status == "running")
            node.stats.exited_count  = sum(1 for c in containers if c.status != "running")
            node.stats.mem_total = int(info.get("MemTotal", 0))
            node.stats.free_mem  = self.node_free_mem(node)
            node.stats.used_mem  = node.stats.mem_total - node.stats.free_mem

    def print_nodes_table(self):
        header = f"\n{'Node':20} | {'Total RAM':10} | {'Used RAM':9} | {'Free RAM':9} | {'Running':7} | {'Exited':6}"
        sep = "-" * len(header)
        print(f"\n{sep}\n{header}\n{sep}")
        totals = [0, 0, 0, 0, 0]
        for node in self.nodes:
            info = self._node_call(node, node.client.info)
            mem_total = int(info.get("MemTotal", 0))
            containers = self._node_call(
                node, node.client.containers.list, all=True,
                filters={"label": [f"{DockerLabels.CTFD}=true"]},
            )
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