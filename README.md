# Docker Manager
## Features
### Challenge Setup
- Create challenges with one or more containers per team, each with its own image, label, and exposed ports
- Source images from an uploaded .tar archive or pull directly from a private Docker registry
- Configure per-container port mappings with custom labels shown to players
  
<img height="500" alt="image" src="https://github.com/user-attachments/assets/c34f0839-96b4-4230-862a-66fe109078f7" />

### Infrastructure Management
- Add and manage multiple worker nodes (via SSH) from the Docker Manager admin page
- The CTFd host orchestrates everything over SSH, no containers run on the CTFd host itself unless you want to
- Nodes are selected automatically based on available RAM; containers are scheduled

### Resource Controls
- Set per-container memory and CPU limits
- Configure a max number of concurrent challenges per team
- Define a host port allocation range for container access
- Assign containers to a custom Docker networks for isolated challenge environments or controlled inter-container communication
  
### Container Lifecycle
- Inactive containers are automatically suspended after a configurable timeout
- Containers are fully removed after a second, longer inactivity deadline
- Players can start, resume, reset, or stop their own containers from the challenge modal

### Private Registry Support
- Connect a private Docker registry with credentials and an optional namespace filter
- Upload a CA certificate for self-signed HTTPS registries, the plugin distributes it to all worker nodes automatically
- 
## Preview
<img height="250" alt="image" src="https://github.com/user-attachments/assets/b2262bc9-4bfd-4317-bd5b-f03ff5dfe523" />
<img height="250" alt="image" src="https://github.com/user-attachments/assets/fb1c09f5-4cfe-4e38-abc7-aed4c47793ca" />
<img height="250" alt="image" src="https://github.com/user-attachments/assets/b2ca007b-5c4e-4d66-9dfa-7ec6a31c39f9" />
<img height="250" alt="image" src="https://github.com/user-attachments/assets/e41af070-8767-4a2d-978a-52fb1e2a26dd" />
<img height="250" alt="image" src="https://github.com/user-attachments/assets/458137b0-6798-419e-ad67-3b52931b32af" />

<img height="250" alt="image" src="https://github.com/user-attachments/assets/0c031da3-5c26-453a-ba0b-e7fd8f67635f" />

## Plugin installation
Go to `CTFd/CTFd/plugins/` and clone the plugin in the folder and start the installer script.
```bash
git clone https://github.com/TLB1/Docker-Manager.git
cd Docker-Manager
./install.sh
```
