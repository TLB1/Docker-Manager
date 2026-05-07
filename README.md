# Docker Manager
## Features
### Challenge Setup
- Create challenges with one or more containers per team, each with its own image, label, and exposed ports (HTTP and TCP)
- Source images from an uploaded .tar archive or pull directly from a private Docker registry
- Configure per-container port mappings with custom labels shown to players
- Additional options can be set, like network isolation, or set containers as global 

### Infrastructure Management
- Add and manage multiple worker nodes (via SSH) from the Docker Manager admin page
- The CTFd host orchestrates everything over SSH, no containers run on the CTFd host itself unless you want to
- Nodes are selected automatically based on available RAM; containers are scheduled
- Images are dynamically pulled to worker nodes in the background

### Resource Controls
- Set per-container memory and CPU limits
- Configure a max number of concurrent challenges per team
- Define a host port allocation range for container access
- Assign containers to a custom Docker networks for isolated challenge environments or controlled inter-container communication
- Create globally accessible challenges can be utilised to save on system resources
  
### Container Lifecycle
- Inactive containers are automatically suspended after a configurable timeout
- Containers are fully removed after a second, longer inactivity deadline
- Players can start, resume, reset, or stop their own containers from the challenge modal
- Global challenges use the same system but can only be stopped by admins but started by anyone

### Private Registry Support
- Connect a private Docker registry with credentials and an optional namespace filter
- Upload a CA certificate for self-signed HTTPS registries, the plugin distributes it to all worker nodes automatically
- Registries are not required as you can always upload images directly during challenge creation
  
## Preview
<img height="250" alt="image" src="https://github.com/user-attachments/assets/b2262bc9-4bfd-4317-bd5b-f03ff5dfe523" />
<img height="250" alt="image" src="https://github.com/user-attachments/assets/fb1c09f5-4cfe-4e38-abc7-aed4c47793ca" />
<img height="250" alt="image" src="https://github.com/user-attachments/assets/b2ca007b-5c4e-4d66-9dfa-7ec6a31c39f9" />
<img height="250" alt="image" src="https://github.com/user-attachments/assets/e41af070-8767-4a2d-978a-52fb1e2a26dd" />
<img height="250" alt="image" src="https://github.com/user-attachments/assets/394fc063-a235-4575-a3f9-150dc24e4aaa" />
<img height="250" alt="image" src="https://github.com/user-attachments/assets/ebe7da2c-c995-4790-95e0-72b517421d04" />


<img height="250" alt="image" src="https://github.com/user-attachments/assets/0c031da3-5c26-453a-ba0b-e7fd8f67635f" />

## Plugin installation

### Automatic install (recommended)
Go to `CTFd/CTFd/plugins/`, clone the plugin in the folder and start the installer script. Follow the given instructions.
```bash
git clone https://github.com/TLB1/Docker-Manager.git
cd Docker-Manager
./install.sh
```
After CTFd is up, log in and finish configuration (worker nodes, registry, port ranges, limits) under `Admin Panel -> Plugins -> Docker Settings`.

### Manual install
If you'd rather not run the script, perform the same steps by hand from the plugin directory `CTFd/CTFd/plugins/Docker-Manager/`.

#### 1. Generate the SSH keypair
```bash
mkdir -p ssh/ctfd_ssh_keys
ssh-keygen -t ed25519 -f ssh/ctfd_ssh_keys/id_ed25519 -N ""
chmod 700 ssh/ctfd_ssh_keys
chmod 600 ssh/ctfd_ssh_keys/id_ed25519
chmod 644 ssh/ctfd_ssh_keys/id_ed25519.pub
```

#### 2. Patch the CTFd `Dockerfile`
Insert the following block **immediately before** the `USER 1001` line in `CTFd/Dockerfile`:
```Dockerfile
# --- Docker Manager plugin requirements ---
RUN apt-get update && apt-get install -y openssh-client
COPY --chown=1001:1001 ./CTFd/plugins/Docker-Manager/ssh/ctfd_ssh_keys /home/ctfd/.ssh/
RUN mkdir -p /.ssh && chown -R 1001:1001 /home/ctfd/.ssh
RUN mkdir /var/images/ && chown 1001:1001 ./CTFd/plugins/Docker-Manager/nginx/ /var/images/
# -------------------------------------------
```

#### 3. Patch `docker-compose.yml`
Find the host's docker group GID:
```bash
getent group docker | cut -d: -f3
```

Add the named volumes at the top level:
```yaml
volumes:
  proxy_data:
  docker_images:
  # ...keep any existing volumes
```

Add the proxy service to `services:`:
```yaml
  ctfd-nginx-proxy:
    image: nginx:stable-alpine
    container_name: ctfd-nginx-proxy
    restart: always
    volumes:
      - ./CTFd/plugins/Docker-Manager/nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - proxy_data:/etc/nginx/data
    network_mode: host
    depends_on:
      - ctfd
```

Patch the existing `ctfd` service — replace `<DOCKER_GID>` with the GID from above:
```yaml
  ctfd:
    # ...existing config
    group_add:
      - <DOCKER_GID>
    volumes:
      # ...existing mounts
      - proxy_data:/opt/CTFd/CTFd/plugins/Docker-Manager/nginx/data
      - docker_images:/var/images/
      - /var/run/docker.sock:/var/run/docker.sock
```

#### 4. Update the nginx resolver
Edit `nginx/nginx.conf` and replace the `resolver` directive with your host's real upstream DNS servers (from `/etc/resolv.conf`, ignoring `127.0.0.53` and other loopback stubs). Example:
```nginx
resolver 1.1.1.1 8.8.8.8 valid=30s ipv6=off;
```

#### 5. Worker-node setup
For every Docker worker node, copy the public key and grant the SSH user the permissions the plugin needs.

On the CTFd host:
```bash
ssh-copy-id -i ssh/ctfd_ssh_keys/id_ed25519.pub <user>@<node>
```

On the worker node, as a user with `sudo`:
```bash
sudo tee /etc/sudoers.d/ctfd-cert > /dev/null <<EOF
<user> ALL=(ALL) NOPASSWD: /bin/mkdir -p /etc/docker/certs.d/*
<user> ALL=(ALL) NOPASSWD: /bin/cp /tmp/ctfd_ca_*.crt /etc/docker/certs.d/*/ca.crt
<user> ALL=(ALL) NOPASSWD: /bin/chmod 644 /etc/docker/certs.d/*/ca.crt
EOF
sudo chmod 440 /etc/sudoers.d/ctfd-cert
sudo usermod -aG docker <user>
```

Optionally but recommended, expand Docker's address pools so the node can host many challenge bridge networks:
```bash
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "default-address-pools": [
    { "base": "10.200.0.0/13", "size": 24 }
  ]
}
EOF
sudo systemctl restart docker
```

#### 6. Build and start CTFd
From the CTFd root:
```bash
sudo docker compose up --build
```

After CTFd is up, log in and finish configuration (worker nodes, registry, port ranges, limits) under `Admin Panel -> Plugins -> Docker Settings`.
