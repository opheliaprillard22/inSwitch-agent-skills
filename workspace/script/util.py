"""Utility functions for k8s_manager API calls used by this skill."""

from __future__ import annotations

import copy
import json
from itertools import combinations
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

DEFAULT_TIMEOUT_SECONDS = 30

VEHICLE_TYPES = []
APPROACH_ZONES = []
PEDESTRIAN_TYPES = []
CROSSWALK_ZONES = []


def _derive_scenario(
    fixed_services: list[dict[str, Any]] | None = None,
    service_variants: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    object_types: set[str] = set()
    zones: set[str] = set()

    services_to_scan: list[dict[str, Any]] = []
    services_to_scan.extend(fixed_services or [])
    for candidates in (service_variants or {}).values():
        services_to_scan.extend(candidates or [])

    for service in services_to_scan:
        env_vars = (service.get("env_vars") or {}) if isinstance(service, dict) else {}
        object_types.update(
            item.upper()
            for item in _as_list(env_vars.get("OBJECT_TYPE_FILTER"))
            if item and item.strip()
        )
        object_types.update(
            item.upper()
            for item in _as_list(env_vars.get("OBJECT_TYPES"))
            if item and item.strip()
        )
        zones.update(
            item.upper()
            for item in _as_list(env_vars.get("ZONES_OF_INTEREST"))
            if item and item.strip()
        )
        zones.update(
            item.upper()
            for item in _as_list(env_vars.get("APPROACH_ZONES"))
            if item and item.strip()
        )
        zones.update(
            item.upper()
            for item in _as_list(env_vars.get("CROSSWALK_ZONE"))
            if item and item.strip()
        )
        zones.update(
            item.upper()
            for item in _as_list(env_vars.get("ZONE"))
            if item and item.strip()
        )

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
    print("TEST DEBUG", fixed_services, service_variants)
    normalized_fixed = _normalize_fixed_services(fixed_services)
    normalized_variants = _normalize_service_variants(service_variants)
    print("TEST DEBUG", normalized_variants)
    if not normalized_variants:
        return {"plans": []}

    scenario = _derive_scenario(normalized_fixed, normalized_variants)
    if not scenario["types"] or not scenario["zones"] or not scenario["need"]:
        return {"plans": []}
    valid_instances = find_valid_instances(
        scenario["types"], scenario["zones"], scenario["need"]
    )
    plans = find_all_plans(valid_instances, scenario["need"])
    print(
        f"TEST DEBUG: {len(plans)} plans generated for {len(valid_instances)} valid instances"
    )
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
                    "service_name": "object_in_zone_detection",
                    "action": "new_instance",
                    "env_vars": {
                        "OBJECT_TYPE_FILTER": ",".join(sorted(inst["types"])),
                        "ZONES_OF_INTEREST": ",".join(sorted(inst["zones"])),
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


DEFAULT_TIMEOUT_SECONDS = 30


def _http_get_json(service_endpoint: str, relative_path: str) -> dict[str, Any]:
    base = service_endpoint.rstrip("/") + "/"
    url = urljoin(base, relative_path.lstrip("/"))
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def get_deployable_services_basic(service_endpoint: str) -> dict[str, Any]:
    """Fetch deployable services and return only name/description/dependencies.

    Args:
        service_endpoint: Base API endpoint, for example "http://host.docker.internal:8001".

    Returns:
        JSON-compatible dictionary:
        {
          "services": [
            {"name": str, "description": str, "dependencies": list[str]},
            ...
          ],
          "total": int
        }
    """

    raw_payload = _http_get_json(service_endpoint, "info/services")

    services = raw_payload.get("services", [])
    compact_services: list[dict[str, Any]] = []
    for item in services:
        compact_services.append(
            {
                "name": item.get("name"),
                "description": item.get("description"),
                "dependencies": item.get("dependencies", []) or [],
            }
        )

    return {"services": compact_services, "total": len(compact_services)}


def get_service_env_schema(service_endpoint: str, service_name: str) -> dict[str, Any]:
    """Fetch required/optional env var metadata for a specific service.

    Args:
        service_endpoint: Base API endpoint, for example "http://host.docker.internal:8001".
        service_name: Service name to look up.

    Returns:
        JSON-compatible dictionary:
        {
          "name": str,
          "required_env_vars": list[dict],
          "optional_env_vars": list[dict]
        }
    """

    raw_payload = _http_get_json(service_endpoint, "info/services")
    for item in raw_payload.get("services", []):
        if item.get("name") == service_name:
            return {
                "name": service_name,
                "required_env_vars": item.get("required_env_vars", []) or [],
                "optional_env_vars": item.get("optional_env_vars", []) or [],
            }
    raise ValueError(f"Service not found in /info/services: {service_name}")


def is_service_deployed(
    service_endpoint: str, expected: dict[str, Any]
) -> dict[str, Any]:
    """Check whether a deployed instance matches the expected env var values.

    Args:
        service_endpoint: Base API endpoint, for example "http://host.docker.internal:8001".
        expected: JSON-like dict with:
            {
              "service_name": str,
              "env_vars": {str: Any}
            }

    Returns:
        JSON-compatible dictionary:
        {
          "service_name": str,
          "is_deployed": bool,
          "matched_instance": dict | None
        }
    """

    service_name = expected.get("service_name")
    if not service_name:
        raise ValueError("expected.service_name is required")

    expected_env = expected.get("env_vars", {}) or {}
    if not isinstance(expected_env, dict):
        raise ValueError("expected.env_vars must be an object/dict")

    status_candidates = [
        f"status/name={service_name}",
        f"status?service_name={service_name}",
        f"status?name={service_name}",
    ]

    payload: dict[str, Any] | None = None
    last_error: Exception | None = None
    for path in status_candidates:
        try:
            payload = _http_get_json(service_endpoint, path)
            break
        except HTTPError as exc:
            last_error = exc
            if exc.code != 404:
                raise
        except Exception as exc:  # pragma: no cover - transport/runtime safety
            last_error = exc

    if payload is None:
        if last_error is not None:
            raise last_error
        raise RuntimeError("Unable to fetch service status")

    expected_str = {k: str(v) for k, v in expected_env.items()}
    for instance in payload.get("instances", []):
        if instance.get("service_name") != service_name:
            continue
        actual_env = instance.get("env_vars", {}) or {}
        actual_str = {k: str(v) for k, v in actual_env.items()}
        if all(actual_str.get(key) == value for key, value in expected_str.items()):
            return {
                "service_name": service_name,
                "is_deployed": True,
                "matched_instance": instance,
            }

    return {
        "service_name": service_name,
        "is_deployed": False,
        "matched_instance": None,
    }
