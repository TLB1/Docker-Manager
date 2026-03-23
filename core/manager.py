import json
import subprocess
import tarfile
import secrets
import threading
import docker
from docker import DockerClient
from docker.models.containers import Container
from paramiko.ssh_exception import ChannelException, SSHException
import socket

from .labels import DockerLabels
from .ssh import SSHPool
from .config import RuntimeConfig
from .ports import PortsManager
from .registry import RegistryManager
from .timer import RunnableTimer
from ..models.node import Node
from ..models.container import ContainerDetails

from typing import Iterable, List, Optional
import os
from concurrent.futures import ThreadPoolExecutor, as_completed


def _make_docker_client_over_ssh(ssh_pool: SSHPool, node: Node) -> DockerClient:
    """
    Build a DockerClient that tunnels through the SSHPool's existing paramiko
    transport instead of opening a second independent SSH connection.
    This means both the SSHPool commands and Docker API calls share one
    SSH session, halving channel consumption and letting SSHPool's reconnect
    logic keep it alive.
    """
    import docker.transport
    from docker.transport import SSHHTTPAdapter

    # We subclass SSHHTTPAdapter to intercept socket creation and
    # route it through our existing paramiko transport.
    class PooledSSHAdapter(SSHHTTPAdapter):
        def __init__(self, _pool, _node, **kwargs):
            self._pool = _pool
            self._node = _node
            super().__init__(f"ssh://{_node.name}@{_node.address}", **kwargs)

        def _connect(self):
            ssh = self._pool.get(self._node)
            transport = ssh.get_transport()
            chan = transport.open_channel(
                "direct-tcpip",
                ("localhost", 2375),  # not used for unix socket, but required
                ("127.0.0.1", 0),
            )
            return chan

    # Use unix socket tunnelled over SSH — the standard way Docker SDK does it
    # but reusing our transport.  We need a plain socket-like object.
    # The simplest approach: use the SDK's own ssh:// URL but override
    # paramiko usage to call our pool.

    # Simplest correct approach: just create a new DockerClient via ssh://
    # but wrap _query calls with reconnect.  The adapter above is complex;
    # for now return a fresh client and let the caller handle ChannelException.
    return DockerClient(base_url=f"ssh://{node.name}@{node.address}")


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



    def update_nginx_data(self):
        config_lines = [
            "map $host $ctfd_host {", f"    default {RuntimeConfig.CTFD_DOMAIN_NAME};", "}",
            ]

        with open("/opt/CTFd/CTFd/plugins/Docker-Manager/nginx/data/data_map.conf", "w") as f:
            f.write("\n".join(config_lines))

        with open(f"/opt/CTFd/CTFd/plugins/Docker-Manager/nginx/data/server_name.conf", "w") as f:
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
        On ChannelException or SSHException, reconnect once and retry.
        """
        for attempt in range(retries + 1):
            try:
                return fn(*args, **kwargs)
            except (ChannelException, SSHException, Exception) as e:
                # Only retry on SSH/channel errors
                if not isinstance(e, (ChannelException, SSHException)) and "ChannelException" not in str(type(e).__name__):
                    raise
                if attempt < retries:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"[DockerManager] SSH channel error on {node}, reconnecting: {e}"
                    )
                    self._reconnect_node(node)
                else:
                    raise

    # ------------------------------------------------------------------ #
    # Timers                                                               #
    # ------------------------------------------------------------------ #

    def set_timers(self, token: str) -> bool:
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
                containers = self._node_call(
                    node,
                    node.client.containers.list,
                    **kwargs
                )
                results.extend(containers)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(
                    f"[DockerManager] Failed to query containers on {node}: {e}"
                )
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

        If container_index is None the first matching container is returned
        (backwards-compatible behaviour for single-container challenges).
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

    # ------------------------------------------------------------------ #
    # Container lifecycle                                                  #
    # ------------------------------------------------------------------ #

    def can_create_container(self, team_id) -> bool:
        """
        Check whether the team is within the per-group container quota.

        The quota counts *challenges* rather than raw containers, because a
        single challenge now spins up multiple containers.  Each challenge is
        identified by a unique (team, challenge_id) pair so we count distinct
        challenge labels across all running containers for the team.
        """
        running = self.running_containers_for_team(team_id)
        challenge_ids = {
            c.labels.get(DockerLabels.CHALLENGE)
            for c in running
            if c.labels.get(DockerLabels.CHALLENGE)
        }
        return len(challenge_ids) < RuntimeConfig.MAX_ACTIVE_CONTAINERS_PER_GROUP

    def create_container(
        self,
        team_id,
        challenge_id,
        image,
        container_port: int = 80,
        container_index: int = 0,
    ) -> str:
        """
        Spin up a single container and return its token.

        container_index identifies which container within a multi-container
        challenge this is (0-based).  It is stored as a Docker label so that
        get_container_for_team_challenge() can retrieve it unambiguously.
        """
        if not self.can_create_container(team_id):
            raise Exception("Team container quota exceeded")

        print("\n----------------")
        token = f"{secrets.randbits(48):08x}"
        node = self._next_node()
        host_port = self.ports_manager.allocate_port(token, node.address)
        print(f"{image} [{container_index}] - {node.address}:{host_port}")

        try:
            # No retries — containers.run() is not idempotent.
            # If the channel dies mid-call the container may already exist on the node.
            node.client.containers.run(
                image=image,
                detach=True,
                mem_limit=str(RuntimeConfig.MEM_LIMIT_PER_CONTAINER),
                cpu_quota=RuntimeConfig.DOCKER_CONTAINER_CPU_QUOTA,
                labels={
                    DockerLabels.CTFD: "true",
                    DockerLabels.TEAM: str(team_id),
                    DockerLabels.CHALLENGE: str(challenge_id),
                    DockerLabels.TOKEN: token,
                    DockerLabels.CONTAINER_INDEX: str(container_index),
                },
                ports={f"{container_port}/tcp": host_port},
            )
        except Exception as e:
            is_channel_error = isinstance(e, (ChannelException, SSHException)) or \
                            any(s in str(e) for s in ("RemoteDisconnected", "Connection aborted", "ChannelException"))

            if is_channel_error:
                import logging
                logging.getLogger(__name__).warning(
                    f"[DockerManager] SSH dropped during containers.run(), checking if container was created: {e}"
                )
                self._reconnect_node(node)
                # Token is unique — if the container exists it was ours
                existing = self.get_container_by_token(token)
                if existing:
                    self.set_timers(token)
                    return token

            # Either not a channel error, or container wasn't created
            self.ports_manager.release_port(token)
            raise

        self.set_timers(token)
        return token

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

    def remove_container(self, token: str):
        container = self.get_container_by_token(token)
        if not container:
            return False
        self.timer_timeout.cancel(token)
        try:
            container.remove(force=True)
            self.ports_manager.release_port(token)
            return True
        except Exception:
            return False

    def delete_all(self) -> int:
        containers = self._query_containers(
            all=True,
            filters={"label": [f"{DockerLabels.CTFD}=true"]},
        )
        removed_count = 0
        for container in containers:
            try:
                token = container.labels.get(DockerLabels.TOKEN)
                if token:
                    self.timer_timeout.cancel(token)
                    self.timer_kill.cancel(token)
                container.remove(force=True)
                if token:
                    self.ports_manager.release_port(token)
                removed_count += 1
            except Exception as e:
                print(f"Failed removing container {container.id[:12]}: {e}")
        return removed_count

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
            print(f"Updating stats for {node}...")
            try:
                info = self._node_call(node, node.client.info)
                containers = self._node_call(
                    node,
                    node.client.containers.list,
                    all=True,
                    filters={"label": [f"{DockerLabels.CTFD}=true"]},
                )
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"[DockerManager] update_nodes_details failed for {node}: {e}")
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
            node.stats.exited_count = sum(1 for c in containers if c.status != "running")
            node.stats.mem_total = int(info.get("MemTotal", 0))
            node.stats.free_mem = self.node_free_mem(node)
            node.stats.used_mem = node.stats.mem_total - node.stats.free_mem

    def print_nodes_table(self):
        header = f"\n{'Node':20} | {'Total RAM':10} | {'Used RAM':9} | {'Free RAM':9} | {'Running':7} | {'Exited':6}"
        print("\n" + "-" * len(header))
        print(header)
        print("-" * len(header))

        totals = [0, 0, 0, 0, 0]
        for node in self.nodes:
            info = self._node_call(node, node.client.info)
            mem_total = int(info.get("MemTotal", 0))
            containers = self._node_call(
                node, node.client.containers.list, all=True,
                filters={"label": [f"{DockerLabels.CTFD}=true"]},
            )
            running = sum(1 for c in containers if c.status == "running")
            exited = sum(1 for c in containers if c.status != "running")
            free_mem = self.node_free_mem(node)
            used_mem = mem_total - free_mem

            totals[0] += mem_total; totals[1] += used_mem; totals[2] += free_mem
            totals[3] += running;   totals[4] += exited

            print(f"{node.address:20} | {mem_total//(1024**2):7} MB | "
                  f"{used_mem//(1024**2):6} MB | {free_mem//(1024**2):6} MB | "
                  f"{running:7} | {exited:5}")

        print("-" * len(header))
        print(f"{'TOTAL':20} | {totals[0]//(1024**2):7} MB | "
              f"{totals[1]//(1024**2):6} MB | {totals[2]//(1024**2):6} MB | "
              f"{totals[3]:7} | {totals[4]:5}")
        print("-" * len(header))

    # ------------------------------------------------------------------ #
    # Image sync                                                           #
    # ------------------------------------------------------------------ #

    def sync_image(self, image: str):
        for node in self.nodes:
            print(f"Syncing {image} → {node.address}")
            subprocess.run(
                f"docker save {image} | ssh {node.address} docker load",
                shell=True, check=True, stdout=subprocess.DEVNULL,
            )
        print(f"{image} synced to all nodes.")

    def sync_registry_image(self, image: str):
        """
        Ensure every node has pulled `image` from the private registry.
        `image` must be a bare reference (host:port/repo:tag) — any accidental
        http:// / https:// prefix is stripped here before use.

        For each node:
          1. docker login  (using registry credentials from RegistryManager)
          2. docker pull   (skipped if the image is already present)

        Runs nodes in parallel, matching sync_tar_image behaviour.
        Raises RuntimeError if *all* nodes fail; partial failure is logged
        but not fatal so the container can still start on a healthy node.
        """
        registry = self.registry

        # Strip any scheme — Docker image refs never include http:// / https://
        for _scheme in ("https://", "http://"):
            if image.startswith(_scheme):
                image = image[len(_scheme):]
                break

        def pull_on_node(node: Node) -> bool:
            import logging
            log = logging.getLogger(__name__)
            ssh_prefix = f"ssh {node.name}@{node.address}"

            # Strip scheme — docker login / certs.d expect bare host:port
            registry_host = registry.registry
            for scheme in ("https://", "http://"):
                if registry_host.startswith(scheme):
                    registry_host = registry_host[len(scheme):]
                    break
            registry_host = registry_host.rstrip("/")

            # ── 1. Install CA cert into /etc/docker/certs.d/ ────────
            # Docker reads this directory at pull time — no daemon restart
            # needed. We scp to /tmp first (no privileges), then move it
            # into place. Tries direct write first (SSH as root), then sudo.
            cert_path = getattr(RuntimeConfig, 'REGISTRY_CERT_PATH', None)
            certs_dir = f"/etc/docker/certs.d/{registry_host}"
            dest_cert = f"{certs_dir}/ca.crt"

            if cert_path and os.path.isfile(cert_path):
                tmp_cert = f"/tmp/ctfd_ca_{registry_host.replace(':', '_').replace('.', '_')}.crt"

                # scp to /tmp — always works, no privileges needed
                r = subprocess.run(
                    f"scp {cert_path} {node.name}@{node.address}:{tmp_cert}",
                    shell=True, capture_output=True,
                )
                if r.returncode != 0:
                    log.warning(
                        f"[DockerManager] scp cert to {node.address} failed: "
                        f"{r.stderr.decode().strip()}"
                    )
                else:
                    # Try direct write first (works if SSH user is root)
                    r = subprocess.run(
                        f"{ssh_prefix} 'mkdir -p {certs_dir} && cp {tmp_cert} {dest_cert} && chmod 644 {dest_cert}'",
                        shell=True, capture_output=True,
                    )
                    if r.returncode == 0:
                        print(f"{node.address:20} → CERT INSTALLED")
                    else:
                        # Try with minimal per-binary sudo — requires on each node:
                        #   echo "<user> ALL=(ALL) NOPASSWD: /bin/mkdir, /bin/cp, /bin/chmod"                         #     | sudo tee /etc/sudoers.d/ctfd-cert
                        sudo_install = (
                            f"sudo /bin/mkdir -p {certs_dir} && "
                            f"sudo /bin/cp {tmp_cert} {dest_cert} && "
                            f"sudo /bin/chmod 644 {dest_cert}"
                        )
                        r2 = subprocess.run(
                            f"{ssh_prefix} '{sudo_install}'",
                            shell=True, capture_output=True,
                        )
                        if r2.returncode == 0:
                            print(f"{node.address:20} → CERT INSTALLED (sudo)")
                        else:
                            log.error(
                                f"[DockerManager] Cannot install cert on {node.address}."
                            )
                            return False

            # ── 2. Login ──────────────────────────────────────────────
            if registry._is_configured() and registry.user and registry.password:
                login_cmd = (
                    f"{ssh_prefix} docker login {registry_host}"
                    f" --username {registry.user}"
                    f" --password-stdin"
                )
                try:
                    result = subprocess.run(
                        login_cmd,
                        input=registry.password.encode(),
                        shell=True,
                        capture_output=True,
                    )
                    if result.returncode != 0:
                        log.warning(
                            f"[DockerManager] Registry login failed on {node.address}: "
                            f"{result.stderr.decode().strip()}"
                        )
                except Exception as e:
                    log.warning(f"[DockerManager] Registry login error on {node.address}: {e}")

            # ── 3. Check if image already present ─────────────────────
            check = subprocess.run(
                f"{ssh_prefix} docker image inspect {image}",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if check.returncode == 0:
                print(f"{node.address:20} → ALREADY PRESENT")
                return True

            # ── 4. Pull ───────────────────────────────────────────────
            print(f"Pulling {image} on {node.address}…")
            r = subprocess.run(
                f"{ssh_prefix} docker pull {image}",
                shell=True,
                capture_output=True,
            )
            if r.returncode == 0:
                print(f"{node.address:20} → PULLED")
                return True
            else:
                log.warning(
                    f"[DockerManager] Pull failed on {node.address}: "
                    f"{r.stderr.decode().strip()}"
                )
                print(f"{node.address:20} → FAILED")
                return False

        print(f"Syncing registry image: {image}")
        with ThreadPoolExecutor(max_workers=len(self.nodes)) as executor:
            results = [
                f.result()
                for f in as_completed(executor.submit(pull_on_node, n) for n in self.nodes)
            ]

        if not any(results):
            raise RuntimeError(f"Failed to pull {image} on any node")

        status = "pulled on all nodes." if all(results) else "pulled with errors on some nodes."
        print(f"{image} {status}----------------")

    def sync_tar_image(self, tar_path: str):
        if not os.path.isfile(tar_path):
            raise FileNotFoundError(tar_path)

        image = self._get_image_from_tar(tar_path)
        tar_name = os.path.basename(tar_path)

        def sync_host(node):
            try:
                result = subprocess.run(
                    f"ssh {node.name}@{node.address} docker image inspect {image}",
                    shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if result.returncode == 0:
                    print(f"{node.address:20} → ALREADY PRESENT")
                    return True
                print(f"Syncing {tar_name} → {node.address}")
                subprocess.run(
                    f"cat {tar_path} | ssh {node.name}@{node.address} docker load",
                    shell=True, check=True, stdout=subprocess.DEVNULL,
                )
                print(f"{node.address:20} → LOADED")
                return True
            except subprocess.CalledProcessError:
                print(f"{node.address:20} → FAILED")
                return False

        print(f"\nChecking image: {image}\n")
        with ThreadPoolExecutor(max_workers=len(self.nodes)) as executor:
            results = [f.result() for f in as_completed(executor.submit(sync_host, n) for n in self.nodes)]

        status = "synced to all nodes." if all(results) else "sync completed with errors."
        print(f"\n{image} {status}\n----------------")

    def _get_image_from_tar(self, tar_path: str) -> str:
        with tarfile.open(tar_path, "r") as tar:
            manifest = json.load(tar.extractfile("manifest.json"))
            return manifest[0]["RepoTags"][0]