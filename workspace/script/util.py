"""Utility functions for k8s_manager API calls used by this skill."""

from __future__ import annotations

import base64
import copy
import json
import os
import secrets
import sys
import time
from itertools import combinations
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

DEFAULT_TIMEOUT_SECONDS = 30

# Base URL for the k8s-manager API. Defaults to the compose service DNS name
# for the local mock; override with K8S_MANAGER_URL to point at a real cluster
# (through the ingress that is https://<host> on 443; :8000 only applies to a
# kubectl port-forward).
DEFAULT_SERVICE_ENDPOINT = os.getenv("K8S_MANAGER_URL", "http://test-api:8001")

# Credentials for the real k8s-manager, which sits behind HTTP Basic auth at
# the ingress. Read from the environment here and applied inside _request, so
# they never pass through a function argument, a URL or an error string - all
# three of those are written verbatim to /logs, streamed to the dashboard and
# sent on to the model. Empty by default so the local mock needs no config.
K8S_MANAGER_USERNAME = os.getenv("K8S_MANAGER_USERNAME", "")
K8S_MANAGER_PASSWORD = os.getenv("K8S_MANAGER_PASSWORD", "")

# Second, separate gate. Basic auth at the ingress gets you in; the application
# additionally requires an X-Admin-Key on every write: POST/PUT/DELETE
# /instances, PATCH /kafka/topics/config, POST /snapshots and /admin/whoami.
# Reads need only Basic auth, so this stays empty for read-only use.
K8S_MANAGER_ADMIN_KEY = os.getenv("K8S_MANAGER_ADMIN_KEY", "")

VEHICLE_TYPES = []
APPROACH_ZONES = []
PEDESTRIAN_TYPES = []
CROSSWALK_ZONES = []


def _dedupe_preserving_case(values: list[str], seen: dict[str, str]) -> None:
    """Record values case-insensitively while keeping the casing supplied.

    Zone IDs are matched literally by the services, and the catalogue
    publishes them mixed-case (C5_w, L1_n_ws). Uppercasing them produces IDs
    that match nothing, so comparison is case-folded but the stored value is
    whatever the caller gave us.
    """
    for item in values:
        if not item or not item.strip():
            continue
        item = item.strip()
        seen.setdefault(item.casefold(), item)


def _derive_scenario(
    fixed_services: list[dict[str, Any]] | None = None,
    service_variants: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    object_type_map: dict[str, str] = {}
    zone_map: dict[str, str] = {}

    services_to_scan: list[dict[str, Any]] = []
    services_to_scan.extend(fixed_services or [])
    for candidates in (service_variants or {}).values():
        services_to_scan.extend(candidates or [])

    for service in services_to_scan:
        env_vars = (service.get("env_vars") or {}) if isinstance(service, dict) else {}
        for key in ("OBJECT_TYPE_FILTER", "OBJECT_TYPES"):
            _dedupe_preserving_case(_as_list(env_vars.get(key)), object_type_map)
        for key in ("ZONES_OF_INTEREST", "APPROACH_ZONES", "CROSSWALK_ZONE", "ZONE"):
            _dedupe_preserving_case(_as_list(env_vars.get(key)), zone_map)

    object_types = set(object_type_map.values())
    zones = set(zone_map.values())

    vehicle_types = sorted({t for t in object_types if t.upper() != "PERSON"}, key=str)
    pedestrian_types = sorted(
        {t for t in object_types if t.upper() == "PERSON"}, key=str
    )

    crosswalk_zones = sorted(
        {z for z in zones if z.upper().startswith("C") or z.upper() in {"C5_W", "C5"}},
        key=str,
    )
    approach_zones = sorted(
        {
            z
            for z in zones
            if z.upper() not in {item.upper() for item in crosswalk_zones}
        },
        key=str,
    )

    types = vehicle_types + pedestrian_types
    zns = approach_zones + crosswalk_zones
    need: set[tuple[str, str]] = set()
    for t in vehicle_types:
        for z in approach_zones:
            need.add((t, z))
    for t in pedestrian_types:
        for z in crosswalk_zones:
            need.add((t, z))

    return {"types": types, "zones": zns, "need": need}


def _build_need(
    types: list[str] | None = None, zones: list[str] | None = None
) -> set[tuple[str, str]]:
    type_list = list(types or (VEHICLE_TYPES + PEDESTRIAN_TYPES))
    zone_list = list(zones or (APPROACH_ZONES + CROSSWALK_ZONES))
    need: set[tuple[str, str]] = set()
    for t in type_list:
        for z in zone_list:
            t_upper = t.upper()
            z_upper = z.upper()
            if (
                t_upper == "PERSON"
                and z_upper.startswith("C")
                or t_upper != "PERSON"
                and z_upper.startswith("L")
            ):
                need.add((t, z))
    return need


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def all_nonempty_subsets(items: list[str]) -> list[frozenset[str]]:
    subsets: list[frozenset[str]] = []
    for r in range(1, len(items) + 1):
        for combo in combinations(items, r):
            subsets.append(frozenset(combo))
    return subsets


def instance_coverage(
    group_types: frozenset[str],
    group_zones: frozenset[str],
    need: set[tuple[str, str]] | None = None,
) -> frozenset[tuple[str, str]]:
    active_need = need if need is not None else _build_need()
    return frozenset(
        (t, z) for t in group_types for z in group_zones if (t, z) in active_need
    )


def is_valid_instance(
    group_types: frozenset[str],
    group_zones: frozenset[str],
    need: set[tuple[str, str]] | None = None,
) -> bool:
    active_need = need if need is not None else _build_need()
    for t in group_types:
        if not any((t, z) in active_need for z in group_zones):
            return False
    for z in group_zones:
        if not any((t, z) in active_need for t in group_types):
            return False
    return True


def find_valid_instances(
    types: list[str] | None = None,
    zones: list[str] | None = None,
    need: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    types = list(types or (VEHICLE_TYPES + PEDESTRIAN_TYPES))
    zones = list(zones or (APPROACH_ZONES + CROSSWALK_ZONES))
    active_need = need if need is not None else _build_need(types, zones)
    valid: list[dict[str, Any]] = []
    for group_types in all_nonempty_subsets(types):
        for group_zones in all_nonempty_subsets(zones):
            if is_valid_instance(group_types, group_zones, active_need):
                valid.append(
                    {
                        "types": group_types,
                        "zones": group_zones,
                        "covers": instance_coverage(
                            group_types, group_zones, active_need
                        ),
                    }
                )
    return valid


def find_all_plans(
    valid_instances: list[dict[str, Any]], need: set[tuple[str, str]]
) -> list[list[dict[str, Any]]]:
    need = frozenset(need)
    by_edge = {
        edge: [inst for inst in valid_instances if edge in inst["covers"]]
        for edge in need
    }
    plans: list[list[dict[str, Any]]] = []

    def backtrack(
        remaining: frozenset[tuple[str, str]], chosen: list[dict[str, Any]]
    ) -> None:
        if not remaining:
            plans.append(list(chosen))
            return
        edge = min(remaining, key=lambda item: len(by_edge[item]))
        for inst in by_edge[edge]:
            if inst["covers"] <= remaining:
                chosen.append(inst)
                backtrack(remaining - inst["covers"], chosen)
                chosen.pop()

    backtrack(need, [])
    return plans


def _normalize_service_entry(service: Any) -> dict[str, Any]:
    if isinstance(service, str):
        return {"service_name": service, "action": "new_instance", "env_vars": {}}
    if not isinstance(service, dict):
        raise ValueError(
            f"Service entries must be dicts or strings, got {type(service).__name__}"
        )

    normalized = copy.deepcopy(service)
    normalized.setdefault("action", "new_instance")
    normalized.setdefault("env_vars", {})
    normalized["service_name"] = normalized.get("service_name") or normalized.get(
        "name"
    )
    if normalized["service_name"] is None:
        raise ValueError("Service entries must include a service_name")
    return normalized


def _normalize_service_variants(
    service_variants: Any,
) -> dict[str, list[dict[str, Any]]]:
    if service_variants is None:
        return {}

    if isinstance(service_variants, dict):
        normalized: dict[str, list[dict[str, Any]]] = {}
        for key, variants in service_variants.items():
            if variants is None:
                continue
            items = variants if isinstance(variants, list) else [variants]
            normalized[key] = [_normalize_service_entry(item) for item in items]
        return normalized

    if isinstance(service_variants, (list, tuple)):
        grouped: dict[str, list[dict[str, Any]]] = {}

        # captain style: list of variant groups, each group being a list of service dicts
        if service_variants and all(
            isinstance(group, list) for group in service_variants
        ):
            for group in service_variants:
                for item in group:
                    entry = _normalize_service_entry(item)
                    grouped.setdefault(entry["service_name"], []).append(entry)
            return grouped

        for item in service_variants:
            entry = _normalize_service_entry(item)
            grouped.setdefault(entry["service_name"], []).append(entry)
        return grouped

    return {}


def _normalize_fixed_services(fixed_services: Any) -> list[dict[str, Any]]:
    if fixed_services is None:
        return []
    if not isinstance(fixed_services, list):
        fixed_services = [fixed_services]
    return [_normalize_service_entry(item) for item in fixed_services]


def generate_deployment_plans(
    fixed_services: list[dict[str, Any]] | None = None,
    service_variants: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Generate every exact-cover deployment plan for the required type/zone pairs.

    This follows the exact-cover logic used in the crosswalk planning script:
    - enumerate every valid instance shape that is not vacuous
    - every plan must cover all required (type, zone) pairs exactly once
    - no gaps and no double coverage are allowed

    The captain can emit either the canonical dict form or the intermediate list-based form,
    so the planner normalizes both shapes before exact-cover generation.
    """
    normalized_fixed = _normalize_fixed_services(fixed_services)
    normalized_variants = _normalize_service_variants(service_variants)
    if not normalized_variants:
        return {"plans": []}

    scenario = _derive_scenario(normalized_fixed, normalized_variants)
    if not scenario["types"] or not scenario["zones"] or not scenario["need"]:
        return {"plans": []}
    valid_instances = find_valid_instances(
        scenario["types"], scenario["zones"], scenario["need"]
    )
    plans = find_all_plans(valid_instances, scenario["need"])
    generated: list[dict[str, Any]] = []
    for index, plan in enumerate(plans, start=1):
        plan_services: list[dict[str, Any]] = []
        for service_entry in normalized_fixed:
            plan_services.append(
                {
                    "service_name": service_entry["service_name"],
                    "action": service_entry.get("action", "new_instance"),
                    "env_vars": service_entry.get("env_vars", {}),
                }
            )

        for inst in plan:
            plan_services.append(
                {
                    "service_name": "object-in-zone-detection",
                    "action": "new_instance",
                    "env_vars": {
                        # list-valued per the catalogue; _stringify_env
                        # JSON-encodes these on the way to the API
                        "OBJECT_TYPE_FILTER": sorted(inst["types"]),
                        "ZONES_OF_INTEREST": sorted(inst["zones"]),
                    },
                }
            )

        plan_services.sort(key=lambda item: item["service_name"])
        plan_obj = {
            "plan_name": f"Alternative {index}",
            "services": plan_services,
        }
        generated.append(plan_obj)

    return {"plans": generated}

# ---------------------------------------------------------------------------
# k8s-manager HTTP client
#
# Written against the live-verified contract in k8s-manager-api-reference/.
# Base URL comes from K8S_MANAGER_URL so switching from the mock to a real
# cluster is a config change, not a code change.
# ---------------------------------------------------------------------------


class ApiError(RuntimeError):
    """A non-2xx response from k8s-manager.

    `detail` carries the API's own message. Real k8s-manager is FastAPI and
    reports errors as {"detail": "..."}, not {"error": ...}.
    """

    def __init__(self, status: int, detail: str, url: str) -> None:
        super().__init__(f"{url} -> HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
        self.url = url


def _resolve_endpoint(service_endpoint: str | None) -> str:
    return (service_endpoint or DEFAULT_SERVICE_ENDPOINT).rstrip("/")


def _auth_header() -> dict[str, str]:
    """Build the auth headers for whatever is configured.

    Two independent gates: HTTP Basic at the ingress (needed for everything)
    and X-Admin-Key in the application (needed for writes only). Each is
    omitted when unset, so the local mock works with no configuration and
    read-only use needs no admin key. Basic needs both halves; a half-set pair
    sends no header rather than an invalid one.

    Secrets go in headers and never in the URL, because ApiError puts the URL
    in its message, which reaches the logs, the trace DB and the model.
    """
    headers: dict[str, str] = {}
    if K8S_MANAGER_USERNAME and K8S_MANAGER_PASSWORD:
        raw = f"{K8S_MANAGER_USERNAME}:{K8S_MANAGER_PASSWORD}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    if K8S_MANAGER_ADMIN_KEY:
        headers["X-Admin-Key"] = K8S_MANAGER_ADMIN_KEY
    return headers


def _build_url(
    service_endpoint: str | None,
    relative_path: str,
    params: dict[str, Any] | None = None,
) -> str:
    base = _resolve_endpoint(service_endpoint) + "/"
    url = urljoin(base, relative_path.lstrip("/"))
    if params:
        query = {}
        for key, value in params.items():
            if value is None:
                continue
            if value is True:
                query[key] = "true"
            elif value is False:
                query[key] = "false"
            else:
                query[key] = str(value)
        if query:
            url = url + "?" + urlencode(query)
    return url


# --- request tracing -------------------------------------------------------
#
# Every call is one sequential blocking HTTPS round trip, so a loop over a large
# plan set turns into thousands of them and looks exactly like a hang: the
# subprocess produces no output and run_python has no timeout. These lines go
# to stderr, which lands in the tool result, the agent log and `docker compose
# logs`, so a runaway loop is visible while it is still running.
#
# Set K8S_MANAGER_DEBUG=0 to silence.

_DEBUG = os.getenv("K8S_MANAGER_DEBUG", "1").strip().lower() not in {"0", "false", "no", "off"}
_REQUEST_COUNT = 0
_REQUEST_SECONDS = 0.0
_REPEAT_WARNED = set()
_SEEN = {}


def _debug(message: str) -> None:
    if _DEBUG:
        print(f"[k8s-manager] {message}", file=sys.stderr, flush=True)


def _debug_request_start(method: str, url: str) -> float:
    """Log the outgoing request and warn when one is being repeated a lot."""
    global _REQUEST_COUNT
    _REQUEST_COUNT += 1
    _debug(f"#{_REQUEST_COUNT} -> {method} {url}")

    key = f"{method} {url}"
    _SEEN[key] = _SEEN.get(key, 0) + 1
    for threshold in (50, 200, 1000):
        if _SEEN[key] == threshold and (key, threshold) not in _REPEAT_WARNED:
            _REPEAT_WARNED.add((key, threshold))
            _debug(
                f"WARNING: {key} has now been sent {threshold} times in this "
                "process. Each one is a separate blocking round trip - you are "
                "probably looping over a plan set. Fetch the state once and "
                "compare in memory instead."
            )
    return time.monotonic()


def _debug_request_end(started: float, outcome: str) -> None:
    global _REQUEST_SECONDS
    elapsed = time.monotonic() - started
    _REQUEST_SECONDS += elapsed
    _debug(
        f"#{_REQUEST_COUNT} <- {outcome} in {elapsed:.2f}s "
        f"(total {_REQUEST_COUNT} requests, {_REQUEST_SECONDS:.1f}s)"
    )


def request_stats() -> dict[str, Any]:
    """How many requests this process has made, and how long they took."""
    return {
        "requests": _REQUEST_COUNT,
        "seconds": round(_REQUEST_SECONDS, 2),
        "distinct": len(_SEEN),
        "status_cache_hits": _CACHE_HITS,
    }


# --- status cache ----------------------------------------------------------
#
# Comparing a plan set against running state calls is_service_deployed once per
# service per plan, and the cluster state is identical for all of them. A
# 2351-plan set means ~16,800 requests where 3 would do, which reads as a hang.
#
# The cache lives for the process only - run_python starts a fresh interpreter
# each turn, so state is never carried between turns - and any write clears it,
# so a deploy followed by a check still sees the truth.
#
# Set K8S_MANAGER_CACHE=0 to disable.

_CACHE_ENABLED = os.getenv("K8S_MANAGER_CACHE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_STATUS_CACHE: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
_CACHE_HITS = 0


def _cached_status(
    service_endpoint: str | None, service_name: str | None
) -> list[dict[str, Any]]:
    """GET /status?name=, reusing the answer within this process."""
    global _CACHE_HITS
    key = (service_endpoint, service_name)
    if _CACHE_ENABLED and key in _STATUS_CACHE:
        _CACHE_HITS += 1
        if _CACHE_HITS in (50, 500, 5000):
            _debug(
                f"status cache has served {_CACHE_HITS} repeat lookups; "
                "the cluster state is being re-checked in a loop"
            )
        return _STATUS_CACHE[key]

    payload = _request(
        "GET", service_endpoint, "status", params={"name": service_name}
    )
    instances = _instances_from_payload(payload)
    if _CACHE_ENABLED:
        _STATUS_CACHE[key] = instances
    return instances


def invalidate_status_cache() -> None:
    """Drop cached cluster state. Called automatically after every write."""
    _STATUS_CACHE.clear()


def _request(
    method: str,
    service_endpoint: str | None,
    relative_path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """Perform one request and return parsed JSON, or None for an empty body.

    DELETE /instances/{name} answers 204 with no body, so an empty response is
    a success case and must not be handed to json.loads.
    """
    # print with colour
    print(f"\033[1;34mDEBUG: _request({method}, {service_endpoint}, {relative_path}, {params}, {body})\033[0m", file=sys.stderr)
    url = _build_url(service_endpoint, relative_path, params)
    data = None
    headers = {"Accept": "application/json"}
    headers.update(_auth_header())
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=headers, method=method)
    started = _debug_request_start(method, url)
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read().decode(charset)
            status = getattr(response, "status", None) or getattr(response, "code", "?")
            _debug_request_end(started, f"{status} {len(raw)}B")
    except HTTPError as exc:
        _debug_request_end(started, f"HTTP {exc.code}")
        raw_error = exc.read().decode("utf-8", errors="replace")
        detail = raw_error
        try:
            parsed = json.loads(raw_error)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            detail = str(parsed.get("detail") or parsed.get("error") or raw_error)
        if exc.code in (401, 403):
            # Two different gates can answer 401, and confusing them wastes a
            # lot of time. The ingress challenges with WWW-Authenticate: Basic;
            # the application rejects a missing/wrong X-Admin-Key without one.
            # Never echo a secret, only whether it is set.
            challenge = (exc.headers.get("WWW-Authenticate") or "").lower()
            if "basic" in challenge:
                configured = bool(K8S_MANAGER_USERNAME and K8S_MANAGER_PASSWORD)
                detail = (
                    "ingress rejected HTTP Basic auth; "
                    "K8S_MANAGER_USERNAME/K8S_MANAGER_PASSWORD "
                    + ("are set but wrong" if configured else "are not set")
                )
            else:
                configured = bool(K8S_MANAGER_ADMIN_KEY)
                detail = (
                    f"{method} {relative_path} needs the admin API key; "
                    "K8S_MANAGER_ADMIN_KEY "
                    + ("is set but rejected" if configured else "is not set")
                )
        raise ApiError(exc.code, detail[:500], url) from exc
    except URLError as exc:
        _debug_request_end(started, f"connection failed: {exc.reason}")
        raise ApiError(0, f"connection failed: {exc.reason}", url) from exc

    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ApiError(0, f"invalid JSON: {raw[:200]}", url) from exc


# --- env value comparison --------------------------------------------------
#
# Real /status.env_vars is a flat dict[str, str] where every value is a string,
# but the encodings vary by variable:
#   OBJECT_TYPE_FILTER  '["TRUCK","CAR"]'   compact JSON
#   APPROACH_ZONES      'L1_N_WS,L2_N_S'    comma separated
#   STRIDE              '1'                 number as string
#   MAKE_MOCK           'False'             bool as string
# Deployed zone IDs are also uppercased, while the catalogue publishes them
# mixed-case ('C5_w'). Both sides go through the same canonical form so that a
# Python list, a JSON array string and a comma-separated string compare equal.


def _canonical_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    lowered = text.casefold()
    if lowered in {"true", "false"}:
        return lowered
    try:
        number = float(text)
    except ValueError:
        return lowered
    return repr(number)


def _split_tokens(value: Any) -> list[str]:
    """Split a value into comparable tokens.

    Handles native lists, JSON array strings and comma-separated strings. A
    plain scalar becomes a single token, so "CAR" and '["CAR"]' agree.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in text.split(",") if item.strip()]
    return [str(value).strip()]


def _canonical_env_value(value: Any) -> frozenset[str]:
    return frozenset(_canonical_scalar(token) for token in _split_tokens(value))


def env_values_match(expected_value: Any, actual_value: Any) -> bool:
    """True when two env var values mean the same thing."""
    return _canonical_env_value(expected_value) == _canonical_env_value(actual_value)


def _env_subset_matches(
    expected_env: dict[str, Any], actual_env: dict[str, Any]
) -> bool:
    """Subset comparison: only keys present in `expected_env` are checked.

    This is deliberate, not an oversight. POST /instances merges the submitted
    env_vars with chart defaults (and frozen vars) from the Helm registry, so
    GET /status reports the merged superset and comparing full dicts would
    never match. Note that a catalogue default of null is omitted from the
    merged result entirely, so an expected key whose value is null can never
    be found.
    """
    for key, expected_value in expected_env.items():
        if key not in actual_env:
            return False
        if not env_values_match(expected_value, actual_env[key]):
            return False
    return True


# --- status ----------------------------------------------------------------


def _instances_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Normalize both /status shapes into a list.

    GET /status and GET /status?name= return {"instances": [...], "total": N}.
    GET /status/{instance_id} returns a bare instance object. Calling
    .get("instances", []) on the bare shape silently yields [], which reads as
    "nothing is deployed" rather than as an error.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if "instances" in payload:
            instances = payload.get("instances") or []
            return [item for item in instances if isinstance(item, dict)]
        if "instance_name" in payload:
            return [payload]
    return []


def instance_is_ready(instance: dict[str, Any]) -> bool:
    """Readiness per the real contract.

    deployment_status is "Available" or "Degraded" on real k8s-manager, and
    every pod must be ready and Running. Instances scaled to zero report
    pods == [], which is not ready.
    """
    if str(instance.get("deployment_status", "")).casefold() != "available":
        return False
    pods = instance.get("pods") or []
    if not pods:
        return False
    for pod in pods:
        if not bool(pod.get("ready")):
            return False
        if str(pod.get("phase", "")).casefold() != "running":
            return False
    return True


def _compact_instance(
    instance: dict[str, Any], env_keys: list[str] | None = None
) -> dict[str, Any]:
    """Small instance summary; the full object is 11 keys including metrics."""
    actual_env = instance.get("env_vars") or {}
    if env_keys is None:
        shown_env = actual_env
    else:
        shown_env = {key: actual_env[key] for key in env_keys if key in actual_env}
    return {
        "instance_name": instance.get("instance_name"),
        "service_name": instance.get("service_name"),
        "deployment_status": instance.get("deployment_status"),
        "node": instance.get("node"),
        "ready": instance_is_ready(instance),
        "env_vars": shown_env,
    }


def get_health(*, service_endpoint: str | None = None) -> dict[str, Any]:
    """GET /health - pre-flight check that k8s-manager is reachable."""
    return _request("GET", service_endpoint, "health") or {}


def get_running_instances(
    service_name: str | None = None,
    short_info: bool = False,
    *,
    service_endpoint: str | None = None,
) -> dict[str, Any]:
    """GET /status, optionally filtered to one service.

    The only filter the real API defines is `name`. `short_info` omits
    env_vars, kafka_topics, ram_memory_usage_mb and frequency_metrics.
    """
    if short_info:
        # short_info returns a different field set, so it bypasses the cache.
        payload = _request(
            "GET",
            service_endpoint,
            "status",
            params={"name": service_name, "short_info": True},
        )
        instances = _instances_from_payload(payload)
    else:
        instances = _cached_status(service_endpoint, service_name)
    compact = []
    for item in instances:
        compact.append(
            {
                "instance_name": item.get("instance_name"),
                "service_name": item.get("service_name"),
                "deployment_status": item.get("deployment_status"),
                "ready": instance_is_ready(item),
                "env_vars": item.get("env_vars") or {},
            }
        )
    return {"instances": compact, "total": len(compact)}


# --- catalogue -------------------------------------------------------------


def get_deployable_services_basic(
    *,
    service_endpoint: str | None = None,
) -> dict[str, Any]:
    """Fetch deployable services and return only name/description/dependencies.

    Uses short_info=true: the full catalogue carries every env var for every
    service and is far larger than the selection step needs.

    Returns:
        {"services": [{"name", "description", "dependencies"}], "total": int}
    """
    raw_payload = _request(
        "GET", service_endpoint, "info/services", params={"short_info": True}
    )
    services = (raw_payload or {}).get("services", []) or []
    compact_services = []
    for item in services:
        compact_services.append(
            {
                "name": item.get("name"),
                "description": item.get("description"),
                "dependencies": item.get("dependencies", []) or [],
            }
        )
    return {"services": compact_services, "total": len(compact_services)}


def get_service_env_schema(
    service_name: str, *, service_endpoint: str | None = None
) -> dict[str, Any]:
    """Fetch env var metadata for one service from GET /info/{service_name}.

    The catalogue publishes THREE arrays. frozen_env_vars are set by the chart
    and must not be submitted, but they do appear in GET /status.env_vars, so
    they are returned here to explain values that would otherwise look like
    unexplained extras.

    Each entry has: name, type (a Python type name as a string), description,
    value (the default), required, possible_values (list | null). `required` is
    false on both optional and frozen entries, so array membership is the only
    way to tell them apart.

    Returns:
        {"name", "required_env_vars", "optional_env_vars", "frozen_env_vars"}
    """
    if not service_name:
        raise ValueError("service_name is required")
    try:
        item = _request("GET", service_endpoint, f"info/{service_name}")
    except ApiError as exc:
        if exc.status == 404:
            raise ValueError(f"Service not found: {service_name}") from exc
        raise
    if not isinstance(item, dict):
        raise ValueError(f"Unexpected /info/{service_name} response shape")
    return {
        "name": item.get("name", service_name),
        "required_env_vars": item.get("required_env_vars", []) or [],
        "optional_env_vars": item.get("optional_env_vars", []) or [],
        "frozen_env_vars": item.get("frozen_env_vars", []) or [],
    }


def is_service_deployed(
    expected: dict[str, Any], *, service_endpoint: str | None = None
) -> dict[str, Any]:
    """Check whether a deployed instance matches the expected env var values.

    Args:
        expected: {"service_name": str, "env_vars": {str: Any}}
        service_endpoint: optional override; defaults to K8S_MANAGER_URL.

    Returns:
        {"service_name": str, "is_deployed": bool, "matched_instance": dict | None}
    """
    service_name = expected.get("service_name")
    if not service_name:
        raise ValueError("expected.service_name is required")

    expected_env = expected.get("env_vars", {}) or {}
    if not isinstance(expected_env, dict):
        raise ValueError("expected.env_vars must be an object/dict")

    instances = _cached_status(service_endpoint, service_name)

    for instance in instances:
        if instance.get("service_name") != service_name:
            continue
        actual_env = instance.get("env_vars", {}) or {}
        if _env_subset_matches(expected_env, actual_env):
            return {
                "service_name": service_name,
                "is_deployed": True,
                "matched_instance": _compact_instance(
                    instance, list(expected_env.keys())
                ),
            }

    return {
        "service_name": service_name,
        "is_deployed": False,
        "matched_instance": None,
    }


# --- instances (write) -----------------------------------------------------


def _stringify_env(env_vars: dict[str, Any]) -> dict[str, str]:
    """Render env values the way the API stores them: all strings, with lists
    as compact JSON, matching what GET /status reports back."""
    rendered: dict[str, str] = {}
    for key, value in env_vars.items():
        if isinstance(value, (list, tuple, set, frozenset)):
            rendered[key] = json.dumps(
                [str(item) for item in value], separators=(",", ":")
            )
        elif isinstance(value, bool):
            rendered[key] = "True" if value else "False"
        else:
            rendered[key] = str(value)
    return rendered


def experiment_prefix() -> str:
    """A short id that tags every instance created in this session.

    Instance names become `<prefix>-<service>-<hash>`, so everything deployed
    by one experiment can be found and cleaned up together.

    K8S_MANAGER_EXPERIMENT wins if set. Otherwise one is generated and cached
    beside this module: run_python starts a fresh interpreter per snippet, so
    without the file every call would invent a different prefix.
    """
    configured = os.getenv("K8S_MANAGER_EXPERIMENT", "").strip()
    if configured:
        return _safe_prefix(configured)

    marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".experiment_id")
    try:
        with open(marker, encoding="utf-8") as handle:
            cached = handle.read().strip()
        if cached:
            return _safe_prefix(cached)
    except OSError:
        pass

    generated = "exp" + secrets.token_hex(3)
    try:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(generated)
    except OSError:
        pass  # non-fatal: worst case each process picks its own
    return generated


def _safe_prefix(value: str) -> str:
    """Instance names are DNS labels: lowercase alphanumerics and dashes."""
    cleaned = "".join(c if c.isalnum() else "-" for c in value.lower())
    cleaned = cleaned.strip("-")
    return cleaned[:20] or "exp"


def create_instance(
    service_name: str,
    env_vars: dict[str, Any] | None = None,
    node_name: str | None = None,
    instance_prefix: str | None = None,
    metrics_trackable: bool = True,
    *,
    service_endpoint: str | None = None,
) -> dict[str, Any]:
    """POST /instances.

    Submitted env_vars are merged with chart defaults, explicit values winning.
    metrics_trackable injects the Kafka metrics-proxy sidecar and must stay
    true for anything downstream to observe the instance.

    Returns the API's 5-key response: instance_name, service_name, namespace,
    image, metrics_trackable.
    """
    if not service_name:
        raise ValueError("service_name is required")
    body: dict[str, Any] = {
        "service_name": service_name,
        "env_vars": _stringify_env(env_vars or {}),
        "metrics_trackable": metrics_trackable,
    }
    if node_name:
        body["node_name"] = node_name
    body["instance_prefix"] = _safe_prefix(instance_prefix or experiment_prefix())
    created = _request("POST", service_endpoint, "instances", body=body) or {}
    invalidate_status_cache()
    return created


def update_instance(
    instance_name: str,
    env_vars: dict[str, Any] | None = None,
    *,
    service_endpoint: str | None = None,
) -> dict[str, Any]:
    """PUT /instances/{instance_name}.

    With env_vars: replaces the configuration and rolling-restarts. Without:
    a pure rolling restart. Image and service identity never change. Preferred
    over DELETE + POST for reconfiguration - there is no window in which the
    service is absent, and the instance name survives.
    """
    if not instance_name:
        raise ValueError("instance_name is required")
    body: dict[str, Any] = {}
    if env_vars is not None:
        body["env_vars"] = _stringify_env(env_vars)
    result = _request(
        "PUT", service_endpoint, f"instances/{instance_name}", body=body
    )
    invalidate_status_cache()
    return result or {}


def delete_instance(
    instance_name: str, *, service_endpoint: str | None = None
) -> dict[str, Any]:
    """DELETE /instances/{instance_name}. Answers 204 with no body."""
    if not instance_name:
        raise ValueError("instance_name is required")
    _request("DELETE", service_endpoint, f"instances/{instance_name}")
    invalidate_status_cache()
    return {"instance_name": instance_name, "deleted": True}
