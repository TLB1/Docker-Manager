from CTFd.utils import get_config, set_config
from ..core.config import RuntimeConfig

RUNTIME_ATTRS = [
    "MAX_ACTIVE_CONTAINERS_PER_GROUP",
    "CONTAINER_SUSPENSION_INTERVAL",
    "DOCKER_CONTAINER_LIFETIME",
    "MEM_LIMIT_PER_CONTAINER",
    "MAX_SPARE_RAM",
    "DOCKER_CONTAINER_CPU_QUOTA",
    "DOCKER_CONTAINER_NETWORK",
    "INTERNAL_PORT_RANGE_START",
    "INTERNAL_PORT_RANGE_END",
]

CONFIG_PREFIX = "docker_"



def config_key(attr: str) -> str:
    return f"{CONFIG_PREFIX}{attr.lower()}"



def load_runtime_config():
    for attr in RUNTIME_ATTRS:
        key = config_key(attr)
        default = getattr(RuntimeConfig, attr)
        value = get_config(key, default=default)
        try:
            if isinstance(default, int):
                value = int(value)
        except Exception:
            value = default
        setattr(RuntimeConfig, attr, value)



def save_runtime_config(form_data):
    for attr in RUNTIME_ATTRS:
        key = config_key(attr)
        default = getattr(RuntimeConfig, attr)
        raw = form_data.get(key)
        if raw is None:
            continue
        val = int(raw) if isinstance(default, int) else raw
        set_config(key, val)
        setattr(RuntimeConfig, attr, val)


