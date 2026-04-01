from pathlib import Path

from flask import Blueprint, make_response, request, jsonify, send_from_directory, current_app
from flask.templating import render_template
from werkzeug.utils import secure_filename
import os
import re
import uuid
import tarfile

from CTFd.plugins.challenges import BaseChallenge, CHALLENGE_CLASSES
from CTFd.models import db, Challenges
from CTFd.utils.decorators import admins_only
from CTFd.utils.user import get_current_team, get_current_user
from ..core.labels import DockerLabels
from ..core.config import RuntimeConfig
from ..core.manager import ContainerSpec

PLUGIN_NAME = "Docker-Manager"
MAX_IMAGE_SIZE = 5 * 1024 * 1024 * 1024  # 5 GB


bp = Blueprint("docker_image_challenge", __name__, template_folder="templates")


def get_docker_store_path():
    path = Path("/var/images/")
    os.makedirs(path, exist_ok=True)
    return path


def get_team_or_user():
    team = get_current_team()
    if team:
        return team
    return get_current_user()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_scheme(image: str) -> str:
    """Remove http:// or https:// from an image reference — Docker never accepts a scheme."""
    for scheme in ("https://", "http://"):
        if image.startswith(scheme):
            return image[len(scheme):]
    return image


def _resolve_image_for_config(config):
    """Return the docker image tag/name for a DockerContainerConfig row."""
    if config.docker_image_filename:
        tar_path = os.path.join(get_docker_store_path(), config.docker_image_filename)
        manager = current_app.docker_manager
        return manager._get_image_from_tar(tar_path), tar_path
    if config.docker_image_name:
        # Strip any accidental scheme stored in the DB — Docker image refs
        # must be bare host:port/repo:tag with no http:// / https:// prefix.
        return _strip_scheme(config.docker_image_name), None
    return None, None


def _label_to_alias(label, index: int) -> str:
    """Convert a container label to a valid DNS network alias."""
    if label:
        alias = re.sub(r"[^a-z0-9-]", "-", label.lower()).strip("-")
        if alias:
            return alias
    return f"container-{index}"


def _start_all_containers(manager, actor_name, challenge_id, configs, use_network=True):
    """
    Start one container per DockerContainerConfig entry on the same node.
    When use_network=True all containers share a per-challenge bridge network.
    Returns a list of dicts: [{index, label, token}, ...]
    Raises on first failure.
    """
    # ── Sync all images first ─────────────────────────────────────────
    resolved = []
    for cfg in configs:
        image, tar_path = _resolve_image_for_config(cfg)
        if image is None:
            raise ValueError(f"Container '{cfg.label or cfg.container_index}' has no image configured")

        if tar_path:
            try:
                manager.sync_tar_image(tar_path)
            except Exception as e:
                current_app.logger.warning(
                    f"[DockerImageChallenge] Image sync warning for config {cfg.id}: {e}"
                )
        elif image:
            try:
                manager.sync_registry_image(image)
            except Exception as e:
                current_app.logger.warning(
                    f"[DockerImageChallenge] Registry image sync warning for config {cfg.id}: {e}"
                )
        resolved.append((cfg, image))

    # ── Build one ContainerSpec per config ────────────────────────────
    # expose_port=False when the config has neither port_mappings nor a
    # container_port — the container runs on the challenge network only.
    specs = [
        ContainerSpec(
            image=image,
            network_alias=_label_to_alias(cfg.label, cfg.container_index),
            port_mappings=cfg.port_mappings or [],
            container_port=cfg.container_port or None,
            expose_port=bool(cfg.port_mappings or cfg.container_port),
        )
        for cfg, image in resolved
    ]

    # ── Start all containers in one call (same node, shared network) ──
    tokens = manager.create_challenge_containers(actor_name, challenge_id, specs, use_network=use_network)

    return [
        {
            "index": cfg.container_index,
            "label": cfg.label or f"Container {cfg.container_index}",
            "token": token,
        }
        for (cfg, _), token in zip(resolved, tokens)
    ]


def _remove_all_containers(manager, actor_name, challenge_id, configs):
    """Remove every running container for the given challenge."""
    for cfg in configs:
        container = manager.get_container_for_team_challenge(
            actor_name, challenge_id, container_index=cfg.container_index
        )
        if container:
            token = container.labels.get(DockerLabels.TOKEN)
            try:
                manager.remove_container(token)
            except Exception as e:
                current_app.logger.warning(
                    f"[DockerImageChallenge] Could not remove container {token}: {e}"
                )


def _container_status_list(manager, actor_name, challenge_id, configs):
    """
    Return a status list for every config entry.
    Each entry: {index, label, port_mappings, exists, status, token, url}
    port_mappings is always included so the JS can render labelled port links.
    """
    results = []
    for cfg in configs:
        container = manager.get_container_for_team_challenge(
            actor_name, challenge_id, container_index=cfg.container_index
        )
        if container:
            token = container.labels.get(DockerLabels.TOKEN)

            # Enrich each port_mapping with live TCP allocation info so the
            # frontend can display  hostname:NNNNN  for TCP ports.
            tcp_allocs = {
                m.container_port: m
                for m in manager.ports_manager.get_tcp_mappings(token)
            }
            enriched = []
            for pm in cfg.port_mappings:
                pm_copy = dict(pm)
                if pm.get("http", True):
                    pm_copy["url"] = f"http://{token}.{RuntimeConfig.CTFD_DOMAIN_NAME}:8008/"
                tcp = tcp_allocs.get(pm.get("container_port"))
                if tcp:
                    pm_copy["ctfd_tcp_port"] = tcp.ctfd_tcp_port
                    pm_copy["node_addr"]     = tcp.node_addr
                    pm_copy["node_host_port"] = tcp.node_host_port
                enriched.append(pm_copy)

            results.append({
                "index": cfg.container_index,
                "label": cfg.label or f"Container {cfg.container_index}",
                "port_mappings": enriched,
                "exists": True,
                "status": container.status,
                "token": token,
            })
        else:
            results.append({
                "index": cfg.container_index,
                "label": cfg.label or f"Container {cfg.container_index}",
                "port_mappings": cfg.port_mappings,
                "exists": False,
                "status": None,
                "token": None,
            })
    return results


# ---------------------------------------------------------------------------
# Player-facing API
# ---------------------------------------------------------------------------

@bp.route("/docker/api/challenge/<int:challenge_id>/status", methods=["GET"])
def api_docker_status(challenge_id):
    actor = get_team_or_user()
    if not actor:
        return jsonify({"success": False, "error": "You must be logged in"}), 403

    manager = current_app.docker_manager
    if not manager:
        return jsonify({"success": False, "error": "Docker manager not configured"}), 500

    challenge = DockerImageChallengeModel.query.get(challenge_id)
    if not challenge:
        return jsonify({"success": False, "error": "Challenge not found"}), 404

    configs = _get_ordered_configs(challenge_id)
    containers = _container_status_list(manager, actor.name, challenge_id, configs)

    return jsonify({
        "success": True,
        "containers": containers,
        # Convenience: overall "exists" is True if at least one container is up
        "exists": any(c["exists"] for c in containers),
    })


@bp.route("/docker/api/challenge/<int:challenge_id>/start", methods=["POST"])
def api_docker_start(challenge_id):
    actor = get_team_or_user()
    if not actor:
        return jsonify({"success": False, "error": "You must be logged in"}), 403

    manager = current_app.docker_manager
    if not manager:
        return jsonify({"success": False, "error": "Docker manager not configured"}), 500

    challenge = DockerImageChallengeModel.query.get(challenge_id)
    if not challenge:
        return jsonify({"success": False, "error": "Challenge not found"}), 404

    configs = _get_ordered_configs(challenge_id)
    if not configs:
        return jsonify({"success": False, "error": "No containers configured for this challenge"}), 400

    try:
        results = _start_all_containers(manager, actor.name, challenge_id, configs,
                                        use_network=challenge.use_challenge_network)
        return jsonify({"success": True, "containers": results})
    except Exception as e:
        current_app.logger.error(f"[DockerImageChallenge] Failed to start containers: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/docker/api/challenge/<int:challenge_id>/resume", methods=["POST"])
def api_docker_resume(challenge_id):
    actor = get_team_or_user()
    if not actor:
        return jsonify({"success": False, "error": "You must be logged in"}), 403

    manager = current_app.docker_manager
    if not manager:
        return jsonify({"success": False, "error": "Docker manager not configured"}), 500

    challenge = DockerImageChallengeModel.query.get(challenge_id)
    if not challenge:
        return jsonify({"success": False, "error": "Challenge not found"}), 404

    configs = _get_ordered_configs(challenge_id)
    resumed = []
    errors = []

    for cfg in configs:
        container = manager.get_container_for_team_challenge(
            actor.name, challenge_id, container_index=cfg.container_index
        )
        if not container:
            errors.append(f"No container for index {cfg.container_index}")
            continue
        token = container.labels.get(DockerLabels.TOKEN)
        try:
            ok = manager.resume_container(token)
            resumed.append({
                "index": cfg.container_index,
                "label": cfg.label or f"Container {cfg.container_index}",
                "token": token,
                "resumed": ok,
            })
        except Exception as e:
            current_app.logger.error(
                f"[DockerImageChallenge] Failed to resume container {token}: {e}"
            )
            errors.append(str(e))

    return jsonify({"success": True, "resumed": resumed, "errors": errors})


@bp.route("/docker/api/token/<token>/status", methods=["GET"])
def api_token_status(token):
    """Return container status by token only — no auth required (token is the secret)."""
    manager = current_app.docker_manager
    if not manager:
        return jsonify({"success": False, "error": "Docker manager not configured"}), 500

    container = manager.get_container_by_token(token)
    if not container:
        return jsonify({"success": True, "exists": False})

    url = f"http://{token}.{RuntimeConfig.CTFD_DOMAIN_NAME}:8008/"
    return jsonify({
        "success": True,
        "exists": True,
        "status": container.status,
        "url": url,
        "token": token,
    })


@bp.route("/docker/api/token/<token>/resume", methods=["POST"])
def api_token_resume(token):
    """Resume a container by token only — no auth required (token is the secret)."""
    manager = current_app.docker_manager
    if not manager:
        return jsonify({"success": False, "error": "Docker manager not configured"}), 500

    container = manager.get_container_by_token(token)
    if not container:
        return jsonify({"success": False, "error": "Container not found"}), 404

    try:
        ok = manager.resume_container(token)
        url = f"http://{token}.{RuntimeConfig.CTFD_DOMAIN_NAME}:8008/"
        return jsonify({"success": True, "resumed": ok, "token": token, "url": url})
    except Exception as e:
        current_app.logger.error(f"[DockerImageChallenge] Failed to resume container by token: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/docker/api/challenge/<int:challenge_id>/stop", methods=["POST"])
def api_docker_stop(challenge_id):
    actor = get_team_or_user()
    if not actor:
        return jsonify({"success": False, "error": "You must be logged in"}), 403

    manager = current_app.docker_manager
    if not manager:
        return jsonify({"success": False, "error": "Docker manager not configured"}), 500

    challenge = DockerImageChallengeModel.query.get(challenge_id)
    if not challenge:
        return jsonify({"success": False, "error": "Challenge not found"}), 404

    configs = _get_ordered_configs(challenge_id)
    _remove_all_containers(manager, actor.name, challenge_id, configs)
    return jsonify({"success": True, "stopped": True})


@bp.route("/docker/api/challenge/<int:challenge_id>/reset", methods=["POST"])
def api_docker_reset(challenge_id):
    actor = get_team_or_user()
    if not actor:
        return jsonify({"success": False, "error": "You must be logged in"}), 403

    manager = current_app.docker_manager
    if not manager:
        return jsonify({"success": False, "error": "Docker manager not configured"}), 500

    challenge = DockerImageChallengeModel.query.get(challenge_id)
    if not challenge:
        return jsonify({"success": False, "error": "Challenge not found"}), 404

    configs = _get_ordered_configs(challenge_id)
    if not configs:
        return jsonify({"success": False, "error": "No containers configured for this challenge"}), 400

    _remove_all_containers(manager, actor.name, challenge_id, configs)

    try:
        results = _start_all_containers(manager, actor.name, challenge_id, configs,
                                        use_network=challenge.use_challenge_network)
        return jsonify({"success": True, "containers": results})
    except Exception as e:
        current_app.logger.error(f"[DockerImageChallenge] Reset failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/docker/api/token/<token>/keepalive", methods=["GET", "POST"])
def api_token_keepalive(token):
    """Called by nginx mirror on every proxied request to reset the suspension timer."""
    manager = current_app.docker_manager
    if not manager:
        return "", 204
    try:
        manager.set_timers(token)
    except Exception:
        pass
    return "", 204

@bp.route("/docker/api/token/<token>/backend")
def get_backend(token):
    manager = current_app.docker_manager
    server_url, port = manager.ports_manager.allocated_ports.get(token, (None, None))
    if not server_url:
        return "", 404
    response = make_response("", 200)
    response.headers["X-Backend"] = f"{server_url}:{port}"
    return response


@bp.route("/challenge-unavailable/<token>")
def challenge_unavailable(token):
    ctfd_root = current_app.config.get("APPLICATION_ROOT", "").rstrip("/")
    return render_template(
        "challenge_unavailable.html",
        token=token,
        ctfd_root=ctfd_root,
        ctfd_domain=RuntimeConfig.CTFD_DOMAIN_NAME,
    )


# ---------------------------------------------------------------------------
# Admin API — image upload (unchanged)
# ---------------------------------------------------------------------------

@bp.route("/admin/docker/upload", methods=["POST"])
@admins_only
def upload_docker_image():
    if "image_tar" not in request.files:
        return jsonify({"success": False, "error": "No file part"}), 400

    f = request.files["image_tar"]
    if not f.filename:
        return jsonify({"success": False, "error": "No file selected"}), 400
    if not f.filename.lower().endswith(".tar"):
        return jsonify({"success": False, "error": "Only .tar files are allowed"}), 400

    content_length = request.content_length
    if content_length is not None:
        if content_length == 0:
            return jsonify({"success": False, "error": "Empty file"}), 400
        if content_length > MAX_IMAGE_SIZE:
            return jsonify({"success": False, "error": f"File too large (max {MAX_IMAGE_SIZE // 1024 ** 3} GB)"}), 413

    unique_filename = f"{uuid.uuid4().hex}_{secure_filename(f.filename)}"
    save_path = os.path.join(get_docker_store_path(), unique_filename)

    try:
        bytes_written = 0
        with open(save_path, "wb") as out:
            while chunk := f.stream.read(4 * 1024 * 1024):
                bytes_written += len(chunk)
                if bytes_written > MAX_IMAGE_SIZE:
                    out.close()
                    os.unlink(save_path)
                    return jsonify({"success": False, "error": f"File too large (max {MAX_IMAGE_SIZE // 1024 ** 3} GB)"}), 413
                out.write(chunk)

        if bytes_written == 0:
            os.unlink(save_path)
            return jsonify({"success": False, "error": "Empty file"}), 400

        if not tarfile.is_tarfile(save_path):
            os.unlink(save_path)
            return jsonify({"success": False, "error": "Not a valid tar archive"}), 400

        current_app.logger.info(f"[DockerImageChallenge] Uploaded {unique_filename} ({bytes_written / 1024 / 1024:.1f} MB)")
        return jsonify({"success": True, "filename": unique_filename})

    except Exception as e:
        current_app.logger.error(f"[DockerImageChallenge] Upload failed: {e}")
        if os.path.exists(save_path):
            os.unlink(save_path)
        return jsonify({"success": False, "error": "Upload failed"}), 500


# ---------------------------------------------------------------------------
# Admin API — container config CRUD
# ---------------------------------------------------------------------------

@bp.route("/admin/docker/challenge/<int:challenge_id>/containers", methods=["GET"])
@admins_only
def admin_list_containers(challenge_id):
    configs = _get_ordered_configs(challenge_id)
    return jsonify({
        "success": True,
        "containers": [_config_to_dict(c) for c in configs],
    })


@bp.route("/admin/docker/challenge/<int:challenge_id>/containers", methods=["POST"])
@admins_only
def admin_add_container(challenge_id):
    """Append a new container config to the challenge."""
    body = request.get_json() or {}

    # Auto-assign the next index
    last = (
        DockerContainerConfig.query
        .filter_by(challenge_id=challenge_id)
        .order_by(DockerContainerConfig.container_index.desc())
        .first()
    )
    next_index = (last.container_index + 1) if last else 0

    cfg = DockerContainerConfig(
        challenge_id=challenge_id,
        container_index=next_index,
        label=body.get("label") or None,
        docker_image_filename=body.get("docker_image_filename") or None,
        docker_image_name=body.get("docker_image_name") or None,
        container_port=_int_or_none(body.get("container_port")),
    )
    db.session.add(cfg)
    db.session.commit()
    return jsonify({"success": True, "container": _config_to_dict(cfg)}), 201


@bp.route("/admin/docker/challenge/<int:challenge_id>/containers/<int:config_id>", methods=["PATCH"])
@admins_only
def admin_update_container(challenge_id, config_id):
    cfg = DockerContainerConfig.query.filter_by(id=config_id, challenge_id=challenge_id).first_or_404()
    body = request.get_json() or {}

    if "label" in body:
        cfg.label = body["label"] or None

    if "docker_image_filename" in body:
        new_fn = body["docker_image_filename"] or None
        if cfg.docker_image_filename and cfg.docker_image_filename != new_fn:
            DockerImageChallenge._delete_image_file(cfg.docker_image_filename)
        cfg.docker_image_filename = new_fn

    if "docker_image_name" in body:
        cfg.docker_image_name = body["docker_image_name"] or None

    if "container_port" in body:
        cfg.container_port = _int_or_none(body["container_port"])

    db.session.commit()
    return jsonify({"success": True, "container": _config_to_dict(cfg)})


@bp.route("/admin/docker/challenge/<int:challenge_id>/containers/<int:config_id>", methods=["DELETE"])
@admins_only
def admin_delete_container(challenge_id, config_id):
    cfg = DockerContainerConfig.query.filter_by(id=config_id, challenge_id=challenge_id).first_or_404()
    if cfg.docker_image_filename:
        DockerImageChallenge._delete_image_file(cfg.docker_image_filename)
    db.session.delete(cfg)
    db.session.commit()
    return jsonify({"success": True})


@bp.route("/uploads/docker_images/<path:filename>")
@admins_only
def serve_docker_image(filename):
    return send_from_directory(get_docker_store_path(), filename)


@bp.route("/admin/docker/images", methods=["GET"])
@admins_only
def admin_list_registry_images():
    """
    Return all available Docker images.

    Strategy:
      1. Ask the RegistryManager for images from the private registry
         (queries the Registry HTTP API v2 — no Docker daemon needed).
      2. If the registry is not configured or returns nothing, fall back
         to listing images cached locally on each Docker node.

    Each entry always contains at least: { tag, source }
    Registry entries also have: { repo, short_tag }
    Node-local entries also have: { id, size_mb, node }
    """
    manager = current_app.docker_manager
    if not manager:
        return jsonify({"success": False, "error": "Docker manager not configured"}), 500

    # ── 1. Try the private registry ──────────────────────────────────
    registry = manager.registry
    images   = []

    if registry and registry._is_configured():
        try:
            images = registry.list_images()
        except Exception as exc:
            current_app.logger.warning(
                f"[DockerImageChallenge] Registry listing failed, falling back to nodes: {exc}"
            )

    # ── 2. Fall back to node-local images ────────────────────────────
    if not images:
        seen: set = set()
        for node in manager.nodes:
            try:
                node_images = manager._node_call(node, node.client.images.list)
                for img in node_images:
                    for tag in (img.tags or []):
                        if tag in seen:
                            continue
                        seen.add(tag)
                        images.append({
                            "tag":     tag,
                            "id":      img.short_id,
                            "size_mb": round(img.attrs.get("Size", 0) / 1024 / 1024, 1),
                            "node":    node.address,
                            "source":  "node",
                        })
            except Exception as exc:
                current_app.logger.warning(
                    f"[DockerImageChallenge] Could not list images on {node}: {exc}"
                )
        images.sort(key=lambda x: x["tag"])

    return jsonify({"success": True, "images": images, "source": images[0]["source"] if images else "none"})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_ordered_configs(challenge_id):
    return (
        DockerContainerConfig.query
        .filter_by(challenge_id=challenge_id)
        .order_by(DockerContainerConfig.container_index.asc())
        .all()
    )


def _config_to_dict(cfg):
    return {
        "id": cfg.id,
        "challenge_id": cfg.challenge_id,
        "index": cfg.container_index,
        "label": cfg.label,
        "docker_image_filename": cfg.docker_image_filename,
        "docker_image_name": cfg.docker_image_name,
        # port_mappings is a JSON list of {container_port, host_label} objects
        # stored in the new column; fall back to legacy single-port for old rows.
        "port_mappings": cfg.port_mappings if cfg.port_mappings else (
            [{"container_port": cfg.container_port}] if cfg.container_port else []
        ),
    }


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class DockerContainerConfig(db.Model):
    """
    One row per container that belongs to a challenge.
    A challenge can have many of these (one-to-many).
    """
    __tablename__ = "docker_container_configs"

    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(
        db.Integer,
        db.ForeignKey("challenges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Stable ordinal used as an extra Docker label so the manager can look up
    # "the second container of challenge 42 for team foo" deterministically.
    container_index = db.Column(db.Integer, nullable=False, default=0)
    # Human-readable name shown in the UI (e.g. "Web server", "Database")
    label = db.Column(db.String(128), nullable=True)
    # Exactly one of the two image fields should be set.
    docker_image_filename = db.Column(db.String(512), nullable=True)
    docker_image_name = db.Column(db.String(512), nullable=True)
    # Legacy single-port column kept for DB backwards compatibility.
    container_port = db.Column(db.Integer, nullable=True, default=80)
    # JSON list of port mappings: [{"container_port": 80, "label": "HTTP"}, ...]
    # When present this takes precedence over the legacy container_port column.
    _port_mappings = db.Column("port_mappings", db.Text, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("challenge_id", "container_index", name="uq_challenge_container_index"),
    )

    @property
    def port_mappings(self):
        """Return port mappings as a Python list, falling back to legacy column."""
        import json as _json
        if self._port_mappings:
            try:
                return _json.loads(self._port_mappings)
            except Exception:
                pass
        if self.container_port:
            return [{"container_port": self.container_port, "label": ""}]
        return []

    @port_mappings.setter
    def port_mappings(self, value):
        import json as _json
        self._port_mappings = _json.dumps(value) if value is not None else None
        # Keep legacy column in sync with the first mapping for old code paths.
        if value:
            self.container_port = value[0].get("container_port")
        else:
            self.container_port = None


class DockerImageChallengeModel(Challenges):
    __mapper_args__ = {"polymorphic_identity": "docker"}

    id = db.Column(
        db.Integer,
        db.ForeignKey("challenges.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # The actual per-container configuration lives in DockerContainerConfig.
    # These columns are kept for backwards compatibility with existing DB rows
    # but new challenges should use the container config table instead.
    docker_image_filename = db.Column(db.String(512), nullable=True)
    docker_image_name = db.Column(db.String(512), nullable=True)
    docker_port = db.Column(db.Integer, nullable=True)
    # When True all containers for this challenge share a Docker bridge network
    # so they can reach each other by hostname.  Set to False for single-
    # container challenges that don't need inter-container communication.
    use_challenge_network = db.Column(db.Boolean, nullable=False, default=True, server_default="1")


# ---------------------------------------------------------------------------
# Challenge class
# ---------------------------------------------------------------------------

class DockerImageChallenge(BaseChallenge):
    id = "docker"
    name = "Docker Image"
    templates = {
        "create": f"/plugins/{PLUGIN_NAME}/assets/docker-challenge/create.html",
        "update": f"/plugins/{PLUGIN_NAME}/assets/docker-challenge/update.html",
        "view":   f"/plugins/{PLUGIN_NAME}/assets/docker-challenge/view.html",
    }
    scripts = {
        "create": f"/plugins/{PLUGIN_NAME}/assets/docker-challenge/create.js",
        "update": f"/plugins/{PLUGIN_NAME}/assets/docker-challenge/update.js",
        "view":   f"/plugins/{PLUGIN_NAME}/assets/docker-challenge/view.js",
    }
    challenge_model = DockerImageChallengeModel

    @classmethod
    def create(cls, request):
        """
        CTFd's BaseChallenge.create() does `cls.challenge_model(**request.form)`
        which blows up on our extra `docker_containers_json` field.
        We replicate the base logic here, stripping that field first.
        """
        import json as _json

        data = request.form.to_dict() if request.form else (request.get_json() or {})

        # Pull out our fields before they reach the SQLAlchemy constructor.
        raw_json = data.pop("docker_containers_json", "[]")
        use_network_raw = data.pop("use_challenge_network", None)

        challenge = cls.challenge_model(**data)
        # HTML checkboxes send "on" when checked and nothing when unchecked.
        challenge.use_challenge_network = use_network_raw not in (None, "", "false", "0", "off")
        db.session.add(challenge)
        db.session.commit()

        # Parse and persist the container configs.
        try:
            containers = _json.loads(raw_json) if raw_json else []
        except Exception:
            containers = []

        for cfg_data in containers:
            cfg = DockerContainerConfig(
                challenge_id=challenge.id,
                container_index=cfg_data.get("index", 0),
                label=cfg_data.get("label") or None,
                docker_image_filename=cfg_data.get("docker_image_filename") or None,
                docker_image_name=cfg_data.get("docker_image_name") or None,
            )
            cfg.port_mappings = cfg_data.get("port_mappings") or []
            db.session.add(cfg)

        db.session.commit()
        return challenge

    @classmethod
    def read(cls, challenge):
        data = super().read(challenge)

        # Include the full container config list.
        configs = _get_ordered_configs(challenge.id)
        data["containers"] = [_config_to_dict(c) for c in configs]

        # Backwards-compat fields
        data["docker_image_filename"] = challenge.docker_image_filename
        data["docker_image_name"] = challenge.docker_image_name
        data["docker_port"] = challenge.docker_port
        data["use_challenge_network"] = challenge.use_challenge_network

        # Live container status for each config entry
        data["docker_container_exists"] = False
        data["docker_containers"] = []
        try:
            actor = get_team_or_user()
            manager = current_app.docker_manager
            if actor and manager:
                statuses = _container_status_list(manager, actor.name, challenge.id, configs)
                data["docker_containers"] = statuses
                data["docker_container_exists"] = any(s["exists"] for s in statuses)
        except Exception:
            pass

        return data

    @classmethod
    def update(cls, challenge, request):
        import json as _json

        data = super().update(challenge, request)
        body = request.form or request.get_json() or {}

        # Backwards-compat single-image fields
        new_filename = body.get("docker_image_filename")
        if new_filename is not None:
            old_filename = challenge.docker_image_filename
            if old_filename and old_filename != new_filename:
                cls._delete_image_file(old_filename)
            challenge.docker_image_filename = new_filename

        if "docker_image_name" in body:
            challenge.docker_image_name = body["docker_image_name"] or None

        if "docker_port" in body:
            challenge.docker_port = _int_or_none(body["docker_port"])

        # When the multi-container JSON is submitted, treat missing
        # use_challenge_network as unchecked (HTML checkboxes omit the field).
        raw_containers = body.get("docker_containers_json")
        if raw_containers is not None:
            use_network_raw = body.get("use_challenge_network", None)
            challenge.use_challenge_network = use_network_raw not in (None, "", "false", "0", "off", False)

            try:
                containers_data = _json.loads(raw_containers) if raw_containers else []
            except Exception:
                containers_data = []

            existing = {cfg.container_index: cfg for cfg in _get_ordered_configs(challenge.id)}
            incoming_indices = set()

            for cfg_data in containers_data:
                idx = cfg_data.get("index", 0)
                incoming_indices.add(idx)

                if idx in existing:
                    cfg = existing[idx]
                    cfg.label = cfg_data.get("label") or None

                    new_fn = cfg_data.get("docker_image_filename") or None
                    if cfg.docker_image_filename and cfg.docker_image_filename != new_fn:
                        cls._delete_image_file(cfg.docker_image_filename)
                    cfg.docker_image_filename = new_fn
                    cfg.docker_image_name = cfg_data.get("docker_image_name") or None
                    cfg.port_mappings = cfg_data.get("port_mappings") or []
                else:
                    cfg = DockerContainerConfig(
                        challenge_id=challenge.id,
                        container_index=idx,
                        label=cfg_data.get("label") or None,
                        docker_image_filename=cfg_data.get("docker_image_filename") or None,
                        docker_image_name=cfg_data.get("docker_image_name") or None,
                    )
                    cfg.port_mappings = cfg_data.get("port_mappings") or []
                    db.session.add(cfg)

            # Delete configs that were removed in the UI
            for idx, cfg in existing.items():
                if idx not in incoming_indices:
                    if cfg.docker_image_filename:
                        cls._delete_image_file(cfg.docker_image_filename)
                    db.session.delete(cfg)
        elif "use_challenge_network" in body:
            v = body["use_challenge_network"]
            challenge.use_challenge_network = v not in (False, None, "", "false", "0", "off")

        db.session.commit()
        return data

    @classmethod
    def delete(cls, challenge):
        # Remove every container config and their uploaded image files.
        configs = _get_ordered_configs(challenge.id)
        for cfg in configs:
            if cfg.docker_image_filename:
                cls._delete_image_file(cfg.docker_image_filename)
            db.session.delete(cfg)

        # Also clean up the legacy single-image field if set.
        if challenge.docker_image_filename:
            cls._delete_image_file(challenge.docker_image_filename)

        return super().delete(challenge)

    @staticmethod
    def _delete_image_file(filename):
        if not filename:
            return
        path = os.path.join(get_docker_store_path(), filename)
        try:
            if os.path.exists(path):
                os.unlink(path)
                current_app.logger.info(f"[DockerImageChallenge] Deleted image: {filename}")
        except Exception as e:
            current_app.logger.warning(f"[DockerImageChallenge] Failed to delete {filename}: {e}")


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def load(app):
    app.register_blueprint(bp)
    app.config["MAX_CONTENT_LENGTH"] = MAX_IMAGE_SIZE
    with app.app_context():
        CHALLENGE_CLASSES["docker"] = DockerImageChallenge
        db.create_all()
    app.logger.info("✓ Docker Image Challenge plugin loaded")