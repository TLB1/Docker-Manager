import json
import subprocess
import tarfile
#import uuid
import secrets
import threading
import docker
from docker import DockerClient
from docker.models.containers import Container

from tests.constants import time

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
import gevent.threading
from concurrent.futures import ThreadPoolExecutor, as_completed


class DockerManager:



    def __init__(self, base_urls: Optional[Iterable[str]] = None):
        """
        Creates DockerManager instances for local or multiple remote Docker hosts.
        
        :param base_urls: Iterable of remote SSH Docker hosts (e.g. ["user@host1", "user@host2"])
        """
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

        self._node_index = 0 #Round Robin Scheduling




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



    def running_containers_for_team(self, team_id) -> List[Container]:
        return self._query_containers(
            all=True,
            filters={"label": [f"{DockerLabels.TEAM}={team_id}"]},
        )


    
    def get_container_for_team_challenge(self, team_id: int, challenge_id: int) -> Optional[Container]:
        containers = self._query_containers(
            all=True,
            filters={"label": [f"{DockerLabels.TEAM}={team_id}", f"{DockerLabels.CHALLENGE}={challenge_id}"]},
        )
        return containers[0] if containers else None    
    


    def running_containers(self, client: DockerClient) -> List[Container]:
        return client.containers.list(
            filters={"label": [f"{DockerLabels.CTFD}=true"]},
        )



    def can_create_container(self, team_id) -> bool:
        running = self.running_containers_for_team(team_id)
        return len(running) < RuntimeConfig.MAX_ACTIVE_CONTAINERS_PER_GROUP



    def create_container(self, team_id, challenge_id, image, container_port = 80) -> str:
        if not self.can_create_container(team_id):
            raise Exception("Team container quota exceeded")

        print("\n----------------")
        token = f"{secrets.randbits(48):08x}"
        node = self._next_node()
        host_port = self.ports_manager.allocate_port(token, node.address)
        
        #image = self.registry.ensure_image_exists(image)
        #node.client.images.pull(image)

        print(f"{image} - {node.address}:{host_port}")
        
        node.client.containers.run(
            image = image,
            detach = True,
            #network = RuntimeConfig.DOCKER_CONTAINER_NETWORK,
            mem_limit = str(RuntimeConfig.MEM_LIMIT_PER_CONTAINER),
            cpu_quota = RuntimeConfig.DOCKER_CONTAINER_CPU_QUOTA,
            labels = {
                DockerLabels.CTFD: "true",
                DockerLabels.TEAM: str(team_id),
                DockerLabels.CHALLENGE: str(challenge_id),
                DockerLabels.TOKEN: token
            },
            ports={f"{container_port}/tcp": host_port}
        )

        self.set_timers(token)
        return token



    def _query_containers(self, **kwargs) -> List[Container]:
        results: List[Container] = []
        for node in self.nodes:
            results.extend(node.client.containers.list(**kwargs))
        return results
    


    def _next_node(self) -> Node:
#        """
#        Choose the Docker client with the most free RAM using.
#        """
        """
        Round-robin across nodes, but skip nodes without enough free RAM.
        """
        required_mem = RuntimeConfig.MAX_SPARE_RAM
        num_nodes = len(self.nodes)

        for _ in range(num_nodes):
            node = self.nodes[self._node_index]
            self._node_index = (self._node_index + 1) % num_nodes

            free_mem = self.node_free_mem(node)

            if free_mem >= required_mem:
                return node

        raise Exception("No node has enough available memory")



    def get_container_by_token(self, token: str) -> Optional[Container]:
        containers = self._query_containers(
            all=True,
            filters={"label": [f"{DockerLabels.TOKEN}={token}"]},
        )
        return containers[0] if containers else None




    def suspend_container(self, token: str) -> bool:
        """
        Suspends/stops a container by token, returns true if the container has been suspended
        """
        container = self.get_container_by_token(token)
        if not container:
            return False

        try:
            container.stop()
            return True
        except Exception:
            return False



    def resume_container(self, token: str) -> bool:
        """
        Resumes a container by token, returns true if the container has been resumed or the timer has been extended
        """
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
        """
        Removes ALL containers managed by this system (DockerLabels.CTFD=true)
        across all nodes and releases their allocated ports.

        :return: number of containers removed
        """
        containers = self._query_containers(
            all=True,
            filters={"label": [f"{DockerLabels.CTFD}=true"]},
        )

        removed_count = 0

        for container in containers:
            try:
                token = container.labels.get(DockerLabels.TOKEN)

                # cancel timers if token exists
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



    def node_free_mem(self, node: Node):

        if node.address == "localhost":
            with open("/proc/meminfo") as f:
                for line in f:
                    if "MemAvailable" in line:
                        kb = int(line.split()[1])
                        return kb * 1024

        ssh = self.ssh_pool.get(node)

        stdin, stdout, stderr = ssh.exec_command(
            "grep MemAvailable /proc/meminfo"
        )

        result = stdout.read().decode()
        kb = int(result.split()[1])
        return kb * 1024



    def sync_image(self, image: str):
        """
        Saves a local Docker image and loads it on all remote nodes via SSH.
        """
        for node in self.nodes:
            print(f"Syncing {image} → {node.address}")

            subprocess.run(
                f"docker save {image} | ssh {node.address} docker load",
                shell=True,
                check=True,
                stdout=subprocess.DEVNULL,
            )
        print(f"{image} synced to all nodes.")



    
    def sync_tar_image(self, tar_path: str):
        if not os.path.isfile(tar_path):
            raise FileNotFoundError(tar_path)

        image = self._get_image_from_tar(tar_path)
        tar_name = os.path.basename(tar_path)

        def sync_host(node):
            try:
                check_cmd = f"ssh {node.name}@{node.address} docker image inspect {image}"
                result = subprocess.run(check_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                if result.returncode == 0:
                    print(
                        f"{node.address:20} → "
                        f"ALREADY PRESENT"
                    )
                    return True

                print(
                    f"Syncing {tar_name} → {node.address}"
                )

                subprocess.run(
                    f"cat {tar_path} | ssh {node.name}@{node.address} docker load",
                    shell=True,
                    check=True,
                    stdout=subprocess.DEVNULL,
                )

                print(f"{node.address:20} → LOADED")
                return True

            except subprocess.CalledProcessError:
                print(f"{node.address:20} → FAILED")
                return False

        print(f"\nChecking image: {image}\n")

        with ThreadPoolExecutor(max_workers=len(self.nodes)) as executor:
            futures = [executor.submit(sync_host, node) for node in self.nodes]
            results = [future.result() for future in as_completed(futures)]

        if all(results):
            print(
                f"\n{image} "
                f"synced to all nodes."
            )
        else:
            print(
                f"\n{image} "
                f"Sync completed with errors."
            )




    def _get_image_from_tar(self, tar_path: str) -> str:
        with tarfile.open(tar_path, "r") as tar:
            manifest = json.load(tar.extractfile("manifest.json"))
            return manifest[0]["RepoTags"][0]


    
    def update_nodes_details(self):
        for node in self.nodes:
            node.containers = []
            print(f"Updating stats for {node}...")
            info = node.client.info()
            containers = node.client.containers.list(
                all=True,
                filters={"label": [f"{DockerLabels.CTFD}=true"]},
            )
            for container in containers:
                node.containers.append(ContainerDetails(
                    challenge=container.labels.get(DockerLabels.CHALLENGE),
                    team=container.labels.get(DockerLabels.TEAM),
                    token=container.labels.get(DockerLabels.TOKEN),
                    url=f"http://{container.labels.get(DockerLabels.TOKEN)}.challenges.ctf:8008/",
                    image=container.image,
                    status=container.status
                ))

            node.stats.running_count = sum(1 for c in containers if c.status == "running")
            node.stats.exited_count = sum(1 for c in containers if c.status != "running")
            node.stats.mem_total = int(info.get("MemTotal", 0))
            node.stats.free_mem = self.node_free_mem(node)
            node.stats.used_mem = node.stats.mem_total - node.stats.free_mem

    def print_nodes_table(self):
        """
        Prints a one-line table overview of all Docker nodes: RAM and container counts,
        including a TOTAL summary row.
        """

        header = f"\n{'Node':20} | {'Total RAM':10} | {'Used RAM':9} | {'Free RAM':9} | {'Running':7} | {'Exited':6}"
        print("\n")
        print("-" * len(header))
        print(header)
        print("-" * len(header))

        # Totals
        total_mem_total = 0
        total_used_mem = 0
        total_free_mem = 0
        total_running = 0
        total_exited = 0

        for node in self.nodes:
            info = node.client.info()
            mem_total = int(info.get("MemTotal", 0))
            containers = node.client.containers.list(
                all=True,
                filters={"label": [f"{DockerLabels.CTFD}=true"]},
            )
            running_count = sum(1 for c in containers if c.status == "running")
            exited_count = sum(1 for c in containers if c.status != "running")
            free_mem = self.node_free_mem(node)
            used_mem = mem_total - free_mem

            # Accumulate totals
            total_mem_total += mem_total
            total_used_mem += used_mem
            total_free_mem += free_mem
            total_running += running_count
            total_exited += exited_count

            print(f"{node.address:20} | "
                f"{mem_total // (1024**2):7} MB | "
                f"{used_mem // (1024**2):6} MB | "
                f"{free_mem // (1024**2):6} MB | "
                f"{running_count:7} | "
                f"{exited_count:5}")

        # Print total row
        print("-" * len(header))
        print(f"{'TOTAL':20} | "
            f"{total_mem_total // (1024**2):7} MB | "
            f"{total_used_mem // (1024**2):6} MB | "
            f"{total_free_mem // (1024**2):6} MB | "
            f"{total_running:7} | "
            f"{total_exited:5}")
        print("-" * len(header))



if __name__ == "__main__":
    try:
        manager = DockerManager(["user@10.20.100.14", "user@10.20.100.15", "user@10.20.100.35"])
        print(f"Cleaned up {manager.delete_all()} containers")

        #manager.sync_image("mysql:oraclelinux9")
        manager.sync_tar_image("images/postgres.tar")
        manager.sync_tar_image("images/gradle.tar")

        # Create containers in a loop
        containers = []
        for i in range(1, 30):
            name = f"test{i}"
            ct = manager.create_container(name, name, "ctfd/ctfd:latest", 8000)
            containers.append(ct)

        for i in range(1, 10):
            name = f"test{i}"
            ct = manager.create_container(name, name, "postgres:14.21-trixie", 5432)
            containers.append(ct)

        ct1 = manager.create_container("test1_nginx", "test1_nginx", "nginx")
        ct2 = manager.create_container("test2_httpd", "test2_httpd", "httpd:trixie")
        containers.extend([ct1, ct2])

        manager.print_nodes_table()
        input("\nPress ENTER to stop and remove containers...")

        for ct in containers:
            manager.remove_container(ct)

    except Exception as e:
        raise Exception(f"Could not connect to server: {e}")
