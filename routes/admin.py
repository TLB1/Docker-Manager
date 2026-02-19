
from flask import Blueprint, render_template, request, current_app, redirect, url_for
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

    dm = DockerManager(RuntimeConfig.WORKER_NODES)
    current_app.docker_manager = dm

    dm.delete_all()
    dm.create_container("test1_nginx", "test1_nginx", "nginx")
    dm.create_container("test1_nginx", "test1_nginx", "nginx")
    dm.print_nodes_table()

    return {"success": True}



@admin_docker.route("/admin/docker_manager/nodes")
@admins_only
def nodes_dashboard():
    dm = current_app.docker_manager
    dm.update_nodes_details()
    return render_template("admin_nodes.html", nodes=dm.nodes)




def load(app):
    app.register_blueprint(admin_docker)
    load_runtime_config()

    app.docker_manager = DockerManager(RuntimeConfig.WORKER_NODES)

    try:
        app.docker_manager.delete_all()
        app.docker_manager.create_container("test1_nginx", "test1_nginx", "nginx")
        app.docker_manager.create_container("test1_nginx", "test1_nginx", "nginx")
        app.docker_manager.print_nodes_table()
        app.docker_manager.update_nodes_details()
    except Exception:
        app.docker_manager = None



def unload(app):
    return None
