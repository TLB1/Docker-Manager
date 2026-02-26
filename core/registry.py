import docker
from docker.errors import NotFound, APIError
from typing_extensions import List

from .config import RuntimeConfig
from ..models.node import Node


class RegistryManager:

    def __init__(self):
        self.registry = RuntimeConfig.REGISTRY_URL
        self.user = RuntimeConfig.REGISTRY_USER
        self.password = RuntimeConfig.REGISTRY_PASSWORD
        self.namespace = RuntimeConfig.REGISTRY_NAMESPACE

        self.client = docker.from_env()

    # -------------------------
    # Helpers
    # -------------------------

    def challenge_repo(self, image: str) -> str:
        return f"{self.registry}/{self.namespace}/{image}"

    def tag_for_challenge(self, image: str) -> str:
        return f"{self.challenge_repo(image)}:latest"

    # -------------------------
    # Auth
    # -------------------------

    def login_local(self):
        self.client.login(
            username=self.user,
            password=self.password,
            registry=self.registry,
            reauth=True,
        )

    def login_node(self, node: Node):
        if not node.client:
            return

        node.client.login(
            username=self.user,
            password=self.password,
            registry=self.registry,
            reauth=True,
        )

    def login_all_nodes(self, nodes: List[Node]):
        for node in nodes:
            self.login_node(node)

    # -------------------------
    # Push
    # -------------------------

    def push_challenge_image(self, image: str) -> str:
        """
        Tags and pushes an image to the private registry.
        """
        self.login_local()

        tagged = self.tag_for_challenge(image)

        img = self.client.images.get(image)
        img.tag(tagged)

        self.client.images.push(tagged)

        return tagged

    # -------------------------
    # Existence check
    # -------------------------

    def ensure_image_exists(self, image: str) -> str:
        tagged = self.tag_for_challenge(image)
        print(f"Checking for challenge image {tagged} in registry...")

        try:
            self.client.images.get_registry_data(tagged)
            return tagged

        except NotFound:
            print(f"Pushing missing challenge image {image}")
            return self.push_challenge_image(image)

        except APIError as e:
            raise RuntimeError(f"Registry check failed: {e}")