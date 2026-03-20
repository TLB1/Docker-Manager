
from flask import Blueprint, abort, render_template, request, current_app, redirect, url_for, jsonify
from CTFd.utils.decorators import admins_only

from ..core.manager import DockerManager
from ..core.config import RuntimeConfig
from ..utils.config_sync import load_runtime_config, save_runtime_config, config_key
import os

BASE_DIR = os.path.dirname(os.path.dirname(__file__))

admin_docker = Blueprint(
    "admin_docker_manager",
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "assets"),
)

def _cert_file() -> str:
    from CTFd.utils.uploads import get_uploader
    try:
        # Prefer CTFd's configured upload path
        base = current_app.config.get("UPLOAD_FOLDER", "/var/uploads")
    except RuntimeError:
        base = "/var/uploads"
    cert_dir = os.path.join(base, "docker_registry")
    os.makedirs(cert_dir, exist_ok=True)
    return os.path.join(cert_dir, "ca.crt")
 
 
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
 
    # Persist path in config (updates the live RuntimeConfig + DB key)
    _set_config("REGISTRY_CERT_PATH", cert_file)
    RuntimeConfig.REGISTRY_CERT_PATH = cert_file
 
    current_app.logger.info(f"[DockerManager] Registry cert saved to {cert_file}")
    return jsonify({"success": True, "path": cert_file})
 
 
@admin_docker.route("/admin/docker/registry/cert/delete", methods=["POST"])
@admins_only
def delete_registry_cert():
    """Remove the stored registry cert and clear the config key."""
    path = getattr(RuntimeConfig, "REGISTRY_CERT_PATH", None) or _get_cert_path()
    if path and os.path.isfile(path):
        try:
            os.unlink(path)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
 
    _set_config("REGISTRY_CERT_PATH", "")
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

    dm = DockerManager(RuntimeConfig.WORKER_NODES)
    current_app.docker_manager = dm

    dm.delete_all()
    dm.print_nodes_table()

    return {"success": True}



@admin_docker.route("/admin/docker_manager/nodes")
@admins_only
def nodes_dashboard():
    dm = current_app.docker_manager
    dm.update_nodes_details()
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

    return redirect(url_for("admin_docker_manager.nodes_dashboard")) 



@admins_only
@admin_docker.route("/admin/container/suspend", methods=["POST"])
def suspend_container():
    token = request.get_json().get("token")
    current_app.docker_manager.suspend_container(token)
    return {"success": True}


@admins_only
@admin_docker.route("/admin/container/resume", methods=["POST"])
def resume_container():
    token = request.get_json().get("token")
    current_app.docker_manager.resume_container(token)
    return {"success": True}



def load(app):
    app.register_blueprint(admin_docker)
    load_runtime_config()

    app.docker_manager = DockerManager(RuntimeConfig.WORKER_NODES)

    try:
        app.docker_manager.delete_all()
        app.docker_manager.print_nodes_table()
        app.docker_manager.update_nodes_details()
    except Exception:
        app.docker_manager = None



def unload(app):
    return None
