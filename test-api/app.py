#!/usr/bin/env python3
"""Offline stand-in for the k8s-manager API.

Shapes follow the live-verified contract captured in
../k8s-manager-api-reference/ (API_REFERENCE.md plus one real response per
endpoint in outputs/). Where this mock and that reference disagree, the
reference wins - the point of this process is that passing against the mock
means something against the real cluster.

Implemented: /health, /info/services, /info/{service}, deployment-tree,
/status (list, filtered, single), and the /instances write surface.
Not implemented (unused by the agent): /kafka/*, /snapshots/*, PATCH.

Instance state is in-memory and seeded from instances.json, so
POST -> GET /status -> DELETE is coherent within a process lifetime.
"""

import json
import re
import uuid
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

NAMESPACE = "intend"

# Keys dropped when short_info=true, per GET /status?short_info=true.
STATUS_SHORT_DROP = (
    "env_vars",
    "kafka_topics",
    "ram_memory_usage_mb",
    "frequency_metrics",
)

DATA_PATH = Path(__file__).with_name("services.json")
INSTANCES_PATH = Path(__file__).with_name("instances.json")

SERVICES_PAYLOAD = json.loads(DATA_PATH.read_text(encoding="utf-8"))
SERVICES_BY_NAME = {item["name"]: item for item in SERVICES_PAYLOAD["services"]}

_seed = json.loads(INSTANCES_PATH.read_text(encoding="utf-8"))
INSTANCES = {item["instance_name"]: item for item in _seed["instances"]}


def _truthy(values: list[str]) -> bool:
    return bool(values) and str(values[0]).lower() in {"1", "true", "yes", "on"}


def _chart_defaults(service_name: str, instance_name: str) -> dict[str, str]:
    """Reproduce the chart-default merge that POST /instances performs.

    Real k8s-manager merges submitted env_vars with the Helm chart defaults,
    explicit values winning, and GET /status reports the merged result. Env
    vars whose default is null are omitted entirely.
    """
    service = SERVICES_BY_NAME.get(service_name, {})
    merged: dict[str, str] = {}
    for bucket in ("required_env_vars", "optional_env_vars", "frozen_env_vars"):
        for var in service.get(bucket) or []:
            value = var.get("value")
            if value is None or value == "":
                continue
            if isinstance(value, list):
                merged[var["name"]] = json.dumps(value, separators=(",", ":"))
            else:
                merged[var["name"]] = str(value)
    # Frozen consumer group ids are rewritten per instance on the real API.
    if "KAFKA_CONSUMER_GROUP_ID" in merged:
        merged["KAFKA_CONSUMER_GROUP_ID"] = (
            f"{merged['KAFKA_CONSUMER_GROUP_ID']}-{instance_name}"
        )
    return merged


def _new_instance(body: dict) -> dict:
    service_name = body.get("service_name")
    prefix = body.get("instance_prefix")
    suffix = uuid.uuid4().hex[:8]
    parts = [p for p in (prefix, service_name, suffix) if p]
    instance_name = "-".join(parts)

    submitted = {k: str(v) for k, v in (body.get("env_vars") or {}).items()}
    env_vars = _chart_defaults(service_name, instance_name)
    env_vars.update(submitted)

    metrics_trackable = body.get("metrics_trackable")
    if metrics_trackable is None:
        metrics_trackable = True

    return {
        "instance_name": instance_name,
        "service_name": service_name,
        "namespace": NAMESPACE,
        "node": body.get("node_name"),
        "deployment_status": "Available",
        "pods": [
            {
                "name": f"{instance_name}-{uuid.uuid4().hex[:10]}-{uuid.uuid4().hex[:5]}",
                "started": "2026-09-14T09:20:08+00:00",
                "phase": "Running",
                "ready": True,
                "restart_count": 0,
            }
        ],
        "env_vars": env_vars,
        "kafka_topics": [],
        "metrics_trackable": bool(metrics_trackable),
        "ram_memory_usage_mb": 0.0,
        "frequency_metrics": None,
    }


def _creation_response(instance: dict) -> dict:
    """The 5-key response POST and PUT both return."""
    image_name = (instance["service_name"] or "").replace("-", "_")
    return {
        "instance_name": instance["instance_name"],
        "service_name": instance["service_name"],
        "namespace": instance["namespace"],
        "image": f"ghcr.io/gate-institute/intend/{image_name}:latest",
        "metrics_trackable": instance["metrics_trackable"],
    }


def _shorten_instance(instance: dict) -> dict:
    return {k: v for k, v in instance.items() if k not in STATUS_SHORT_DROP}


def _shorten_service(service: dict) -> dict:
    """short_info=true returns 4 keys, and versions comes back empty."""
    return {
        "name": service.get("name"),
        "description": service.get("description"),
        "versions": [],
        "dependencies": service.get("dependencies") or [],
    }


def _deployment_tree(service_name: str, env: dict) -> dict:
    """Recursive {service_name, parameters, dependencies}.

    parameters is a flat dict of strings; list values appear as JSON encoded
    inside a string. Unresolved EnvVarAlias references stay as literal $NAME,
    which is what the real endpoint returns.
    """
    service = SERVICES_BY_NAME.get(service_name, {})
    parameters: dict[str, str] = {}
    for bucket in ("required_env_vars", "optional_env_vars", "frozen_env_vars"):
        for var in service.get(bucket) or []:
            name = var["name"]
            if name in env:
                value = env[name]
            else:
                value = var.get("value")
            if value is None:
                continue
            if isinstance(value, list):
                parameters[name] = json.dumps(value, separators=(",", ":"))
            else:
                parameters[name] = str(value)
    return {
        "service_name": service_name,
        "parameters": parameters,
        "dependencies": [
            _deployment_tree(dep, env) for dep in service.get("dependencies") or []
        ],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "test-api/2.0"

    # --- plumbing ---------------------------------------------------------

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_no_content(self) -> None:
        """DELETE answers 204 with no body at all."""
        self.send_response(204)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

    def _not_found(self, detail: str) -> None:
        """FastAPI error shape: {"detail": ...}, never {"error": ...}."""
        self._send_json({"detail": detail}, status=404)

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    # --- GET --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        short_info = _truthy(query.get("short_info", []))

        if path == "/health":
            self._send_json({"status": "ok"})
            return

        if path == "/info/services":
            services = SERVICES_PAYLOAD["services"]
            if short_info:
                services = [_shorten_service(item) for item in services]
            self._send_json({"services": services, "total": len(services)})
            return

        if path == "/status":
            name = query.get("name", [None])[0]
            instances = list(INSTANCES.values())
            if name:
                instances = [i for i in instances if i["service_name"] == name]
            if short_info:
                instances = [_shorten_instance(i) for i in instances]
            self._send_json({"instances": instances, "total": len(instances)})
            return

        match = re.fullmatch(r"/status/([^/]+)", path)
        if match:
            instance = INSTANCES.get(match.group(1))
            if instance is None:
                self._not_found(f"Instance '{match.group(1)}' not found.")
                return
            # Bare instance object - no instances/total wrapper.
            self._send_json(
                _shorten_instance(instance) if short_info else instance
            )
            return

        match = re.fullmatch(r"/info/([^/]+)", path)
        if match:
            service = SERVICES_BY_NAME.get(match.group(1))
            if service is None:
                self._not_found(f"Service '{match.group(1)}' not found.")
                return
            self._send_json(service)
            return

        self._not_found(f"Not Found: {path}")

    # --- POST -------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        body = self._read_body()
        if body is None:
            self._send_json({"detail": "Invalid JSON body."}, status=422)
            return

        if path == "/instances":
            service_name = body.get("service_name")
            if not service_name:
                self._send_json(
                    {"detail": "service_name is required."}, status=422
                )
                return
            if service_name not in SERVICES_BY_NAME:
                self._not_found(f"Service '{service_name}' not found.")
                return
            instance = _new_instance(body)
            INSTANCES[instance["instance_name"]] = instance
            self._send_json(_creation_response(instance))
            return

        match = re.fullmatch(r"/info/([^/]+)/deployment-tree", path)
        if match:
            service_name = match.group(1)
            if service_name not in SERVICES_BY_NAME:
                self._not_found(f"Service '{service_name}' not found.")
                return
            env = body.get("env") or {}
            self._send_json(_deployment_tree(service_name, env))
            return

        self._not_found(f"Not Found: {path}")

    # --- PUT / DELETE -----------------------------------------------------

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        match = re.fullmatch(r"/instances/([^/]+)", path)
        if not match:
            self._not_found(f"Not Found: {path}")
            return

        body = self._read_body()
        if body is None:
            self._send_json({"detail": "Invalid JSON body."}, status=422)
            return

        instance_name = match.group(1)
        instance = INSTANCES.get(instance_name)
        if instance is None:
            self._not_found(f"Instance '{instance_name}' not found.")
            return

        if "env_vars" in body:
            updated = deepcopy(instance)
            merged = _chart_defaults(instance["service_name"], instance_name)
            merged.update({k: str(v) for k, v in (body["env_vars"] or {}).items()})
            updated["env_vars"] = merged
            INSTANCES[instance_name] = updated
            instance = updated

        self._send_json(_creation_response(instance))

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        match = re.fullmatch(r"/instances/([^/]+)", path)
        if not match:
            self._not_found(f"Not Found: {path}")
            return
        instance_name = match.group(1)
        if INSTANCES.pop(instance_name, None) is None:
            self._not_found(f"Instance '{instance_name}' not found.")
            return
        self._send_no_content()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8001), Handler)
    server.serve_forever()
