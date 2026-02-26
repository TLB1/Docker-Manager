
from flask import Blueprint, abort, render_template, request, current_app, redirect, url_for
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
        app.docker_manager.create_container("Web Devs", "Nginx Challenge", "nginx")
        app.docker_manager.create_container("I love CSS", "Nginx Challenge", "nginx")
        app.docker_manager.create_container("Web Devs", "httpd Challenge", "httpd:trixie")
        app.docker_manager.create_container("I love CSS", "httpd Challenge", "httpd:trixie")
        app.docker_manager.create_container("Programmers", "hello-world Challenge", "hello-world:latest")
        app.docker_manager.create_container("Programmers", "Nginx Challenge", "nginx")
        app.docker_manager.create_container("Programmers", "httpd Challenge", "httpd:trixie")
        app.docker_manager.print_nodes_table()
        app.docker_manager.update_nodes_details()
    except Exception:
        app.docker_manager = None



def unload(app):
    return None
