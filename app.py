
import os
import re
import time
import json
import base64
import hashlib
import logging
import requests

from urllib.parse import urlparse

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("traefik-sync")

# Configuration
TRAEFIK_API = os.getenv(
    "TRAEFIK_API", "http://traefik:8080"
).rstrip("/")

ETCD_ENDPOINT = os.getenv(
    "ETCD_ENDPOINT", "http://etcd:2379"
).rstrip("/")

# Traefik's KV providers use keys without a leading slash
# (rootKey "traefik" -> "traefik/http/routers/..."), so strip it
# even if ETCD_PREFIX is set as "/traefik".
ETCD_PREFIX = os.getenv(
    "ETCD_PREFIX", "traefik"
).strip("/")

HOST_ID = os.environ["HOST_ID"]

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

USE_HTTPS_UPSTREAM = (
    os.getenv("USE_HTTPS_UPSTREAM", "false").lower() == "true"
)

UPSTREAM_TLS_INSECURE = (
    os.getenv("UPSTREAM_TLS_INSECURE", "false").lower() == "true"
)

UPSTREAM_TLS_SERVER_NAME = os.getenv(
    "UPSTREAM_TLS_SERVER_NAME", ""
)

SYNC_TCP_ROUTERS = (
    os.getenv("SYNC_TCP_ROUTERS", "false").lower() == "true"
)

UPSTREAMS = json.loads(os.getenv("UPSTREAMS", "{}"))

TCP_UPSTREAMS = json.loads(
    os.getenv("TCP_UPSTREAMS", "{}")
)

CENTRAL_ENTRYPOINTS = json.loads(
    os.getenv("CENTRAL_ENTRYPOINTS", "{}")
)

CENTRAL_TCP_ENTRYPOINTS = json.loads(
    os.getenv("CENTRAL_TCP_ENTRYPOINTS", "{}")
)

session = requests.Session()


# --------------------------------------------------
# Utility functions
# --------------------------------------------------

def b64(value):
    return base64.b64encode(
        str(value).encode("utf-8")
    ).decode("ascii")


def prefix_end(prefix):
    data = bytearray(prefix.encode("utf-8"))

    for i in range(len(data) - 1, -1, -1):
        if data[i] < 255:
            data[i] += 1
            return bytes(data[:i + 1])

    return b"\0"


def safe_name(value):
    return re.sub(r"[^a-zA-Z0-9_-]", "-", value)


def resource_name(router, entrypoint, protocol):
    identity = f"{HOST_ID}-{protocol}-{router}-{entrypoint}"
    digest = hashlib.sha256(
        identity.encode()
    ).hexdigest()[:10]

    return safe_name(identity)[:100] + "-" + digest


def http_upstream(entrypoint):
    value = UPSTREAMS.get(entrypoint)

    if not value:
        return None

    # Allow either host:port or a complete URL. An explicit scheme
    # wins; USE_HTTPS_UPSTREAM only applies to bare host:port values.
    if "://" in value:
        parsed = urlparse(value)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc
    else:
        scheme = "https" if USE_HTTPS_UPSTREAM else "http"
        netloc = value

    return f"{scheme}://{netloc}"


# --------------------------------------------------
# etcd operations
# --------------------------------------------------

# etcd rejects transactions with more than --max-txn-ops (default 128).
ETCD_MAX_TXN_OPS = int(os.getenv("ETCD_MAX_TXN_OPS", "100"))


def etcd_range(prefix):
    """Return {key: value} for every key under prefix."""
    response = session.post(
        ETCD_ENDPOINT + "/v3/kv/range",
        json={
            "key": b64(prefix),
            "range_end": base64.b64encode(
                prefix_end(prefix)
            ).decode("ascii")
        },
        timeout=10
    )

    response.raise_for_status()

    return {
        base64.b64decode(item["key"]).decode("utf-8"):
        base64.b64decode(item.get("value", "")).decode("utf-8")
        for item in response.json().get("kvs", [])
    }


def etcd_txn(puts, deletes):
    """Apply puts ({key: value}) and deletes ([key]) in one transaction,
    so Traefik never observes a half-written router or service."""
    operations = [
        {"requestPut": {"key": b64(key), "value": b64(value)}}
        for key, value in puts.items()
    ] + [
        {"requestDeleteRange": {"key": b64(key)}}
        for key in deletes
    ]

    if not operations:
        return

    response = session.post(
        ETCD_ENDPOINT + "/v3/kv/txn",
        json={"success": operations},
        timeout=10
    )

    response.raise_for_status()


# --------------------------------------------------
# Traefik API
# --------------------------------------------------

def get_json(path):
    response = session.get(
        TRAEFIK_API + path,
        timeout=10
    )

    response.raise_for_status()
    return response.json()


def get_http_rawdata():
    return get_json("/api/rawdata")


def get_tcp_routers():
    return get_json("/api/tcp/routers")


# --------------------------------------------------
# HTTP router synchronization
# --------------------------------------------------

def build_http_config(raw):
    desired = {}

    for original_name, router in raw.get("routers", {}).items():

        if router.get("status") != "enabled":
            continue

        if router.get("error"):
            continue

        rule = router.get("rule")

        if not rule:
            continue

        for entrypoint in router.get("entryPoints", []):

            upstream = http_upstream(entrypoint)

            if not upstream:
                log.warning(
                    "No HTTP upstream for entrypoint %s",
                    entrypoint
                )
                continue

            central_ep = CENTRAL_ENTRYPOINTS.get(
                entrypoint, entrypoint
            )

            name = resource_name(
                original_name, entrypoint, "http"
            )

            router_key = (
                f"{ETCD_PREFIX}/http/routers/{name}"
            )

            service_key = (
                f"{ETCD_PREFIX}/http/services/{name}"
            )

            # HTTP router
            desired[router_key + "/rule"] = rule
            desired[router_key + "/service"] = name
            desired[router_key + "/entryPoints/0"] = central_ep

            # Keep TLS termination enabled at primary Traefik.
            # Traefik reports default TLS as an empty object ({}),
            # which is falsy, so test for presence rather than truthiness.
            if router.get("tls") is not None:
                desired[router_key + "/tls"] = "true"

            # HTTP service
            desired[
                service_key + "/loadBalancer/servers/0/url"
            ] = upstream

            desired[
                service_key + "/loadBalancer/passHostHeader"
            ] = "true"

            # Applies to every HTTPS upstream, however the scheme was chosen.
            if upstream.startswith("https://"):
                transport_name = "sync-" + safe_name(HOST_ID)

                desired[
                    service_key + "/loadBalancer/serversTransport"
                ] = transport_name

                transport_key = (
                    f"{ETCD_PREFIX}/http/serversTransports/"
                    f"{transport_name}"
                )

                if UPSTREAM_TLS_INSECURE:
                    desired[
                        transport_key + "/insecureSkipVerify"
                    ] = "true"

                if UPSTREAM_TLS_SERVER_NAME:
                    desired[
                        transport_key + "/serverName"
                    ] = UPSTREAM_TLS_SERVER_NAME

    return desired


# --------------------------------------------------
# TCP router synchronization
# --------------------------------------------------

def build_tcp_config():
    desired = {}

    if not SYNC_TCP_ROUTERS:
        return desired

    try:
        routers = get_tcp_routers()
    except Exception:
        log.exception("Unable to retrieve TCP routers")
        raise

    for router in routers:

        if router.get("status") != "enabled":
            continue

        if router.get("error"):
            continue

        original_name = router.get("name")
        rule = router.get("rule")

        if not original_name or not rule:
            continue

        for entrypoint in router.get("entryPoints", []):

            upstream = TCP_UPSTREAMS.get(entrypoint)

            if not upstream:
                log.warning(
                    "No TCP upstream configured for %s",
                    entrypoint
                )
                continue

            central_ep = CENTRAL_TCP_ENTRYPOINTS.get(
                entrypoint, entrypoint
            )

            name = resource_name(
                original_name, entrypoint, "tcp"
            )

            router_key = (
                f"{ETCD_PREFIX}/tcp/routers/{name}"
            )

            service_key = (
                f"{ETCD_PREFIX}/tcp/services/{name}"
            )

            # TCP router
            desired[router_key + "/rule"] = rule
            desired[router_key + "/service"] = name
            desired[router_key + "/entryPoints/0"] = central_ep

            tls = router.get("tls")

            if tls is not None:
                if tls.get("passthrough", False):
                    desired[
                        router_key + "/tls/passthrough"
                    ] = "true"
                else:
                    # Central Traefik terminates TLS.
                    desired[router_key + "/tls"] = "true"

            if router.get("priority"):
                desired[
                    router_key + "/priority"
                ] = str(router["priority"])

            # TCP service
            desired[
                service_key +
                "/loadBalancer/servers/0/address"
            ] = upstream

    return desired


# --------------------------------------------------
# Reconciliation
# --------------------------------------------------

def resource_of(key):
    """Map 'traefik/http/routers/<name>/rule' to ('http', 'routers', '<name>')."""
    parts = key[len(ETCD_PREFIX) + 1:].split("/")

    if len(parts) < 3:
        return None

    return parts[0], parts[1], parts[2]


def synchronize():
    raw = get_http_rawdata()

    desired = {}
    desired.update(build_http_config(raw))
    desired.update(build_tcp_config())

    host_prefix = safe_name(HOST_ID) + "-"
    transport_name = "sync-" + safe_name(HOST_ID)

    def owned(key):
        resource = resource_of(key)

        if not resource:
            return False

        protocol, kind, name = resource

        if kind == "serversTransports":
            return name == transport_name

        return (
            kind in ("routers", "services")
            and name.startswith(host_prefix)
        )

    # Only keys created by this host; other hosts' keys are left alone.
    existing = {
        key: value
        for key, value in etcd_range(ETCD_PREFIX + "/").items()
        if owned(key)
    }

    puts = {
        key: value
        for key, value in desired.items()
        if existing.get(key) != value
    }

    deletes = [key for key in existing if key not in desired]

    if not puts and not deletes:
        log.info(
            "Host %s up to date (%d keys)", HOST_ID, len(desired)
        )
        return

    # Group changes by resource name so a router and its service
    # (which share a name) are written in the same transaction.
    # Servers transports go first, since services refer to them.
    groups = {}

    for key, value in puts.items():
        name = resource_of(key)[2]
        groups.setdefault(name, ({}, []))[0][key] = value

    for key in deletes:
        name = resource_of(key)[2]
        groups.setdefault(name, ({}, []))[1].append(key)

    ordered = sorted(
        groups.items(), key=lambda item: item[0] != transport_name
    )

    batch_puts, batch_deletes = {}, []

    for name, (group_puts, group_deletes) in ordered:
        size = len(group_puts) + len(group_deletes)
        batch_size = len(batch_puts) + len(batch_deletes)

        if batch_size and batch_size + size > ETCD_MAX_TXN_OPS:
            etcd_txn(batch_puts, batch_deletes)
            batch_puts, batch_deletes = {}, []

        batch_puts.update(group_puts)
        batch_deletes.extend(group_deletes)

    etcd_txn(batch_puts, batch_deletes)

    for key in deletes:
        log.info("Deleted stale key: %s", key)

    log.info(
        "Host %s synchronized: %d keys, %d written, %d deleted "
        "(TCP=%s, HTTPS=%s)",
        HOST_ID,
        len(desired),
        len(puts),
        len(deletes),
        SYNC_TCP_ROUTERS,
        USE_HTTPS_UPSTREAM
    )


# --------------------------------------------------
# Main
# --------------------------------------------------

def main():
    log.info("Starting Traefik synchronizer")
    log.info("Host ID: %s", HOST_ID)
    log.info("HTTPS upstream: %s", USE_HTTPS_UPSTREAM)
    log.info("TCP synchronization: %s", SYNC_TCP_ROUTERS)

    while True:
        try:
            synchronize()
        except Exception:
            log.exception("Synchronization failed")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
