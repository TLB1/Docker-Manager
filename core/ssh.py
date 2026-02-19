import os
from pathlib import Path

import paramiko
from paramiko.client import SSHClient



class SSHPool:
    def __init__(self, user_overrides):
        self.clients = {}
        self.user_overrides = user_overrides

        for host in user_overrides.keys():
            try:
                self.create_connection(host)
                print(f"[SSHPool] Connected to {host} successfully.")
            except Exception as e:
                print(f"[SSHPool] Failed to connect to {host}: {e}")

    def get(self, host):
        ssh = self.clients.get(host)

        if ssh is None:
            return self.create_connection(host)

        try:
            ssh.exec_command("true")
            return ssh
        except:
            ssh.close()
            return self.create_connection(host)


    
    def create_connection(self, host) -> SSHClient:
        ssh_dir = Path("/home/ctfd/.ssh")
        known_hosts = ssh_dir / "known_hosts"
        key_file = ssh_dir / "id_ed25519"
        os.environ["SSH_KNOWN_HOSTS"] = str(known_hosts)

        client = SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        username = self.user_overrides[host] if host in self.user_overrides else "user"

        client.connect(
            host,
            username=username,
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
        hostkeys.add(host, key.get_name(), key)
        hostkeys.save(str(known_hosts))

        transport.set_keepalive(30)
        self.clients[host] = client
        return client
