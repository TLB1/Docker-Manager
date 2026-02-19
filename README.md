# Docker Manager

## Installation
Dockerfile
```
RUN mkdir -p /.ssh && chown -R 1001:1001 /.ssh
```
### SSH Keys
```
cd Docker-Manager/ssh/ctfd_ssh_keys
ssh-keygen -t ed25519 -f id_ed25519 -N ""
sudo chown -R 1001:1001 .
chmod 700 .
chmod 600 id_ed25519
chmod 644 id_ed25519.pub
```
``` 
ssh-copy-id -i id_ed25519.pub user@ip
```

