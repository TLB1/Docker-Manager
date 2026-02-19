from docker import DockerClient
from .container import ContainerDetails


class Node:
    def __init__(self, name: str, address: str, port: int = 22, client: DockerClient = None, containers: list[ContainerDetails] = []):
        self.name: str = name
        self.address: str = address
        self.port: int = port
        self.status: str = "Unknown"
        self.client: DockerClient = client
        self.stats: NodeStats = NodeStats()
        self.containers: list[ContainerDetails] = containers

    def __repr__(self):
        return f"Node({self.name}@{self.address}:{self.port})"
    

class NodeStats:
    def __init__(self, running_count: int = 0, exited_count: int = 0, mem_total: int = 0, free_mem: int = 0, used_mem: int = 0):
        self.running_count = running_count
        self.exited_count = exited_count
        self.mem_total = mem_total
        self.free_mem = free_mem
        self.used_mem = used_mem