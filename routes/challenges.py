from pathlib import Path

from flask import Blueprint, request, jsonify, send_from_directory, current_app, render_template_string
from flask.templating import render_template
from werkzeug.utils import secure_filename
import os
import uuid
import tarfile

from CTFd.plugins.challenges import BaseChallenge, CHALLENGE_CLASSES
from CTFd.models import db, Challenges
from CTFd.utils.decorators import admins_only
from CTFd.utils.user import get_current_team, get_current_user
from ..core.labels import DockerLabels

PLUGIN_NAME = "my-plugin"
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

    container = manager.get_container_for_team_challenge(actor.name, challenge_id)
    if not container:
        return jsonify({"success": True, "exists": False})

    token = container.labels.get(DockerLabels.TOKEN)
    url = f"http://{token}.challenges.ctf:8008/"
    return jsonify({
        "success": True,
        "exists": True,
        "status": container.status,
        "url": url,
        "token": token,
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

    image = None
    if challenge.docker_image_filename:
        tar_path = os.path.join(get_docker_store_path(), challenge.docker_image_filename)
        image = manager._get_image_from_tar(tar_path)
        try:
            manager.sync_tar_image(tar_path)
        except Exception as e:
            current_app.logger.warning(f"[DockerImageChallenge] Image sync warning: {e}")
    elif challenge.docker_image_name:
        image = challenge.docker_image_name
    else:
        return jsonify({"success": False, "error": "No docker image configured for this challenge"}), 400

    container_port = challenge.docker_port or 80

    try:
        token = manager.create_container(actor.name, challenge_id, image, container_port=container_port)
        url = f"http://{token}.challenges.ctf:8008/"
        return jsonify({"success": True, "token": token, "url": url})
    except Exception as e:
        current_app.logger.error(f"[DockerImageChallenge] Failed to start container: {e}")
        return jsonify({"success": False, "error": str(e)}), 500



@bp.route("/docker/api/challenge/<int:challenge_id>/resume", methods=["POST"])
def api_docker_resume(challenge_id):
    actor = get_team_or_user()
    if not actor:
        return jsonify({"success": False, "error": "You must be logged in"}), 403

    manager = current_app.docker_manager
    if not manager:
        return jsonify({"success": False, "error": "Docker manager not configured"}), 500

    container = manager.get_container_for_team_challenge(actor.name, challenge_id)
    if not container:
        return jsonify({"success": False, "error": "No container exists to resume"}), 404

    token = container.labels.get(DockerLabels.TOKEN)
    try:
        ok = manager.resume_container(token)
        return jsonify({"success": True, "resumed": ok, "token": token})
    except Exception as e:
        current_app.logger.error(f"[DockerImageChallenge] Failed to resume container: {e}")
        return jsonify({"success": False, "error": str(e)}), 500



@bp.route("/docker/api/token/<token>/status", methods=["GET"])
def api_token_status(token):
    """Return container status by token only — no auth required (token is the secret)."""
    manager = current_app.docker_manager
    if not manager:
        return jsonify({"success": False, "error": "Docker manager not configured"}), 500

    container = manager.get_container_by_token(token)
    if not container:
        return jsonify({"success": True, "exists": False})

    url = f"http://{token}.challenges.ctf:8008/"
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
        url = f"http://{token}.challenges.ctf:8008/"
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

    container = manager.get_container_for_team_challenge(actor.name, challenge_id)
    if not container:
        return jsonify({"success": False, "error": "No container found"}), 404

    token = container.labels.get(DockerLabels.TOKEN)
    try:
        ok = manager.remove_container(token)  # was: suspend_container
        return jsonify({"success": True, "stopped": ok})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



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

    # Remove existing container if present
    container = manager.get_container_for_team_challenge(actor.name, challenge_id)
    if container:
        token = container.labels.get(DockerLabels.TOKEN)
        manager.remove_container(token)

    # Resolve image
    image = None
    if challenge.docker_image_filename:
        tar_path = os.path.join(get_docker_store_path(), challenge.docker_image_filename)
        image = manager._get_image_from_tar(tar_path)
    elif challenge.docker_image_name:
        image = challenge.docker_image_name
    else:
        return jsonify({"success": False, "error": "No docker image configured"}), 400

    container_port = challenge.docker_port or 80

    try:
        token = manager.create_container(actor.name, challenge_id, image, container_port=container_port)
        url = f"http://{token}.challenges.ctf:8008/"
        return jsonify({"success": True, "token": token, "url": url})
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



@bp.route("/challenge-unavailable/<token>")
def challenge_unavailable(token):
    ctfd_root = current_app.config.get("APPLICATION_ROOT", "").rstrip("/")
    return render_template(
        "challenge_unavailable.html",
        token=token,
        ctfd_root=ctfd_root,
    )


# ---------------------------------------------------------------------------
# Admin API
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


@bp.route("/uploads/docker_images/<path:filename>")
@admins_only
def serve_docker_image(filename):
    return send_from_directory(get_docker_store_path(), filename)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class DockerImageChallengeModel(Challenges):
    __mapper_args__ = {"polymorphic_identity": "docker_image"}

    id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), primary_key=True)
    docker_image_filename = db.Column(db.String(512), nullable=True)
    docker_image_name = db.Column(db.String(512), nullable=True)
    docker_port = db.Column(db.Integer, nullable=True)


# ---------------------------------------------------------------------------
# Challenge class
# ---------------------------------------------------------------------------

class DockerImageChallenge(BaseChallenge):
    id = "docker_image"
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
    def read(cls, challenge):
        data = super().read(challenge)
        data["docker_image_filename"] = challenge.docker_image_filename
        data["docker_image_name"] = challenge.docker_image_name
        data["docker_port"] = challenge.docker_port

        data["docker_container_exists"] = False
        data["docker_container_status"] = None
        data["docker_container_url"] = None
        try:
            actor = get_team_or_user()
            manager = current_app.docker_manager
            if actor and manager:
                container = manager.get_container_for_team_challenge(actor.id, challenge.id)
                if container:
                    token = container.labels.get(DockerLabels.TOKEN)
                    data["docker_container_exists"] = True
                    data["docker_container_status"] = container.status
                    data["docker_container_url"] = f"http://{token}.challenges.ctf:8008/"
        except Exception:
            pass

        return data

    @classmethod
    def update(cls, challenge, request):
        data = super().update(challenge, request)
        body = request.form or request.get_json() or {}

        new_filename = body.get("docker_image_filename")
        if new_filename is not None:
            old_filename = challenge.docker_image_filename
            if old_filename and old_filename != new_filename:
                cls._delete_image_file(old_filename)
            challenge.docker_image_filename = new_filename

        if "docker_image_name" in body:
            challenge.docker_image_name = body["docker_image_name"] or None

        if "docker_port" in body:
            try:
                challenge.docker_port = int(body["docker_port"])
            except (TypeError, ValueError):
                challenge.docker_port = None

        db.session.commit()
        return data

    @classmethod
    def delete(cls, challenge):
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
        CHALLENGE_CLASSES["docker_image"] = DockerImageChallenge
        db.create_all()
    app.logger.info("✓ Docker Image Challenge plugin loaded")
