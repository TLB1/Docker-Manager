import docker
from .config import RuntimeConfig


class PortsManager:
    def __init__(
        self,
        port_range_start=RuntimeConfig.INTERNAL_PORT_RANGE_START,
        port_range_end=RuntimeConfig.INTERNAL_PORT_RANGE_END,
    ):
        self.port_range_start = port_range_start
        self.port_range_end = port_range_end

        self.allocated_ports: dict[str, tuple[str, int]] = {} # token -> (server_url, port)


    def allocate_port(self, token: str, server_url: str) -> int:
        used_ports = {
            port
            for (host, port) in self.allocated_ports.values()
            if host == server_url
        }

        for port in range(self.port_range_start, self.port_range_end):
            if port not in used_ports:
                self.allocated_ports[token] = (server_url, port)
                self.update_nginx_token_map()

                print(f"http://{token}.challenges.ctf:8008/")
                return port

        raise Exception(f"No free ports available on {server_url}")



    def release_port(self, token: str):
        if token in self.allocated_ports:
            del self.allocated_ports[token]
            self.update_nginx_token_map()



    def get_port(self, token: str) -> int | None:
        entry = self.allocated_ports.get(token)
        return entry[1] if entry else None
    


    def update_nginx_token_map(self):
        config_lines = ["map $token $backend {", "    default \"\";"]

        for token, (server_url, port) in self.allocated_ports.items():
            config_lines.append(f"    {token} {server_url}:{port};")

        config_lines.append("}")

        with open("/opt/CTFd/CTFd/plugins/my-plugin/nginx/data/token_map.conf", "w") as f:
            f.write("\n".join(config_lines))

        client = docker.from_env()
        container = client.containers.get("ctfd-nginx-proxy")
        container.exec_run("nginx -s reload")

    