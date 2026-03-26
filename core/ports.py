from dataclasses import dataclass
from .config import RuntimeConfig


STREAM_MAP_PATH = "/opt/CTFd/CTFd/plugins/Docker-Manager/nginx/data/stream_map.conf"

@dataclass
class TcpPortMapping:
    """
    Represents one non-HTTP port exposed by a container.

    ctfd_tcp_port  — port open on the CTFd host (TCP range, e.g. 10000-11000).
                     The external proxy listens here.
    node_addr      — address of the worker node the container is running on.
    node_host_port — port Docker bound on the node (HTTP range, e.g. 50000-60000).
                     The external proxy forwards ctfd_tcp_port → node_addr:node_host_port.
    container_port — original port inside the container.
    """
    ctfd_tcp_port: int
    node_addr: str
    node_host_port: int
    container_port: int


class PortsManager:
    def __init__(
        self,
        port_range_start: int = RuntimeConfig.INTERNAL_PORT_RANGE_START,
        port_range_end: int   = RuntimeConfig.INTERNAL_PORT_RANGE_END,
        tcp_range_start: int  = RuntimeConfig.TCP_PORT_RANGE_START,
        tcp_range_end: int    = RuntimeConfig.TCP_PORT_RANGE_END,
    ):
        # ── Node-side host ports (HTTP range) ─────────────────────────
        # Used for ALL Docker port bindings: both HTTP and TCP container
        # ports need a node host port so Docker can forward traffic.
        #
        # Key:   token          → primary HTTP port   (existing behaviour)
        #        "token:N"      → extra port for container port N
        self.port_range_start = port_range_start
        self.port_range_end   = port_range_end
        self.allocated_ports: dict[str, tuple[str, int]] = {}
        # token → (node_addr, node_host_port)

        # ── CTFd-side TCP ports ───────────────────────────────────────
        # One entry per non-HTTP port mapping across all containers.
        # Key: token  →  list[TcpPortMapping]
        self.tcp_range_start = tcp_range_start
        self.tcp_range_end   = tcp_range_end
        self.tcp_mappings: dict[str, list[TcpPortMapping]] = {}

    # ------------------------------------------------------------------ #
    # Node host-port allocation (shared by HTTP + TCP container ports)    #
    # ------------------------------------------------------------------ #

    def _used_node_ports(self, server_url: str) -> set:
        return {
            port
            for (host, port) in self.allocated_ports.values()
            if host == server_url
        }

    def allocate_port(self, token: str, server_url: str) -> int:
        """
        Allocate the PRIMARY node host port for a container (HTTP proxy path).
        Stores under the plain token key — the existing contract for nginx/backend lookup.
        """
        used = self._used_node_ports(server_url)
        for port in range(self.port_range_start, self.port_range_end):
            if port not in used:
                self.allocated_ports[token] = (server_url, port)
                print(f"http://{token}.{RuntimeConfig.CTFD_DOMAIN_NAME}:8008/")
                return port
        raise Exception(f"No free HTTP ports available on {server_url}")

    def allocate_extra_node_port(
        self, token: str, container_port: int, server_url: str
    ) -> int:
        """
        Allocate an additional node host port for a non-primary container port
        (used for both extra HTTP ports and TCP ports).

        Stored under key  "token:container_port"  so it does not collide with
        the primary token key and can be cleaned up by prefix on release.
        """
        key  = f"{token}:{container_port}"
        used = self._used_node_ports(server_url)
        for port in range(self.port_range_start, self.port_range_end):
            if port not in used:
                self.allocated_ports[key] = (server_url, port)
                return port
        raise Exception(
            f"No free node ports available on {server_url} "
            f"for container port {container_port}"
        )

    # ------------------------------------------------------------------ #
    # CTFd TCP port allocation (non-HTTP container ports only)            #
    # ------------------------------------------------------------------ #

    def _used_ctfd_tcp_ports(self) -> set:
        return {
            m.ctfd_tcp_port
            for mappings in self.tcp_mappings.values()
            for m in mappings
        }

    def allocate_tcp_port(
        self,
        token: str,
        container_port: int,
        node_addr: str,
        node_host_port: int,
    ) -> int:
        """
        Allocate a CTFd-side TCP port for a non-HTTP container port and store
        the full mapping so the caller can configure an external TCP proxy.

        Returns the allocated CTFd TCP port number.
        """
        used = self._used_ctfd_tcp_ports()
        for port in range(self.tcp_range_start, self.tcp_range_end):
            if port not in used:
                mapping = TcpPortMapping(
                    ctfd_tcp_port=port,
                    node_addr=node_addr,
                    node_host_port=node_host_port,
                    container_port=container_port,
                )
                self.tcp_mappings.setdefault(token, []).append(mapping)
                print(
                    f"tcp://{RuntimeConfig.CTFD_DOMAIN_NAME}:{port} → "
                    f"{node_addr}:{node_host_port} (container:{container_port})"
                )
                self.update_proxy()
                return port
        raise Exception("No free TCP ports available in the configured range")

    # ------------------------------------------------------------------ #
    # Release                                                              #
    # ------------------------------------------------------------------ #

    def release_port(self, token: str):
        """
        Release the primary node port and any extra node ports for this token,
        plus all CTFd TCP ports.
        """
        # Primary port
        self.allocated_ports.pop(token, None)

        # Extra node ports  (keys like  "token:4444")
        prefix = f"{token}:"
        extra_keys = [k for k in self.allocated_ports if k.startswith(prefix)]
        for k in extra_keys:
            del self.allocated_ports[k]

        # CTFd TCP port mappings
        self.tcp_mappings.pop(token, None)

    def update_proxy(self):
        """
        Regenerate the nginx stream map config from the current tcp_mappings
        table and write it to disk.  One  server { }  block per active TCP
        port mapping.
        """
        import os
        import docker as _docker
 
        lines = []
        for token, mappings in self.tcp_mappings.items():
            for m in mappings:
                lines += [
                    "server {",
                    f"    listen              {m.ctfd_tcp_port};",
                    f"    proxy_pass          {m.node_addr}:{m.node_host_port};",
                    "    proxy_connect_timeout 5s;",
                    "    proxy_timeout       10m;",
                    "}",
                    "",
                ]
 
        tmp_path = STREAM_MAP_PATH + ".tmp"
        try:
            os.makedirs(os.path.dirname(STREAM_MAP_PATH), exist_ok=True)
            with open(tmp_path, "w") as f:
                f.write("\n".join(lines))
            os.replace(tmp_path, STREAM_MAP_PATH)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(
                f"[PortsManager] Failed to write stream map: {e}"
            )
            return
 
        # Signal nginx to reload its stream config
        try:
            client    = _docker.from_env()
            container = client.containers.get("ctfd-nginx-proxy")
            container.exec_run("nginx -s reload")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"[PortsManager] nginx reload failed: {e}"
            )  
    # ------------------------------------------------------------------ #
    # Lookup helpers                                                       #
    # ------------------------------------------------------------------ #

    def get_port(self, token: str) -> int | None:
        """Return the primary node host port for a token (HTTP proxy lookup)."""
        entry = self.allocated_ports.get(token)
        return entry[1] if entry else None

    def get_tcp_mappings(self, token: str) -> list[TcpPortMapping]:
        """Return all TCP port mappings for a token."""
        return self.tcp_mappings.get(token, [])

    def all_tcp_mappings(self) -> dict[str, list[TcpPortMapping]]:
        """Return the full TCP mapping table (for proxy config generation)."""
        return dict(self.tcp_mappings)