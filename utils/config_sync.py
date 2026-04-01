from CTFd.utils import get_config, set_config
from ..core.config import RuntimeConfig

RUNTIME_ATTRS = [
    "WORKER_NODES",
    "CTFD_DOMAIN_NAME",
    "REGISTRY_URL",
    "REGISTRY_USER",
    "REGISTRY_PASSWORD",
    "REGISTRY_NAMESPACE",
    "REGISTRY_CERT_PATH",
    "MAX_ACTIVE_CONTAINERS_PER_GROUP",
    "CONTAINER_SUSPENSION_INTERVAL",
    "DOCKER_CONTAINER_LIFETIME",
    "MEM_LIMIT_PER_CONTAINER",
    "MAX_SPARE_RAM",
    "DOCKER_CONTAINER_CPU_QUOTA",
    "DOCKER_CONTAINER_NETWORK",
    "INTERNAL_PORT_RANGE_START",
    "INTERNAL_PORT_RANGE_END",
    "TCP_PORT_RANGE_START",
    "TCP_PORT_RANGE_END",
    "CONTAINER_CACHE_TTL",
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
            elif isinstance(default, list):
                # Handle stored JSON strings for lists
                import json
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                        if not isinstance(value, list):
                            value = default
                    except Exception:
                        # Fallback: comma-separated
                        value = [v.strip() for v in value.split(",") if v.strip()]
                elif not isinstance(value, list):
                    value = default
        except Exception:
            value = default

        setattr(RuntimeConfig, attr, value)



def save_runtime_config(form_data):
    import json

    for attr in RUNTIME_ATTRS:
        key = config_key(attr)
        default = getattr(RuntimeConfig, attr)
        raw = form_data.get(key)

        if raw is None:
            continue

        if isinstance(default, int):
            val = int(raw)
        elif isinstance(default, list):
            # Expect raw to be a list (from JS dynamic inputs)
            val = raw if isinstance(raw, list) else [v.strip() for v in str(raw).split(",") if v.strip()]
            val = json.dumps(val)  # store as JSON string
        else:
            val = raw

        set_config(key, val)
        setattr(RuntimeConfig, attr, raw if isinstance(default, list) else val)



