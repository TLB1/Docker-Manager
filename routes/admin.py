from pathlib import Path
from flask import Blueprint, render_template, request, redirect, url_for
import paramiko
from CTFd.utils import get_config
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

    manager = DockerManager(RuntimeConfig.WORKER_NODES)
    manager.delete_all()
    manager.create_container("test1_nginx", "test1_nginx", "nginx")
    manager.create_container("test1_nginx", "test1_nginx", "nginx")
    manager.print_nodes_table()
    return {"success": True}



def load(app):
    app.register_blueprint(admin_docker)
    load_runtime_config()

    try:
        base_urls_raw = get_config("docker_base_urls", default="")
        base_urls = [u.strip() for u in base_urls_raw.split(",") if u.strip()] if base_urls_raw else None
        app.docker_manager = DockerManager(base_urls=base_urls)
    except Exception:
        app.docker_manager = None




def unload(app):
    return None
