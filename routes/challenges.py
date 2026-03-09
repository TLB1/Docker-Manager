from flask import Blueprint, request, jsonify, send_from_directory, current_app
from werkzeug.utils import secure_filename
import os
import uuid
import tarfile

from CTFd.plugins import register_plugin_assets_directory
from CTFd.plugins.challenges import BaseChallenge, CHALLENGE_CLASSES
from CTFd.models import db, Challenges
from CTFd.utils.decorators import admins_only
from CTFd.utils import uploads
from CTFd.utils.user import get_current_team, get_current_user
from ..core.labels import DockerLabels

PLUGIN_NAME = "my-plugin"
MAX_IMAGE_SIZE = 5 * 1024 * 1024 * 1024  # 5 GB - change as needed

bp = Blueprint(
    "docker_image_challenge",
    __name__,
    template_folder="templates"
)



def get_docker_store_path():
    """Path inside CTFd's uploads directory for Docker .tar files"""
    path = os.path.join(uploads.uploads_path(), "docker_images")
    os.makedirs(path, exist_ok=True)
    return path



@bp.route("/docker/api/challenge/<int:challenge_id>/status", methods=["GET"])
def api_docker_status(challenge_id):
    """Return container existence/status/url for the current team on this challenge."""
    team = get_current_team()
    if not team:
        user = get_current_user()
        if not user:
            return jsonify({"success": False, "error": "You must be logged in"}), 403
        team = user

    manager = current_app.docker_manager
    if not manager:
        return jsonify({"success": False, "error": "Docker manager not configured"}), 500

    container = manager.get_container_for_team_challenge(team.id, challenge_id)
    if not container:
        return jsonify({"success": True, "exists": False})

    token = container.labels.get(DockerLabels.TOKEN)
    status = container.status
    url = f"http://{token}.challenges.ctf:8008/"  # adapt if you use a different URL scheme
    return jsonify({"success": True, "exists": True, "status": status, "url": url, "token": token})



@bp.route("/docker/api/challenge/<int:challenge_id>/start", methods=["POST"])
def api_docker_start(challenge_id):
    """Start a new container for the user's team for this challenge."""
    team = get_current_team()
    if not team:
        user = get_current_user()
        if not user:
            return jsonify({"success": False, "error": "You must be logged in"}), 403
        team = user

    manager = current_app.docker_manager
    if not manager:
        return jsonify({"success": False, "error": "Docker manager not configured"}), 500

    # Resolve image: prefer a stored tar filename, otherwise use challenge.extra.docker_image
    challenge = Challenges.query.get(challenge_id)
    if not challenge:
        return jsonify({"success": False, "error": "Challenge not found"}), 404

    image = None
    if getattr(challenge, "docker_image_filename", None):
        tar_name = os.path.join(get_docker_store_path(), challenge.docker_image_filename)
        image = manager._get_image_from_tar(tar_name)
        try:
            manager.sync_tar_image(tar_name)
        except Exception as e:
            current_app.logger.warning(f"[DockerImageChallenge] Image sync warning: {e}")
    elif getattr(challenge, "docker_image_name", None):
        image = challenge.docker_image_name
    else:
        return jsonify({"success": False, "error": "No docker image configured for this challenge"}), 400

    container_port = getattr(challenge, "docker_port", None) or 80

    try:
        token = manager.create_container(team.name, challenge_id, image, container_port=container_port)
        # build URL
        url = f"http://{token}.challenges.ctf:8008/"
        return jsonify({"success": True, "token": token, "url": url})
    except Exception as e:
        current_app.logger.error(f"[DockerImageChallenge] Failed to start container: {e}")
        return jsonify({"success": False, "error": str(e)}), 500



@bp.route("/docker/api/challenge/<int:challenge_id>/resume", methods=["POST"])
def api_docker_resume(challenge_id):
    """Resume an existing container for the user's team."""
    team = get_current_team()
    if not team:
        user = get_current_user()
        if not user:
            return jsonify({"success": False, "error": "You must be logged in"}), 403
        team = user

    manager = current_app.docker_manager
    if not manager:
        return jsonify({"success": False, "error": "Docker manager not configured"}), 500

    container = manager.get_container_for_team_challenge(team.id, challenge_id)
    if not container:
        return jsonify({"success": False, "error": "No container exists to resume"}), 404

    token = container.labels.get(DockerLabels.TOKEN)
    try:
        ok = manager.resume_container(token)
        return jsonify({"success": True, "resumed": ok, "token": token})
    except Exception as e:
        current_app.logger.error(f"[DockerImageChallenge] Failed to resume container: {e}")
        return jsonify({"success": False, "error": str(e)}), 500



@bp.route("/admin/docker/upload", methods=["POST"])## TODO change this in js
@admins_only
def upload_docker_image():
    """Called by create.js / update.js via AJAX"""
    if "image_tar" not in request.files:
        return jsonify({"success": False, "error": "No file part"}), 400

    f = request.files["image_tar"]
    if not f.filename:
        return jsonify({"success": False, "error": "No file selected"}), 400

    if not f.filename.lower().endswith(".tar"):
        return jsonify({"success": False, "error": "Only .tar files are allowed"}), 400

    # Size check (memory-efficient)
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    if size > MAX_IMAGE_SIZE or size == 0:
        return jsonify({"success": False, "error": f"File too large (max {MAX_IMAGE_SIZE//(1024**3)} GB)"}), 413

    filename = secure_filename(f.filename)
    unique_filename = f"{uuid.uuid4().hex}_{filename}"
    store = get_docker_store_path()
    save_path = os.path.join(store, unique_filename)

    try:
        f.save(save_path)

        # Strict tar validation
        if not tarfile.is_tarfile(save_path):
            os.unlink(save_path)
            return jsonify({"success": False, "error": "Not a valid tar archive"}), 400

        current_app.logger.info(
            f"[DockerImageChallenge] Uploaded {unique_filename} ({size / 1024 / 1024:.1f} MB)"
        )
        return jsonify({"success": True, "filename": unique_filename})

    except Exception as e:
        current_app.logger.error(f"[DockerImageChallenge] Upload failed: {e}")
        if os.path.exists(save_path):
            os.unlink(save_path)
        return jsonify({"success": False, "error": "Upload failed"}), 500



@bp.route("/uploads/docker_images/<path:filename>")
@admins_only
def serve_docker_image(filename):
    """Serve uploaded images (admin-only by default)"""
    return send_from_directory(get_docker_store_path(), filename)



class DockerImageChallengeModel(Challenges):
    __mapper_args__ = {"polymorphic_identity": "docker_image"}
    id = db.Column(
        db.Integer,
        db.ForeignKey("challenges.id", ondelete="CASCADE"),
        primary_key=True,
    )
    docker_image_filename = db.Column(db.String(512), nullable=True)
    docker_image_name = db.Column(db.String(512), nullable=True)
    docker_port = db.Column(db.Integer, nullable=True)



class DockerImageChallenge(BaseChallenge):
    id = "docker_image"
    name = "Docker Image"
    templates = {
        "create": f"/plugins/{PLUGIN_NAME}/assets/docker-challenge/create.html",
        "update": f"/plugins/{PLUGIN_NAME}/assets/docker-challenge/update.html",
        "view": f"/plugins/{PLUGIN_NAME}/assets/docker-challenge/view.html",
    }
    scripts = {
        "create": f"/plugins/{PLUGIN_NAME}/assets/docker-challenge/create.js",
        "update": f"/plugins/{PLUGIN_NAME}/assets/docker-challenge/update.js",
        "view": f"/plugins/{PLUGIN_NAME}/assets/docker-challenge/view.js",
    }

    challenge_model = DockerImageChallengeModel

    @classmethod
    def read(cls, challenge):
        data = super().read(challenge)
        data["docker_image_filename"] = getattr(challenge, "docker_image_filename", None)
        data["docker_image_name"] = getattr(challenge, "docker_image_name", None)
        data["docker_port"] = getattr(challenge, "docker_port", None)

        # Add per-team container info for the view
        try:
            from CTFd.utils.user import get_current_team
            team = get_current_team()
            manager = current_app.docker_manager
            if team and manager:
                container = manager.get_container_for_team_challenge(team.id, challenge.id)
                if container:
                    token = container.labels.get(DockerLabels.TOKEN)
                    status = container.status
                    url = f"http://{token}.challenges.ctf:8008/"
                    data["docker_container_exists"] = True
                    data["docker_container_status"] = status
                    data["docker_container_url"] = url
                else:
                    data["docker_container_exists"] = False
                    data["docker_container_status"] = None
                    data["docker_container_url"] = None
            else:
                # no team or no manager -> hide buttons
                data["docker_container_exists"] = False
                data["docker_container_status"] = None
                data["docker_container_url"] = None
        except Exception:
            data["docker_container_exists"] = False
            data["docker_container_status"] = None
            data["docker_container_url"] = None

        return data



    @classmethod
    def update(cls, challenge, request):
        data = super().update(challenge, request)

        body = request.form or request.get_json() or {}

        new_filename = body.get("docker_image_filename")
        if new_filename is not None:
            old_filename = getattr(challenge, "docker_image_filename", None)
            challenge.docker_image_filename = new_filename
            if old_filename and old_filename != new_filename:
                cls._delete_image_file(old_filename)

        if "docker_image_name" in body:
            challenge.docker_image_name = body.get("docker_image_name")

        if "docker_port" in body:
            try:
                challenge.docker_port = int(body.get("docker_port"))
            except (TypeError, ValueError):
                challenge.docker_port = None

        db.session.commit()
        return data



    @classmethod
    def delete(cls, challenge):
        """Clean up the Docker image file when the challenge is deleted"""
        filename = getattr(challenge, "docker_image_filename", None)
        if filename:
            cls._delete_image_file(filename)
        return super().delete(challenge)



    @staticmethod
    def _delete_image_file(filename):
        if not filename:
            return
        try:
            path = os.path.join(get_docker_store_path(), filename)
            if os.path.exists(path):
                os.unlink(path)
                current_app.logger.info(f"[DockerImageChallenge] Deleted image: {filename}")
        except Exception as e:
            current_app.logger.warning(f"[DockerImageChallenge] Failed to delete {filename}: {e}")



def load(app):

    app.register_blueprint(bp)

    with app.app_context():
        CHALLENGE_CLASSES["docker_image"] = DockerImageChallenge
        db.create_all()

    current_app.logger.info("✓ Docker Image Challenge plugin loaded successfully")