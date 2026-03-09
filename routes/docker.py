from flask import Blueprint, jsonify, current_app
from CTFd.utils.user import get_current_user
from CTFd.utils.decorators import authed_only
from CTFd.models import Challenges

docker_api = Blueprint("docker_api", __name__)


def _team_id():

    user = get_current_user()
    if not user:
        return None

    # CTFd teams mode vs users mode
    if getattr(user, "team_id", None):
        return user.team_id
    return user.id


def _challenge_image(challenge_id):
    chal = Challenges.query.get(challenge_id)
    if not chal:
        return None

    # adjust if you store image differently
    return chal.connection_info or chal.description or "nginx"


def _container_url(container):
    token = container.labels.get("token") or container.labels.get("ctfd.token")
    if not token:
        return None
    return f"http://{token}.challenges.ctf:8008/"


# ---------------- STATUS ----------------

@docker_api.route("/docker/api/challenge/<int:challenge_id>/status")
@authed_only
def docker_status(challenge_id):
    team_id = _team_id()
    if not team_id:
        return jsonify(success=False, error="Not authenticated")

    manager = current_app.docker_manager

    container = manager.get_container_for_team_challenge(team_id, challenge_id)

    if not container:
        return jsonify(success=True, exists=False)

    url = _container_url(container)

    return jsonify(
        success=True,
        exists=True,
        status=container.status,
        url=url
    )


# ---------------- START ----------------

@docker_api.route("/docker/api/challenge/<int:challenge_id>/start", methods=["POST"])
@authed_only
def docker_start(challenge_id):
    team_id = _team_id()
    manager = current_app.docker_manager

    if manager.get_container_for_team_challenge(team_id, challenge_id):
        return jsonify(success=False, error="Container already exists")

    image = _challenge_image(challenge_id)
    if not image:
        return jsonify(success=False, error="Challenge image not configured")

    try:
        token = manager.create_container(team_id, challenge_id, image)
        return jsonify(success=True, token=token)
    except Exception as e:
        return jsonify(success=False, error=str(e))


# ---------------- RESUME ----------------

@docker_api.route("/docker/api/challenge/<int:challenge_id>/resume", methods=["POST"])
@authed_only
def docker_resume(challenge_id):
    team_id = _team_id()
    manager = current_app.docker_manager

    container = manager.get_container_for_team_challenge(team_id, challenge_id)
    if not container:
        return jsonify(success=False, error="No container")

    token = container.labels.get("token") or container.labels.get("ctfd.token")

    ok = manager.resume_container(token)
    if not ok:
        return jsonify(success=False, error="Resume failed")

    return jsonify(success=True)



def load(app):
    print("Docker Manager plugin loaded")
    #app.register_blueprint(docker_api)