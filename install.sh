#!/bin/bash
# ============================================================
#  Docker Manager – CTFd Plugin Installer
#  Run from: CTFd/CTFd/plugins/Docker-Manager/
# ============================================================
set -euo pipefail

# ── Colours ─────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()    { echo -e "\n${BOLD}${GREEN}==>${NC}${BOLD} $*${NC}"; }

# ── Path resolution ──────────────────────────────────────────
#   Project layout (assumed):
#       CTFd/
#       ├── Dockerfile
#       ├── docker-compose.yml
#       └── CTFd/
#           └── plugins/
#               └── Docker-Manager/   ← run from here
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTFD_ROOT="$(realpath "$SCRIPT_DIR/../../../")"
DOCKERFILE="$CTFD_ROOT/Dockerfile"
COMPOSE_FILE="$CTFD_ROOT/docker-compose.yml"

# Directories that must exist in the plugin
NGINX_DIR="$SCRIPT_DIR/nginx"
SSH_DIR="$SCRIPT_DIR/ssh/ctfd_ssh_keys"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

# ── Clear screen + Banner + confirmation ─────────────────────
clear

echo -e "\n${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   Docker Manager – CTFd Plugin Installer ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}\n"
info "Plugin dir   : $SCRIPT_DIR"
info "CTFd root    : $CTFD_ROOT"
echo ""
echo -e "  This will:"
echo -e "  ${CYAN}·${NC} Generate SSH keys in ${CYAN}ssh/ctfd_ssh_keys/${NC}"
echo -e "  ${CYAN}·${NC} Patch ${CYAN}$(basename "$DOCKERFILE")${NC} and ${CYAN}$(basename "$COMPOSE_FILE")${NC}"
echo -e "  ${CYAN}·${NC} Optionally configure your Docker nodes"
echo ""
echo -ne "${BOLD}  Proceed with installation? [Y/n] ${NC}"
read -r confirm
confirm="${confirm:-Y}"
[[ "$confirm" =~ ^[Yy]$ ]] || { echo -e "\n${YELLOW}Aborted.${NC}"; exit 0; }

# ── Pre-flight checks ────────────────────────────────────────
step "Running pre-flight checks"

[[ -f "$DOCKERFILE"           ]] || error "Dockerfile not found at $DOCKERFILE"
[[ -f "$COMPOSE_FILE"         ]] || error "docker-compose.yml not found at $COMPOSE_FILE"
[[ -d "$NGINX_DIR"            ]] || error "nginx/ directory missing from plugin folder"
[[ -f "$NGINX_DIR/nginx.conf" ]] || error "nginx/nginx.conf not found"
command -v ssh-keygen &>/dev/null || error "ssh-keygen is not installed"
command -v python3    &>/dev/null || error "python3 is required"

success "Pre-flight checks passed"

# ── Backup originals ─────────────────────────────────────────
step "Backing up original files"

cp "$DOCKERFILE"   "$DOCKERFILE.bak"   && info "Backed up Dockerfile"
cp "$COMPOSE_FILE" "$COMPOSE_FILE.bak" && info "Backed up docker-compose.yml"
success "Backups saved as *.bak"

# ── Python dependencies ──────────────────────────────────────
step "Installing Python dependencies"

if [[ -f "$REQUIREMENTS" ]]; then
    info "Installing from requirements.txt"
    pip3 install -q -r "$REQUIREMENTS" --break-system-packages 2>/dev/null \
        || pip3 install -q -r "$REQUIREMENTS" \
        || warn "pip install from requirements.txt failed – continuing"
fi

python3 -c "import yaml" 2>/dev/null || {
    info "PyYAML not found – installing"
    pip3 install -q pyyaml --break-system-packages 2>/dev/null \
        || pip3 install -q pyyaml \
        || error "Could not install PyYAML. Run: pip3 install pyyaml"
}
success "Python dependencies ready"

# ── SSH key setup ────────────────────────────────────────────
step "Setting up SSH keys"

if [[ ! -d "$SSH_DIR" ]]; then
    mkdir -p "$SSH_DIR"
    info "Created $SSH_DIR"
fi

if [[ -f "$SSH_DIR/id_ed25519" ]]; then
    warn "SSH key already exists – skipping keygen"
    warn "Delete $SSH_DIR/id_ed25519 to regenerate"
else
    ssh-keygen -t ed25519 -f "$SSH_DIR/id_ed25519" -N ""
    success "Generated ed25519 keypair"
fi

# Permissions – keep keys owned by the current user so this script can read
# them. The Dockerfile uses COPY --chown=1001:1001 which sets the correct
# ownership inside the container at build time.
chmod 700 "$SSH_DIR"
chmod 600 "$SSH_DIR/id_ed25519"
chmod 644 "$SSH_DIR/id_ed25519.pub"
success "Key permissions set (700/600/644)"

# ── Patch Dockerfile ─────────────────────────────────────────
step "Patching Dockerfile"

if grep -q "openssh-client" "$DOCKERFILE"; then
    warn "Dockerfile already patched – skipping"
else
    python3 - "$DOCKERFILE" <<'PYEOF'
import sys, pathlib

path  = pathlib.Path(sys.argv[1])
lines = path.read_text().splitlines(keepends=True)

snippet = (
    "# --- Docker Manager plugin requirements ---\n"
    "RUN apt-get update && apt-get install -y openssh-client\n"
    "COPY --chown=1001:1001 ./CTFd/plugins/Docker-Manager/ssh/ctfd_ssh_keys /home/ctfd/.ssh/\n"
    "RUN mkdir -p /.ssh && chown -R 1001:1001 /home/ctfd/.ssh\n"
    "RUN mkdir /var/images/ && chown 1001:1001 ./CTFd/plugins/Docker-Manager/nginx/ /var/images/\n"
    "# -------------------------------------------\n"
)

patched  = []
inserted = False
for line in lines:
    if not inserted and line.strip() == "USER 1001":
        patched.append(snippet + "\n")
        inserted = True
    patched.append(line)

if not inserted:
    sys.exit("ERROR: Could not find 'USER 1001' in Dockerfile – patch it manually")

path.write_text("".join(patched))
print("  Inserted snippet before USER 1001")
PYEOF
    success "Dockerfile patched"
fi

# ── Patch docker-compose.yml ─────────────────────────────────
step "Patching docker-compose.yml"

python3 - "$COMPOSE_FILE" <<'PYEOF'
import sys, pathlib

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML missing – run: pip3 install pyyaml")

compose_path = pathlib.Path(sys.argv[1])
text         = compose_path.read_text()

if "ctfd-nginx-proxy" in text:
    print("  docker-compose.yml already contains ctfd-nginx-proxy – skipping")
    sys.exit(0)

compose = yaml.safe_load(text) or {}

if not isinstance(compose.get("volumes"), dict):
    compose["volumes"] = {}
compose["volumes"].setdefault("proxy_data",    None)
compose["volumes"].setdefault("docker_images", None)

services = compose.setdefault("services", {})
services["ctfd-nginx-proxy"] = {
    "image":          "nginx:stable-alpine",
    "container_name": "ctfd-nginx-proxy",
    "restart":        "unless-stopped",
    "volumes": [
        "./CTFd/plugins/Docker-Manager/nginx/nginx.conf:/etc/nginx/nginx.conf:ro",
        "proxy_data:/etc/nginx/data",
    ],
    "ports":      ["8008:8008", "10000-10100:10000-10100"],
    "depends_on": ["ctfd"],
}

if "ctfd" not in services:
    sys.exit("ERROR: No 'ctfd' service found in docker-compose.yml")

ctfd = services["ctfd"]

if "group_add" not in ctfd or ctfd["group_add"] is None:
    ctfd["group_add"] = [989]
elif 989 not in ctfd["group_add"]:
    ctfd["group_add"].append(989)

new_vols = [
    "proxy_data:/opt/CTFd/CTFd/plugins/Docker-Manager/nginx/data",
    "docker_images:/var/images/",
    "/var/run/docker.sock:/var/run/docker.sock",
]
if not isinstance(ctfd.get("volumes"), list):
    ctfd["volumes"] = []
for v in new_vols:
    if v not in ctfd["volumes"]:
        ctfd["volumes"].append(v)

compose_path.write_text(yaml.dump(compose, default_flow_style=False, sort_keys=False))
print("  Added ctfd-nginx-proxy service")
print("  Added proxy_data + docker_images volumes")
print("  Patched ctfd service (group_add + volumes)")
PYEOF

success "docker-compose.yml patched"

# ── Node setup ───────────────────────────────────────────────
setup_node() {
    local node_user="$1"
    local node_ip="$2"

    echo ""
    echo -e "${BOLD}  Configuring node ${CYAN}${node_user}@${node_ip}${NC}"
    echo -e "  ${BOLD}────────────────────────────────────────${NC}"

    # Step 1 – copy public key (will prompt for password if needed)
    info "  Copying SSH public key to node..."
    if ssh-copy-id -i "$SSH_DIR/id_ed25519.pub" "${node_user}@${node_ip}"; then
        success "  Public key copied"
    else
        warn "  ssh-copy-id failed – skipping remote configuration"
        warn "  Run manually: ssh-copy-id -i $SSH_DIR/id_ed25519.pub ${node_user}@${node_ip}"
        return 1
    fi

    # Step 2 – configure sudoers + docker group remotely.
    # -t allocates a pseudo-TTY so sudo can prompt for a password if required.
    # We use printf to build the sudoers lines remotely so there's no nested
    # heredoc quoting issue when passing the script over SSH.
    info "  Applying sudoers rules and docker group on node..."
    info "  (You may be prompted for the node's sudo password)"
    ssh -t -i "$SSH_DIR/id_ed25519" \
        -o StrictHostKeyChecking=accept-new \
        -o PasswordAuthentication=no \
        "${node_user}@${node_ip}" \
        "set -e
         printf '%s ALL=(ALL) NOPASSWD: /bin/mkdir -p /etc/docker/certs.d/*\n'  '${node_user}' | sudo tee    /etc/sudoers.d/ctfd-cert > /dev/null
         printf '%s ALL=(ALL) NOPASSWD: /bin/cp /tmp/ctfd_ca_*.crt /etc/docker/certs.d/*/ca.crt\n' '${node_user}' | sudo tee -a /etc/sudoers.d/ctfd-cert > /dev/null
         printf '%s ALL=(ALL) NOPASSWD: /bin/chmod 644 /etc/docker/certs.d/*/ca.crt\n' '${node_user}' | sudo tee -a /etc/sudoers.d/ctfd-cert > /dev/null
         sudo chmod 440 /etc/sudoers.d/ctfd-cert
         sudo usermod -aG docker '${node_user}'"

    if [[ $? -eq 0 ]]; then
        success "  Node ${node_user}@${node_ip} configured"
    else
        warn "  Remote configuration failed – apply manually on ${node_ip}:"
        echo -e "    ${CYAN}sudo tee /etc/sudoers.d/ctfd-cert${NC}  (see README)"
        echo -e "    ${CYAN}sudo chmod 440 /etc/sudoers.d/ctfd-cert${NC}"
        echo -e "    ${CYAN}sudo usermod -aG docker ${node_user}${NC}"
    fi
}

# ── Interactive node loop ─────────────────────────────────────
step "Node Setup"

echo ""
echo -e "  The installer can now copy your SSH key and configure"
echo -e "  sudoers + docker group on each of your Docker nodes."
echo ""
echo -ne "${BOLD}  Would you like to add a node now? [y/N] ${NC}"
read -r add_node

while [[ "$add_node" =~ ^[Yy]$ ]]; do
    echo ""
    echo -ne "  ${BOLD}Node username:${NC} "
    read -r node_user

    echo -ne "  ${BOLD}Node IP or hostname:${NC} "
    read -r node_ip

    if [[ -z "$node_user" || -z "$node_ip" ]]; then
        warn "  Username and IP are both required – skipping"
    else
        setup_node "$node_user" "$node_ip" || true
    fi

    echo ""
    echo -ne "${BOLD}  Would you like to add another node? [y/N] ${NC}"
    read -r add_node
done

# ── Final summary ─────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║           Installation Complete!         ║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BOLD}Files modified:${NC}"
echo -e "  ${CYAN}$DOCKERFILE${NC}"
echo -e "  ${CYAN}$COMPOSE_FILE${NC}"
echo -e "  ${CYAN}$SSH_DIR/${NC}"
echo ""
echo -e "${GREEN}Backups saved as Dockerfile.bak and docker-compose.yml.bak${NC}"

# ── Optional docker build ─────────────────────────────────────
echo ""
echo -ne "${BOLD}  Would you like to apply the changes now and run the Docker build? [y/N] ${NC}"
read -r run_build
run_build="${run_build:-N}"

if [[ "$run_build" =~ ^[Yy]$ ]]; then
    step "Running docker compose up --build"
    cd "$CTFD_ROOT"
    sudo docker compose up --build
else
    echo ""
    info "Skipping build – run it yourself when ready:"
    echo -e "  ${CYAN}cd $CTFD_ROOT && sudo docker compose up --build${NC}"
fi

echo ""