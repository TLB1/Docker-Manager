"""In-memory TTL cache for Docker container metadata.

Stores lightweight CachedContainer records (labels + status + image tags) and
keeps them consistent via TTL-based full refresh and optimistic mutation updates.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Dict, List, Optional


@dataclass
class CachedContainer:
    token: str
    team_id: str
    challenge_id: str
    container_index: str
    network_alias: str
    status: str
    image_tags: List[str]
    node_address: str

    @classmethod
    def from_docker(cls, container, node_address: str) -> "CachedContainer":
        from .labels import DockerLabels
        labels = container.labels or {}
        try:
            tags = list(container.image.tags or [])
        except Exception:
            tags = []
        return cls(
            token=labels.get(DockerLabels.TOKEN, ""),
            team_id=labels.get(DockerLabels.TEAM, ""),
            challenge_id=labels.get(DockerLabels.CHALLENGE, ""),
            container_index=labels.get(DockerLabels.CONTAINER_INDEX, "0"),
            network_alias=labels.get(DockerLabels.NETWORK_ALIAS, ""),
            status=container.status,
            image_tags=tags,
            node_address=node_address,
        )

    @property
    def image_name(self) -> str:
        return self.image_tags[0] if self.image_tags else ""


class ContainerCache:
    """Thread-safe TTL cache for CTFd container metadata."""

    def __init__(self, ttl: float = 30.0):
        self._ttl = ttl
        self._lock = threading.RLock()
        self._by_token: Dict[str, CachedContainer] = {}
        self._last_refresh: float = 0.0

    # ------------------------------------------------------------------
    # Staleness
    # ------------------------------------------------------------------

    def is_stale(self) -> bool:
        return (time.monotonic() - self._last_refresh) > self._ttl

    def invalidate(self):
        """Force the next read to trigger a full refresh."""
        with self._lock:
            self._last_refresh = 0.0

    # ------------------------------------------------------------------
    # Bulk refresh
    # ------------------------------------------------------------------

    def rebuild(self, containers_by_node: Dict[str, list]):
        """Rebuild from a full Docker API scan.

        containers_by_node maps node_address -> list of docker Container objects.
        """
        with self._lock:
            self._by_token.clear()
            for node_address, containers in containers_by_node.items():
                for c in containers:
                    entry = CachedContainer.from_docker(c, node_address)
                    if entry.token:
                        self._by_token[entry.token] = entry
            self._last_refresh = time.monotonic()

    # ------------------------------------------------------------------
    # Point mutations (called immediately after each Docker operation)
    # ------------------------------------------------------------------

    def add(self, entry: CachedContainer):
        with self._lock:
            self._by_token[entry.token] = entry

    def remove(self, token: str):
        with self._lock:
            self._by_token.pop(token, None)

    def update_status(self, token: str, status: str):
        with self._lock:
            if token in self._by_token:
                self._by_token[token] = replace(self._by_token[token], status=status)

    def clear(self):
        with self._lock:
            self._by_token.clear()
            self._last_refresh = 0.0

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, token: str) -> Optional[CachedContainer]:
        with self._lock:
            return self._by_token.get(token)

    def get_by_team_challenge(
        self,
        team_id: str,
        challenge_id: str,
        container_index: Optional[str] = None,
    ) -> List[CachedContainer]:
        with self._lock:
            results = [
                c for c in self._by_token.values()
                if c.team_id == team_id and c.challenge_id == challenge_id
            ]
            if container_index is not None:
                results = [c for c in results if c.container_index == container_index]
            return results

    def get_by_team(self, team_id: str) -> List[CachedContainer]:
        with self._lock:
            return [c for c in self._by_token.values() if c.team_id == team_id]

    def get_by_node(self, node_address: str) -> List[CachedContainer]:
        with self._lock:
            return [c for c in self._by_token.values() if c.node_address == node_address]

    def all(self) -> List[CachedContainer]:
        with self._lock:
            return list(self._by_token.values())
