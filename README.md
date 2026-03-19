# Docker Manager

## Installation
### Plugin
Go to `CTFd/CTFd/plugins/` and clone the plugin in the folder
```bash
git clone https://github.com/TLB1/Docker-Manager.git
```
Add this at the end of the CTFd `Dockerfile` right before `USER 1001`
```
RUN apt-get update && apt-get install -y openssh-client
COPY --chown=1001:1001 ./CTFd/plugins/my-plugin/ssh/ctfd_ssh_keys /home/ctfd/.ssh/
RUN mkdir -p /.ssh && chown -R 1001:1001 /home/ctfd/.ssh
RUN chown 1001:1001 ./CTFd/plugins/my-plugin/nginx/token_map.conf
RUN mkdir /var/images/ && chown 1001:1001 ./CTFd/plugins/my-plugin/nginx/token_map.conf /var/images/
```
Add this to the top of `docker-compose.yml`
```yaml
volumes:
  token_map_data:
services:
  ctfd-nginx-proxy:
    image: nginx:stable-alpine
    container_name: ctfd-nginx-proxy
    restart: unless-stopped
    volumes:
      - ./CTFd/plugins/my-plugin/nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - token_map_data:/etc/nginx/data
    ports:
      - "8008:8008"
    depends_on:
      - ctfd
```
Add this to the existing ctfd service in `docker-compose.yml`
```yaml
    group_add:
        - 989
    volumes:
        - token_map_data:/opt/CTFd/CTFd/plugins/my-plugin/nginx/data
        - /var/run/docker.sock:/var/run/docker.sock
```  
### SSH Keys
    
```bash
cd Docker-Manager/ssh/ctfd_ssh_keys
ssh-keygen -t ed25519 -f id_ed25519 -N ""
sudo chown -R 1001:1001 .
chmod 700 .
chmod 600 id_ed25519
chmod 644 id_ed25519.pub
```
### On nodes
```bash
ssh-copy-id -i id_ed25519.pub user@ip
```
```
cat << 'EOF' | sudo tee /etc/sudoers.d/ctfd-cert
<username> ALL=(ALL) NOPASSWD: /bin/mkdir -p /etc/docker/certs.d/*
<username> ALL=(ALL) NOPASSWD: /bin/cp /tmp/ctfd_ca_*.crt /etc/docker/certs.d/*/ca.crt
<username> ALL=(ALL) NOPASSWD: /bin/chmod 644 /etc/docker/certs.d/*/ca.crt
EOF
sudo chmod 440 /etc/sudoers.d/ctfd-cert
sudo usermod -aG docker <username>
```
Replace `<username>` with the SSH user configured in your worker nodes