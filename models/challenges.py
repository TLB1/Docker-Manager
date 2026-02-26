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


@bp.route("/admin/upload", methods=["POST"])
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
        return data

    @classmethod
    def update(cls, challenge, request):
        """Handle image change + old file cleanup"""
        data = super().update(challenge, request)  # let BaseChallenge do the heavy lifting

        new_filename = request.form.get("docker_image_filename") or request.get_json().get("docker_image_filename")
        if new_filename is not None:
            old_filename = getattr(challenge, "docker_image_filename", None)
            challenge.docker_image_filename = new_filename

            # Delete old file if it changed
            if old_filename and old_filename != new_filename:
                cls._delete_image_file(old_filename)

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