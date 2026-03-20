import os
from pathlib import Path
from typing import List
import paramiko
from paramiko.client import SSHClient
from paramiko.ssh_exception import ChannelException, SSHException
from ..models.node import Node


class SSHPool:
    def __init__(self, nodes: List[Node]):
        self.nodes = nodes
        self.clients = {}
        for node in nodes:
            try:
                self.create_connection(node)
                print(f"[SSHPool] Connected to {node} successfully.")
            except Exception as e:
                print(f"[SSHPool] Failed to connect to {node}: {e}")



    def get(self, node: Node) -> SSHClient:
        ssh = self.clients.get(node)
        if ssh is None:
            return self.create_connection(node)

        transport = ssh.get_transport()
        if transport is None or not transport.is_active():
            # Transport is dead — reconnect
            try:
                ssh.close()
            except Exception:
                pass
            return self.create_connection(node)

        # Lightweight liveness check: open and immediately close a channel
        try:
            chan = transport.open_session()
            chan.close()
        except (ChannelException, SSHException, EOFError, Exception):
            try:
                ssh.close()
            except Exception:
                pass
            return self.create_connection(node)

        return ssh

    def create_connection(self, node: Node) -> SSHClient:
        ssh_dir = Path("/home/ctfd/.ssh")
        known_hosts = ssh_dir / "known_hosts"
        key_file = ssh_dir / "id_ed25519"

        os.environ["SSH_KNOWN_HOSTS"] = str(known_hosts)

        client = SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=node.address,
            username=node.name,
            key_filename=str(key_file),
            allow_agent=True,
            look_for_keys=True,
            compress=True,
        )

        hostkeys = paramiko.HostKeys()
        if known_hosts.exists():
            hostkeys.load(str(known_hosts))
        transport = client.get_transport()
        key = transport.get_remote_server_key()
        hostkeys.add(node.address, key.get_name(), key)
        hostkeys.save(str(known_hosts))
        transport.set_keepalive(30)

        self.clients[node] = client
        return client