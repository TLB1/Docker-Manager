import os
import requests
import docker
from docker.errors import NotFound, APIError
from typing import List, Optional
from .config import RuntimeConfig
from ..models.node import Node


class RegistryManager:
    def __init__(self):
        self.registry  = RuntimeConfig.REGISTRY_URL
        self.user      = RuntimeConfig.REGISTRY_USER
        self.password  = RuntimeConfig.REGISTRY_PASSWORD
        self.namespace = RuntimeConfig.REGISTRY_NAMESPACE
        self.client    = docker.from_env()

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def challenge_repo(self, image: str) -> str:
        return f"{self._registry_host()}/{self.namespace}/{image}"



    def tag_for_challenge(self, image: str) -> str:
        return f"{self.challenge_repo(image)}:latest"



    def _is_configured(self) -> bool:
        """Return True only when a real registry URL has been set."""
        return bool(self.registry and self.registry.strip())



    def _registry_host(self) -> str:
        """
        Bare host:port for use in Docker image references and docker login.
        Docker never accepts a scheme in an image reference.
        """
        host = self.registry
        for scheme in ("https://", "http://"):
            if host.startswith(scheme):
                host = host[len(scheme):]
                break
        return host.rstrip("/")

    def _registry_base_url(self) -> str:
        """
        Normalised base URL for the Registry HTTP API v2.

        Rules:
          - If the URL already has a scheme (http:// or https://) use it as-is.
          - Otherwise default to http:// — private/internal registries (e.g.
            a plain Docker registry on port 5000) almost never run TLS without
            an explicit scheme being configured.
        """
        url = self.registry.rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        return url

    def _registry_session(self) -> requests.Session:
        """
        Authenticated requests session for the Registry HTTP API.
        Uses the configured CA cert for self-signed TLS registries so we
        never need to disable SSL verification.
        """
        s = requests.Session()
        if self.user and self.password:
            s.auth = (self.user, self.password)
        s.headers["Accept"] = "application/json"

        cert_path = getattr(RuntimeConfig, "REGISTRY_CERT_PATH", None)
        if cert_path and os.path.isfile(cert_path):
            s.verify = cert_path   # verify against our CA cert instead of system bundle
        elif not self._registry_base_url().startswith("https"):
            s.verify = False       # plain HTTP — no TLS at all

        return s

    # ------------------------------------------------------------------ #
    # Auth                                                                 #
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # Image listing                                                        #
    # ------------------------------------------------------------------ #

    def list_images(self) -> List[dict]:
        """
        Return a flat list of every image:tag available in the registry.

        Each entry:
            {
                "tag":      "registry.example.com/ns/myimage:latest",
                "repo":     "ns/myimage",
                "short_tag": "latest",
                "source":   "registry",
            }

        Falls back to an empty list (with a logged warning) on any error so
        the caller can decide how to handle a missing / unreachable registry.

        Pagination:  The Registry API v2 may return a ``Link`` header
        pointing to the next page of repositories.  We follow it until
        exhausted.
        """
        if not self._is_configured():
            return []

        session  = self._registry_session()
        base_url = self._registry_base_url()
        images   = []

        # ── 1. Enumerate repositories (/v2/_catalog) ──────────────────
        repos: List[str] = []
        url: Optional[str] = f"{base_url}/v2/_catalog?n=200"
        while url:
            try:
                resp = session.get(url, timeout=10)
                resp.raise_for_status()
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    f"[RegistryManager] Could not reach registry catalog: {exc}"
                )
                return []

            data = resp.json()
            repos.extend(data.get("repositories") or [])

            # Follow Link header for next page
            url = _parse_link_header(resp.headers.get("Link", ""))

        # ── 2. Filter to our namespace (if set) ───────────────────────
        if self.namespace:
            prefix = self.namespace.rstrip("/") + "/"
            repos  = [r for r in repos if r.startswith(prefix)]

        # ── 3. Enumerate tags for each repo ───────────────────────────
        for repo in repos:
            tags_url: Optional[str] = f"{base_url}/v2/{repo}/tags/list"
            while tags_url:
                try:
                    resp = session.get(tags_url, timeout=10)
                    resp.raise_for_status()
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"[RegistryManager] Could not list tags for {repo}: {exc}"
                    )
                    break

                tag_data = resp.json()
                for short_tag in (tag_data.get("tags") or []):
                    images.append({
                        "tag":       f"{self._registry_host()}/{repo}:{short_tag}",
                        "repo":      repo,
                        "short_tag": short_tag,
                        "source":    "registry",
                    })

                tags_url = _parse_link_header(resp.headers.get("Link", ""))

        return sorted(images, key=lambda x: x["tag"])

    # ------------------------------------------------------------------ #
    # Push / existence                                                     #
    # ------------------------------------------------------------------ #

    def push_challenge_image(self, image: str) -> str:
        """Tag and push a local image to the private registry."""
        self.login_local()
        tagged = self.tag_for_challenge(image)
        img    = self.client.images.get(image)
        img.tag(tagged)
        self.client.images.push(tagged)
        return tagged

    def ensure_image_exists(self, image: str) -> str:
        tagged = self.tag_for_challenge(image)
        print(f"Checking for challenge image {tagged} in registry...")
        try:
            self.client.images.get_registry_data(tagged)
            return tagged
        except NotFound:
            print(f"Pushing missing challenge image {image}")
            return self.push_challenge_image(image)
        except APIError as exc:
            raise RuntimeError(f"Registry check failed: {exc}")


# ── Module-level helper ────────────────────────────────────────────────────

def _parse_link_header(header: str) -> Optional[str]:
    """
    Extract the ``next`` URL from a standard HTTP Link header, e.g.:
        <https://registry/v2/_catalog?last=foo&n=200>; rel="next"
    Returns None when there is no next page.
    """
    if not header:
        return None
    for part in header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            url_part = part.split(";")[0].strip()
            return url_part.strip("<>")
    return None