
import os
import time
import tarfile
import uuid

from flask import Blueprint, abort, render_template, request, current_app, redirect, url_for, jsonify, send_from_directory, make_response
from werkzeug.utils import secure_filename
from CTFd.models import db, Challenges
from CTFd.utils.decorators import admins_only

from ..core.manager import DockerManager
from ..core.config import RuntimeConfig
from ..core.metrics import MetricsStore
from ..utils.config_sync import load_runtime_config, save_runtime_config, config_key
from .challenges import (
    MAX_IMAGE_SIZE,
    get_docker_store_path,
    _get_ordered_configs,
    _config_to_dict,
    _int_or_none,
    DockerContainerConfig,
    DockerImageChallenge,
)


def _challenge_names(ids) -> dict:
    """
    Return {challenge_id_str: name} for every ID in *ids*.

    Uses a single pass so we hit the DB at most once per unique challenge.
    Falls back to the raw ID string if a challenge is not found.
    """
    result = {}
    for raw in set(ids):
        try:
            ch = Challenges.query.get(int(raw))
            result[raw] = ch.name if ch else str(raw)
        except Exception:
            result[raw] = str(raw)
    return result

BASE_DIR = os.path.dirname(os.path.dirname(__file__))

admin_docker = Blueprint(
    "admin_docker_manager",
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "assets"),
)

def _cert_file(app=None) -> str:
    if app is not None:
        base = app.config.get("UPLOAD_FOLDER", "/var/uploads")
    else:
        try:
            base = current_app.config.get("UPLOAD_FOLDER", "/var/uploads")
        except RuntimeError:
            base = "/var/uploads"
    cert_dir = os.path.join(base, "docker_registry")
    os.makedirs(cert_dir, exist_ok=True)
    return os.path.join(cert_dir, "ca.crt")


def _materialize_registry_cert(app):
    """
    Re-create the registry CA cert file on disk from its DB-backed copy.

    The upload folder is wiped on every `docker run --build`, but the CTFd
    config DB persists. We stash the cert bytes alongside the path so we
    can rehydrate the file at startup without forcing the admin to re-upload.
    """
    from CTFd.utils import get_config
    data = get_config("docker_registry_cert_data")
    if not data:
        return
    try:
        path = _cert_file(app=app)
        if not os.path.isfile(path):
            with open(path, "wb") as out:
                out.write(data.encode("utf-8") if isinstance(data, str) else data)
            app.logger.info(f"[DockerManager] Restored registry cert to {path} from DB")
        if RuntimeConfig.REGISTRY_CERT_PATH != path:
            from CTFd.utils import set_config as ctfd_set_config
            ctfd_set_config("docker_registry_cert_path", path)
            RuntimeConfig.REGISTRY_CERT_PATH = path
    except Exception as e:
        app.logger.error(f"[DockerManager] Failed to restore registry cert: {e}")
 
 
@admin_docker.route("/admin/docker/registry/cert", methods=["POST"])
@admins_only
def upload_registry_cert():
    """
    Accept a PEM/CRT file upload, save it to CERT_FILE, and persist the
    path in RuntimeConfig so RegistryManager picks it up immediately
    (no restart required).
    """
    if "cert" not in request.files:
        return jsonify({"success": False, "error": "No file provided"}), 400
 
    f = request.files["cert"]
    if not f.filename:
        return jsonify({"success": False, "error": "Empty filename"}), 400
 
    # Basic sanity — must look like a PEM cert
    cert_bytes = f.read()
    if b"-----BEGIN CERTIFICATE-----" not in cert_bytes:
        return jsonify({"success": False, "error": "File does not appear to be a PEM certificate"}), 400
 
    cert_file = _cert_file()
    try:
        with open(cert_file, "wb") as out:
            out.write(cert_bytes)
    except Exception as e:
        current_app.logger.error(f"[DockerManager] Failed to save registry cert: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
 
    # Persist path AND content in config so we can rehydrate the file
    # after a container rebuild wipes /var/uploads.
    _set_config("REGISTRY_CERT_PATH", cert_file)
    _set_config("REGISTRY_CERT_DATA", cert_bytes.decode("utf-8", errors="replace"))
    RuntimeConfig.REGISTRY_CERT_PATH = cert_file
 
    current_app.logger.info(f"[DockerManager] Registry cert saved to {cert_file}")
    return jsonify({"success": True, "path": cert_file})
 
 
@admin_docker.route("/admin/docker/registry/cert/delete", methods=["POST"])
@admins_only
def delete_registry_cert():
    """Remove the stored registry cert and clear the config key."""
    path = getattr(RuntimeConfig, "REGISTRY_CERT_PATH", None) or _cert_file()
    if path and os.path.isfile(path):
        try:
            os.unlink(path)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
 
    _set_config("REGISTRY_CERT_PATH", "")
    _set_config("REGISTRY_CERT_DATA", "")
    RuntimeConfig.REGISTRY_CERT_PATH = None

    return jsonify({"success": True})
 
 
def _set_config(key: str, value: str):
    """
    Persist a config value the same way your existing save_config endpoint does.
    Replace this with however your plugin writes to the CTFd config store.
    """
    from CTFd.models import db
    from CTFd.utils import set_config as ctfd_set_config
    ctfd_set_config(f"docker_{key.lower()}", value)

@admin_docker.route("/admin/docker_manager", methods=["GET"])
@admins_only
def docker_manager_admin():

    load_runtime_config()
    values = {attr: getattr(RuntimeConfig, attr) for attr in load_runtime_config.__globals__['RUNTIME_ATTRS']}
    return render_template("admin_docker_manager.html", values=values, keyfn=config_key)



@admin_docker.route("/admin/docker_manager/save", methods=["POST"])
@admins_only
def save_config():
    data = request.get_json()
    save_runtime_config(data)

    old_store = getattr(current_app, "metrics_store", None)
    if old_store is not None:
        try:
            old_store.stop()
        except Exception:
            pass

    dm = DockerManager(RuntimeConfig.WORKER_NODES)
    current_app.docker_manager = dm
    dm.update_nginx_data()
    dm.delete_all()
    dm.print_nodes_table()
    dm.update_nodes_details()

    current_app.metrics_store = MetricsStore()
    current_app.metrics_store.start(dm.nodes)

    return {"success": True}



@admin_docker.route("/admin/docker_manager/nodes")
@admins_only
def nodes_dashboard():
    dm = current_app.docker_manager
    dm.update_nodes_details()

    # Resolve raw challenge IDs (Docker labels) to human-readable names
    all_ids = {c.challenge for node in dm.nodes for c in node.containers}
    names = _challenge_names(all_ids)
    for node in dm.nodes:
        for c in node.containers:
            c.challenge = names.get(c.challenge, c.challenge)

    for node in dm.nodes:
        print(f"Node: {node.name} ({node.address}) - Status: {node.status}")
        for c in node.containers:
            print(f"  Container: {c.challenge} ({c.team}) - Status: {c.status}")
    return render_template("admin_nodes.html", nodes=dm.nodes)




@admin_docker.route("/admin/container/delete", methods=["POST"])
@admins_only
def delete_container():
    token = request.get_json().get("token")

    if not token:
        abort(400)

    current_app.docker_manager.remove_container(token)

    store = getattr(current_app, "metrics_store", None)
    if store:
        store.log_event("warning", f"Admin deleted container: token {token}")

    return redirect(url_for("admin_docker_manager.nodes_dashboard"))


@admins_only
@admin_docker.route("/admin/container/suspend", methods=["POST"])
def suspend_container():
    token = request.get_json().get("token")
    current_app.docker_manager.suspend_container(token)

    store = getattr(current_app, "metrics_store", None)
    if store:
        store.log_event("info", f"Admin suspended container: token {token}")

    return {"success": True}


@admins_only
@admin_docker.route("/admin/container/resume", methods=["POST"])
def resume_container():
    token = request.get_json().get("token")
    current_app.docker_manager.resume_container(token)

    store = getattr(current_app, "metrics_store", None)
    if store:
        store.log_event("info", f"Admin resumed container: token {token}")

    return {"success": True}


# ------------------------------------------------------------------ #
# Monitoring dashboard + API                                           #
# ------------------------------------------------------------------ #

@admin_docker.route("/admin/docker_manager/monitoring", methods=["GET"])
@admins_only
def monitoring_dashboard():
    return render_template("admin_monitoring.html", domain=RuntimeConfig.CTFD_DOMAIN_NAME)


@admin_docker.route("/admin/docker_manager/api/metrics", methods=["GET"])
@admins_only
def api_current_metrics():
    """Return the latest metrics snapshot plus recent activity events."""
    store = getattr(current_app, "metrics_store", None)
    if store is None:
        return jsonify({"error": "Metrics store not available"}), 503

    snap   = store.latest()
    events = store.recent_events(100)

    base = snap.to_dict() if snap else {
        "timestamp":  time.time(),
        "nodes":      [],
        "containers": [],
    }

    # Resolve raw challenge IDs to names before sending to the browser
    container_list = base.get("containers", [])
    names = _challenge_names({c["challenge"] for c in container_list})
    for c in container_list:
        c["challenge"] = names.get(c["challenge"], c["challenge"])

    base["events"] = [e.to_dict() for e in events]
    return jsonify(base)


@admin_docker.route("/admin/docker_manager/api/metrics/history", methods=["GET"])
@admins_only
def api_metrics_history():
    """
    Return per-node time-series data for graphing.

    Response shape:
        {
          "nodes": {
            "<address>": {
              "name":          str,
              "labels":        [str, ...],   // HH:MM:SS
              "used_mem_mb":   [float, ...],
              "free_mem_mb":   [float, ...],
              "running_count": [int, ...],
            }
          }
        }
    """
    store = getattr(current_app, "metrics_store", None)
    if store is None:
        return jsonify({"error": "Metrics store not available"}), 503

    nodes_ts: dict = {}
    for snap in store.history():
        label = time.strftime("%H:%M:%S", time.localtime(snap.timestamp))
        for node in snap.nodes:
            if node.address not in nodes_ts:
                nodes_ts[node.address] = {
                    "name":              node.name,
                    "labels":            [],
                    "used_mem_mb":       [],
                    "free_mem_mb":       [],
                    "running_count":     [],
                    "cpu_total_percent": [],
                }
            ts = nodes_ts[node.address]
            ts["labels"].append(label)
            ts["used_mem_mb"].append(node.used_mem_mb)
            ts["free_mem_mb"].append(node.free_mem_mb)
            ts["running_count"].append(node.running_count)
            ts["cpu_total_percent"].append(node.cpu_total_percent)

    return jsonify({"nodes": nodes_ts})



# ---------------------------------------------------------------------------
# Admin API — image upload
# ---------------------------------------------------------------------------

@admin_docker.route("/admin/docker/upload", methods=["POST"])
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

@admin_docker.route("/admin/docker/challenge/<int:challenge_id>/containers", methods=["GET"])
@admins_only
def admin_list_containers(challenge_id):
    configs = _get_ordered_configs(challenge_id)
    return jsonify({
        "success": True,
        "containers": [_config_to_dict(c) for c in configs],
    })


@admin_docker.route("/admin/docker/challenge/<int:challenge_id>/containers", methods=["POST"])
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


@admin_docker.route("/admin/docker/challenge/<int:challenge_id>/containers/<int:config_id>", methods=["PATCH"])
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


@admin_docker.route("/admin/docker/challenge/<int:challenge_id>/containers/<int:config_id>", methods=["DELETE"])
@admins_only
def admin_delete_container(challenge_id, config_id):
    cfg = DockerContainerConfig.query.filter_by(id=config_id, challenge_id=challenge_id).first_or_404()
    if cfg.docker_image_filename:
        DockerImageChallenge._delete_image_file(cfg.docker_image_filename)
    db.session.delete(cfg)
    db.session.commit()
    return jsonify({"success": True})


@admin_docker.route("/uploads/docker_images/<path:filename>")
@admins_only
def serve_docker_image(filename):
    return send_from_directory(get_docker_store_path(), filename)


@admin_docker.route("/admin/docker/images", methods=["GET"])
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


def load(app):
    app.register_blueprint(admin_docker)
    load_runtime_config()
    _materialize_registry_cert(app)

    old_store = getattr(app, "metrics_store", None)
    if old_store is not None:
        try:
            old_store.stop()
        except Exception:
            pass

    app.docker_manager  = DockerManager(RuntimeConfig.WORKER_NODES)
    app.metrics_store   = MetricsStore()

    try:
        app.docker_manager.delete_all()
        app.docker_manager.update_nginx_data()
        app.docker_manager.print_nodes_table()
        app.docker_manager.update_nodes_details()
        app.metrics_store.start(app.docker_manager.nodes)
    except Exception:
        app.docker_manager = None



def unload(app):
    return None
