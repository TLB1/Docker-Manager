from docker import DockerClient


class Node:
    def __init__(self, name: str, address: str, port: int = 22, client: DockerClient = None):
        self.name: str = name
        self.address: str = address
        self.port: int = port
        self.status: str = "Unknown"
        self.client: DockerClient = client

    def __repr__(self):
        return f"Node({self.name}@{self.address}:{self.port})"