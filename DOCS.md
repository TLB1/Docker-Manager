# Docker-Manager Plugin - Reference Documentation

**Version:** 0.1.0  
**License:** MIT  
**Author:** TLB1

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Admin Interface](#admin-interface)
6. [Player API](#player-api)
7. [Challenge Setup](#challenge-setup)
8. [Nginx Reverse Proxy](#nginx-reverse-proxy)
9. [Worker Nodes](#worker-nodes)
10. [Container Lifecycle](#container-lifecycle)
11. [Metrics and Monitoring](#metrics-and-monitoring)
12. [Private Registry](#private-registry)
13. [Networking](#networking)
14. [Port Management](#port-management)
15. [Security](#security)
16. [Troubleshooting](#troubleshooting)

---

## Overview

Docker-Manager is a CTFd plugin that provides Docker container orchestration for hosting dynamic CTF challenges. Each team gets their own isolated container environment, provisioned on-demand from Docker images, and accessible via a per-challenge URL or TCP port.

Key capabilities:

- Provision challenge containers on remote worker nodes via SSH
- Expose containers via HTTP subdomain or raw TCP port using an Nginx reverse proxy
- Enforce per-team container quotas, CPU/memory limits, and container lifetimes
- Support multi-container challenges on a shared private network
- Integrate with a private Docker registry
- Collect real-time node and container metrics

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      CTFd Host                          │
│                                                         │
│  ┌─────────────────┐    ┌──────────────────────────┐    │
│  │   CTFd App      │    │   ctfd-nginx-proxy       │    │
│  │  (Flask/Python) │    │   (nginx:stable-alpine)  │    │
│  │                 │    │                          │    │
│  │  Docker-Manager │    │  :80/:443 HTTP proxy     │    │
│  │  Plugin         │    │  :10000-10100 TCP proxy  │    │
│  └────────┬────────┘    └──────────────────────────┘    │
│           │ SSH                                         │
└───────────┼─────────────────────────────────────────────┘
            │
     ┌──────┴──────┐
     │             │
┌────▼────┐   ┌────▼────┐
│ Worker  │   │ Worker  │  (more nodes as needed)
│ Node 1  │   │ Node 2  │
│         │   │         │
│ [ctr1]  │   │ [ctr3]  │
│ [ctr2]  │   │ [ctr4]  │
└─────────┘   └─────────┘
```

- The **CTFd host** runs the plugin and the Nginx proxy container.
- **Worker nodes** run challenge containers and are managed over SSH.
- Containers are accessed by players through the Nginx proxy, which routes by token (HTTP) or port (TCP).

---

## Installation

### Prerequisites

- A running CTFd instance using Docker Compose
- Python 3 available in the CTFd container
- `ssh-keygen` available on the host
- One or more worker nodes with Docker installed and SSH access from the CTFd host

### Steps

1. Clone or copy the plugin into `CTFd/CTFd/plugins/Docker-Manager/`.

2. From the plugin directory, run the installer:

   ```bash
   bash install.sh
   ```

   The installer will:

   - Verify the CTFd directory structure
   - Install Python dependencies (`docker`, `paramiko`, `gevent`, `pyyaml`)
   - Generate an ed25519 SSH keypair in `ssh/ctfd_ssh_keys/`
   - Patch the CTFd `Dockerfile` to include `openssh-client` and the generated keys
   - Patch `docker-compose.yml` to add the `ctfd-nginx-proxy` service, required volumes, and Docker socket access for the CTFd container

3. Optionally, the installer will prompt you to configure worker nodes interactively:
   - Copy the SSH public key to the node via `ssh-copy-id`
   - Set up a minimal sudoers rule for certificate operations
   - Configure the Docker daemon address pool (`172.17.0.0/12`, `/24` subnets)

4. Rebuild and restart CTFd:

   ```bash
   docker compose up --build -d
   ```

5. Log in to CTFd as an admin and navigate to **Plugins → Docker Manager** to finish configuration.

---

## Configuration

All settings are managed from the admin UI and persisted to the CTFd database. They are also accessible programmatically through the `RuntimeConfig` class in [core/config.py](core/config.py).

### General Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `WORKER_NODES` | `[]` | Worker node addresses in `user@host` format |
| `CTFD_DOMAIN_NAME` | `"challenges.ctf"` | Base domain for challenge URLs (e.g. `<token>.challenges.ctf`) |

### Registry Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `REGISTRY_URL` | `None` | Private Docker registry endpoint |
| `REGISTRY_USER` | `None` | Registry username |
| `REGISTRY_PASSWORD` | `None` | Registry password |
| `REGISTRY_NAMESPACE` | `None` | Default image namespace/prefix |
| `REGISTRY_CERT_PATH` | `None` | Path to CA certificate for self-signed registries |

### Resource Limits

| Setting | Default | Description |
|---------|---------|-------------|
| `MAX_ACTIVE_CONTAINERS_PER_GROUP` | `3` | Maximum concurrent containers per team |
| `MEM_LIMIT_PER_CONTAINER` | `536870912` (512 MB) | Memory limit per container in bytes |
| `MAX_SPARE_RAM` | `1073741824` (1 GB) | RAM to keep free on each node |
| `DOCKER_CONTAINER_CPU_QUOTA` | `50000` | CPU quota units (100000 = 1 full core) |

### Container Lifecycle

| Setting | Default | Description |
|---------|---------|-------------|
| `CONTAINER_SUSPENSION_INTERVAL` | `600` | Seconds of inactivity before a container is suspended |
| `DOCKER_CONTAINER_LIFETIME` | `3600` | Maximum container age in seconds before removal |

### Networking

| Setting | Default | Description |
|---------|---------|-------------|
| `DOCKER_CONTAINER_NETWORK` | `"ctfd-challenges"` | Docker network name on worker nodes |
| `INTERNAL_PORT_RANGE_START` | `50000` | Start of node-side HTTP port pool |
| `INTERNAL_PORT_RANGE_END` | `60000` | End of node-side HTTP port pool |
| `TCP_PORT_RANGE_START` | `10000` | Start of external TCP port range |
| `TCP_PORT_RANGE_END` | `10100` | End of external TCP port range |

### Performance

| Setting | Default | Description |
|---------|---------|-------------|
| `CONTAINER_CACHE_TTL` | `30` | Container metadata cache TTL in seconds |
| `METRICS_HISTORY_SIZE` | `60` | Number of data points kept per metric (ring buffer) |
| `METRICS_POLL_INTERVAL` | `10` | Seconds between metrics collection runs |
| `METRICS_STATS_WORKERS` | `20` | Concurrent threads for stats collection |

---

## Admin Interface

The admin UI is available at `/admin/docker_manager` and has three main sections.

### Configuration Panel (`/admin/docker_manager`)

A form for all `RuntimeConfig` settings. On save, the plugin reinitializes the Docker manager and cleans up orphaned containers.

### Node Management (`/admin/docker_manager/nodes`)

Displays a live overview of all worker nodes:

- Number of running containers per node
- Memory usage (total / used / free)
- CPU stats
- Per-container status snapshots

### Monitoring Dashboard (`/admin/docker_manager/monitoring`)

Shows real-time and historical metrics for nodes and containers, drawn from the background metrics collector.

### Registry Certificate

Upload or remove a CA certificate for private registries with self-signed TLS:

- `POST /admin/docker/registry/cert` — Upload certificate file
- `POST /admin/docker/registry/cert/delete` — Remove certificate

---

## Player API

All endpoints require a logged-in CTFd session. The team/user ID is extracted from the session automatically.

### Get Container Status

```
GET /docker/api/challenge/<challenge_id>/status
```

Returns whether a container exists for the calling team and the given challenge.

**Response:**

```json
{
  "success": true,
  "exists": true,
  "status": "running",
  "url": "http://<token>.challenges.ctf:8008"
}
```

`status` values: `running`, `paused`, `exited`

### Start Container

```
POST /docker/api/challenge/<challenge_id>/start
```

Creates and starts a new container. Fails if the team has reached `MAX_ACTIVE_CONTAINERS_PER_GROUP`.

**Response:**

```json
{
  "success": true,
  "url": "http://<token>.challenges.ctf:8008"
}
```

For TCP challenges, `url` will be `null` and TCP port information is included separately in the challenge description.

### Resume Container

```
POST /docker/api/challenge/<challenge_id>/resume
```

Unpauses a suspended container and resets inactivity and lifetime timers.

### Stop Container

```
POST /docker/api/challenge/<challenge_id>/stop
```

Suspends (pauses) the container manually.

### Reset Container

```
POST /docker/api/challenge/<challenge_id>/reset
```

Destroys and recreates the container from scratch.

### Error Response Format

```json
{
  "success": false,
  "error": "Human-readable error message"
}
```

---

## Challenge Setup

Challenge images are configured by administrators from the challenge editor.

### Supported Image Sources

1. **TAR archive upload** — Upload a Docker image saved with `docker save`. Maximum size: 5 GB.
2. **Private registry pull** — Pull an image from the configured registry.

### Multi-Container Challenges

A challenge can consist of multiple containers that share a private Docker bridge network. Each container gets a DNS alias for inter-container communication. Configure multiple image entries in the challenge editor with distinct network aliases.

### Port Mapping

Each container exposes one or more ports:

- **HTTP port**: Accessed via the Nginx HTTP proxy at `http://<token>.challenges.ctf:8008`.
- **TCP port**: Assigned a port from `TCP_PORT_RANGE_START`–`TCP_PORT_RANGE_END` and forwarded by Nginx stream.

Port mappings are set per image in the challenge configuration.

---

## Nginx Reverse Proxy

The `ctfd-nginx-proxy` service is a dedicated Nginx container added to `docker-compose.yml` by the installer. It handles all inbound player traffic to challenge containers.

### HTTP Proxying (port 8008)

1. Player opens `http://<token>.challenges.ctf:8008`
2. Nginx extracts `<token>` from the subdomain
3. An auth subrequest hits `/docker/api/token/<token>/backend`, which returns the container's backend address in an `X-Backend` header
4. Nginx forwards the request to that backend
5. A keepalive subrequest to `/docker/api/token/<token>/keepalive` resets the inactivity timer
6. Backend lookups are cached for 30 seconds

### TCP Stream Proxying (ports 10000–10100)

Each TCP challenge port is forwarded by an entry in `nginx/data/stream_map.conf`, generated dynamically by the plugin when containers are created or destroyed. Nginx listens on the allocated port and proxies raw TCP traffic to the corresponding container on the worker node.

### Configuration Files

| File | Purpose |
|------|---------|
| `nginx/nginx.conf` | Main Nginx configuration (static) |
| `nginx/data/data_map.conf` | CTFd domain mapping |
| `nginx/data/server_name.conf` | Wildcard server name block |
| `nginx/data/stream_map.conf` | TCP stream port forwarding rules (auto-generated) |

---

## Worker Nodes

Worker nodes are remote Linux hosts that run challenge containers. The CTFd host communicates with them over SSH using a dedicated keypair.

### Requirements

- Docker installed and the daemon running
- SSH access from the CTFd host
- The CTFd SSH public key in `~/.ssh/authorized_keys` for the configured user
- The user should have permission to run Docker commands (member of `docker` group)

### SSH Keypair

The installer generates `ssh/ctfd_ssh_keys/id_ed25519` (private) and `id_ed25519.pub` (public). The private key is copied into the CTFd Docker image. The public key must be added to each worker node.

### Adding a Node

1. Copy the public key to the node:
   ```bash
   ssh-copy-id -i ssh/ctfd_ssh_keys/id_ed25519.pub user@node-host
   ```
2. Add the node address (`user@host`) to `WORKER_NODES` in the admin config.

### Node Selection

When scheduling a new container, the plugin picks the node with the most available RAM (above `MAX_SPARE_RAM`). If no node has enough memory, the request is rejected.

### SSH Connection Pool

The plugin maintains a persistent SSH connection pool (`core/ssh.py`). Connections are reused across requests and automatically re-established if dropped. Keep-alive packets are sent every 30 seconds.

---

## Container Lifecycle

```
[Start request]
      │
      ▼
 Create container ──► Running ◄──── Resume
                          │              ▲
               Inactivity │              │
                  timeout │              │
                          ▼              │
                      Suspended ─────────
                          │
               Lifetime   │
                timeout   │
                          ▼
                       Removed
```

### States

| State | Description |
|-------|-------------|
| `running` | Container is active and accepting connections |
| `paused` | Container is suspended; memory preserved, CPU freed |
| `exited` | Container has stopped (unexpected) |

### Timers

Each container has two timers:

- **Suspension timer** (`CONTAINER_SUSPENSION_INTERVAL`): Reset on every player access (keepalive). When it expires, the container is paused.
- **Lifetime timer** (`DOCKER_CONTAINER_LIFETIME`): Counts from container creation. When it expires, the container is deleted regardless of state.

### Container Tokens

Each container is assigned a unique 12-character hex token at creation time. This token is:

- Used as the subdomain for HTTP access (`<token>.challenges.ctf`)
- Stored as a Docker label on the container
- Passed in API calls to identify the specific container

### Docker Labels

All managed containers carry these labels:

| Label | Value |
|-------|-------|
| `ctfd` | `true` |
| `ctfd-team-id` | Team ID |
| `ctfd-challenge-id` | Challenge ID |
| `ctfd-token` | Container token |
| `ctfd-container-index` | Index within multi-container challenge |
| `ctfd-network-alias` | DNS alias within challenge network |

---

## Metrics and Monitoring

The metrics subsystem runs in a background thread, polling all worker nodes at `METRICS_POLL_INTERVAL`-second intervals using `METRICS_STATS_WORKERS` concurrent threads.

### Collected Metrics

**Per node:**
- Total, used, and free memory
- Total CPU capacity
- Running container count

**Per container:**
- CPU usage (%)
- Memory usage (bytes)
- Container status

### Storage

Metrics are stored in a fixed-size ring buffer of `METRICS_HISTORY_SIZE` entries per metric. No external time-series database is required.

### Access

Metrics are served to the admin monitoring dashboard via a JSON API endpoint. The background collector is non-blocking; reads return cached data immediately without waiting for Docker calls.

---

## Private Registry

The plugin supports pulling challenge images from a private Docker registry.

### Configuration

Set the following in the admin config:

- **Registry URL** — e.g. `registry.example.com` or `https://registry.example.com:5000`
- **Username / Password** — Registry credentials
- **Namespace** — Optional default image prefix (e.g. `ctf-challenges`)
- **CA Certificate** — Upload a `.crt` file if the registry uses a self-signed certificate

### TLS / Self-Signed Certificates

Upload the CA certificate via the admin UI. The plugin will configure both the CTFd host and worker nodes to trust it when pulling images.

### Image Synchronization

When a container is first created for a challenge, the required image is pulled (or loaded from a TAR archive) on the target worker node. Subsequent containers for the same challenge on the same node reuse the cached image.

---

## Networking

### Challenge Networks

Each challenge gets its own Docker bridge network on the worker node. Multi-container challenges use this network for internal DNS resolution between containers (by network alias). The network is removed when the last container in the challenge is deleted.

### External Access

| Access type | Mechanism |
|-------------|-----------|
| HTTP | Nginx HTTP proxy → node port (50000–60000) |
| TCP | Nginx stream proxy → node port (50000–60000) |

### Port Pools

- **Node-side ports** (`INTERNAL_PORT_RANGE_START`–`INTERNAL_PORT_RANGE_END`): Allocated on the worker node for each exposed container port. Shared between HTTP and TCP services.
- **CTFd TCP ports** (`TCP_PORT_RANGE_START`–`TCP_PORT_RANGE_END`): Allocated on the CTFd host for Nginx stream forwarding of non-HTTP services.

Ports are released when the container is removed.

---

## Security

### SSH

- Ed25519 keypair generated at install time; private key lives only in the CTFd container.
- Known hosts are tracked to prevent MITM attacks on worker nodes.

### Container Isolation

- Each team's containers run in their own Docker bridge network.
- Docker labels enforce ownership; the plugin refuses to return or act on containers that do not match the requesting team.

### Resource Limits

- Memory and CPU quotas are enforced at the Docker level per container.
- Per-team concurrent container quotas prevent resource exhaustion.

### Registry

- Credentials are stored in the CTFd database (same security boundary as CTFd itself).
- CA certificate support prevents TLS errors with internal registries without disabling verification.

### Nginx Auth

- Every HTTP request to a challenge container triggers an auth subrequest to the CTFd app before proxying, ensuring only valid tokens receive traffic.

---

## Troubleshooting

### Containers not starting

- Check that the worker node is listed in `WORKER_NODES` and SSH is reachable.
- Verify the node has enough free RAM (above `MAX_SPARE_RAM`).
- Check that the challenge image has been uploaded or the registry is reachable.

### "Max containers" error

- The team has reached `MAX_ACTIVE_CONTAINERS_PER_GROUP`. They must stop a running challenge first.

### HTTP proxy returns 502

- The container may be paused or the backend lookup failed. Check node connectivity.
- Nginx's backend cache (30s TTL) may be stale after a container restart; wait for it to expire.

### TCP port not reachable

- Confirm the TCP port range is open in the firewall on the CTFd host.
- Check `nginx/data/stream_map.conf` was updated after the container was created.
- Nginx must be reloaded after `stream_map.conf` changes — the plugin does this automatically via `nginx -s reload`.

### Registry pull fails

- Verify credentials in the admin config.
- If using HTTPS with a self-signed cert, ensure the CA certificate has been uploaded.
- Check network connectivity from the worker node to the registry.

### SSH connection errors

- Confirm the public key (`ssh/ctfd_ssh_keys/id_ed25519.pub`) is in `~/.ssh/authorized_keys` on the worker node.
- Ensure the SSH private key inside the CTFd container is readable (`chmod 600`).
- Check that `openssh-client` is installed in the CTFd container (the installer patches the Dockerfile for this).

---

## File Reference

| Path | Purpose |
|------|---------|
| [__init__.py](__init__.py) | Plugin entry point (`load`/`unload`) |
| [config.json](config.json) | Plugin metadata for CTFd |
| [requirements.txt](requirements.txt) | Python dependencies |
| [install.sh](install.sh) | One-shot installer |
| [core/manager.py](core/manager.py) | `DockerManager` — main orchestration logic |
| [core/ssh.py](core/ssh.py) | `SSHPool` — persistent SSH connection pool |
| [core/config.py](core/config.py) | `RuntimeConfig` — all configurable defaults |
| [core/registry.py](core/registry.py) | Private registry authentication and pull logic |
| [core/ports.py](core/ports.py) | Port allocation and TCP mapping |
| [core/cache.py](core/cache.py) | Thread-safe TTL container metadata cache |
| [core/metrics.py](core/metrics.py) | Background metrics collection |
| [core/labels.py](core/labels.py) | Docker label constants |
| [core/timer.py](core/timer.py) | `RunnableTimer` for suspension/kill timeouts |
| [routes/admin.py](routes/admin.py) | Admin Flask routes |
| [routes/docker.py](routes/docker.py) | Player API Flask routes |
| [routes/challenges.py](routes/challenges.py) | Challenge image management routes |
| [models/node.py](models/node.py) | `Node` and `NodeStats` dataclasses |
| [models/container.py](models/container.py) | `ContainerDetails` dataclass |
| [utils/config_sync.py](utils/config_sync.py) | Config persistence to CTFd database |
| [nginx/nginx.conf](nginx/nginx.conf) | Nginx reverse proxy configuration |
| [templates/](templates/) | Admin HTML templates |
| [assets/](assets/) | Frontend JavaScript |
| [ssh/ctfd_ssh_keys/](ssh/ctfd_ssh_keys/) | Generated SSH keypair |
