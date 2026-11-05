"""Deployment planner: fetches running services and picks the optimal plan.

Usage:
    python deployment_planner.py                                  # demo with sample running services
    python deployment_planner.py --url http://orchestrator:8080/api
    python deployment_planner.py --url http://orchestrator:8080/api --endpoint services --strict
    python deployment_planner.py --url ... --token MY_TOKEN --out plan.json
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
from itertools import combinations
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_PLANS = 10_000
DETECTION_SERVICE = "object_in_zone_detection"

# Action names emitted in plans. Adjust these if your deployer uses other verbs.
ACTION_NEW = "new_instance"
ACTION_REUSE = "use_existing"
ACTION_REMOVE = "remove_instance"

RUNNING_STATUSES = frozenset({"running", "up", "healthy", "active", "started"})
TYPE_ENV_KEYS = ("OBJECT_TYPE_FILTER", "OBJECT_TYPES")
ZONE_ENV_KEYS = ("ZONES_OF_INTEREST", "APPROACH_ZONES", "CROSSWALK_ZONE", "ZONE")
LIST_ENV_KEYS = frozenset(TYPE_ENV_KEYS + ZONE_ENV_KEYS)

VEHICLE_TYPES: list[str] = []
APPROACH_ZONES: list[str] = []
PEDESTRIAN_TYPES: list[str] = []
CROSSWALK_ZONES: list[str] = []

Edge = tuple[str, str]


class RunningServicesError(RuntimeError):
    """Raised when the list of running services cannot be fetched or parsed."""


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _upper_set(values: Any) -> set[str]:
    return {item.upper() for item in _as_list(values)}


def _is_crosswalk_zone(zone: str) -> bool:
    return zone.upper().startswith("C")


def _is_pedestrian(object_type: str) -> bool:
    return object_type.upper() == "PERSON"


# --------------------------------------------------------------------------- #
# Scenario derivation
# --------------------------------------------------------------------------- #
def _derive_scenario(
    fixed_services: list[dict[str, Any]] | None = None,
    service_variants: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    object_types: set[str] = set()
    zones: set[str] = set()

    services_to_scan: list[dict[str, Any]] = list(fixed_services or [])
    for candidates in (service_variants or {}).values():
        services_to_scan.extend(candidates or [])

    for service in services_to_scan:
        env_vars = (service.get("env_vars") or {}) if isinstance(service, dict) else {}
        for key in TYPE_ENV_KEYS:
            object_types |= _upper_set(env_vars.get(key))
        for key in ZONE_ENV_KEYS:
            zones |= _upper_set(env_vars.get(key))

    vehicle_types = sorted(t for t in object_types if not _is_pedestrian(t))
    pedestrian_types = sorted(t for t in object_types if _is_pedestrian(t))
    crosswalk_zones = sorted(z for z in zones if _is_crosswalk_zone(z))
    approach_zones = sorted(z for z in zones if not _is_crosswalk_zone(z))

    need: set[Edge] = {(t, z) for t in vehicle_types for z in approach_zones}
    need |= {(t, z) for t in pedestrian_types for z in crosswalk_zones}

    return {
        "types": vehicle_types + pedestrian_types,
        "zones": approach_zones + crosswalk_zones,
        "need": need,
    }


def _build_need(
    types: list[str] | None = None, zones: list[str] | None = None
) -> set[Edge]:
    type_list = list(types or (VEHICLE_TYPES + PEDESTRIAN_TYPES))
    zone_list = list(zones or (APPROACH_ZONES + CROSSWALK_ZONES))
    need: set[Edge] = set()
    for t in type_list:
        for z in zone_list:
            pedestrian_pair = _is_pedestrian(t) and z.upper().startswith("C")
            vehicle_pair = (not _is_pedestrian(t)) and z.upper().startswith("L")
            if pedestrian_pair or vehicle_pair:
                need.add((t, z))
    return need


# --------------------------------------------------------------------------- #
# Exact-cover search
# --------------------------------------------------------------------------- #
def all_nonempty_subsets(items: list[str]) -> list[frozenset[str]]:
    subsets: list[frozenset[str]] = []
    for r in range(1, len(items) + 1):
        for combo in combinations(items, r):
            subsets.append(frozenset(combo))
    return subsets


def instance_coverage(
    group_types: frozenset[str],
    group_zones: frozenset[str],
    need: set[Edge] | None = None,
) -> frozenset[Edge]:
    active_need = need if need is not None else _build_need()
    return frozenset(
        (t, z) for t in group_types for z in group_zones if (t, z) in active_need
    )


def spurious_pairs(
    group_types: frozenset[str], group_zones: frozenset[str], need: set[Edge]
) -> int:
    """Pairs an instance would detect that nobody asked for (e.g. PERSON in a lane)."""
    return sum(1 for t in group_types for z in group_zones if (t, z) not in need)


def is_valid_instance(
    group_types: frozenset[str],
    group_zones: frozenset[str],
    need: set[Edge] | None = None,
    strict: bool = False,
) -> bool:
    active_need = need if need is not None else _build_need()
    if strict and spurious_pairs(group_types, group_zones, active_need):
        return False
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
    need: set[Edge] | None = None,
    strict: bool = False,
) -> list[dict[str, Any]]:
    types = list(types or (VEHICLE_TYPES + PEDESTRIAN_TYPES))
    zones = list(zones or (APPROACH_ZONES + CROSSWALK_ZONES))
    active_need = need if need is not None else _build_need(types, zones)
    valid: list[dict[str, Any]] = []
    for group_types in all_nonempty_subsets(types):
        for group_zones in all_nonempty_subsets(zones):
            if is_valid_instance(group_types, group_zones, active_need, strict):
                valid.append(
                    {
                        "types": group_types,
                        "zones": group_zones,
                        "covers": instance_coverage(
                            group_types, group_zones, active_need
                        ),
                        "spurious": spurious_pairs(
                            group_types, group_zones, active_need
                        ),
                    }
                )
    return valid


def find_all_plans(
    valid_instances: list[dict[str, Any]],
    need: set[Edge],
    max_plans: int | None = DEFAULT_MAX_PLANS,
) -> list[list[dict[str, Any]]]:
    """Enumerate exact covers. Stops after ``max_plans`` (None = unlimited)."""
    need = frozenset(need)
    by_edge = {
        edge: [inst for inst in valid_instances if edge in inst["covers"]]
        for edge in need
    }
    plans: list[list[dict[str, Any]]] = []
    truncated = False

    def backtrack(remaining: frozenset[Edge], chosen: list[dict[str, Any]]) -> bool:
        nonlocal truncated
        if not remaining:
            plans.append(list(chosen))
            if max_plans is not None and len(plans) >= max_plans:
                truncated = True
                return False
            return True
        # Deterministic tie-break so plan numbering is stable between runs.
        edge = min(remaining, key=lambda item: (len(by_edge[item]), item))
        for inst in by_edge[edge]:
            if inst["covers"] <= remaining:
                chosen.append(inst)
                keep_going = backtrack(remaining - inst["covers"], chosen)
                chosen.pop()
                if not keep_going:
                    return False
        return True

    backtrack(need, [])
    if truncated:
        logger.warning(
            "Plan enumeration stopped at max_plans=%s; results are not exhaustive",
            max_plans,
        )
    return plans


# --------------------------------------------------------------------------- #
# Input normalisation
# --------------------------------------------------------------------------- #
def _normalize_service_entry(service: Any) -> dict[str, Any]:
    if isinstance(service, str):
        return {"service_name": service, "action": ACTION_NEW, "env_vars": {}}
    if not isinstance(service, dict):
        raise ValueError(
            f"Service entries must be dicts or strings, got {type(service).__name__}"
        )

    normalized = copy.deepcopy(service)
    normalized.setdefault("action", ACTION_NEW)
    normalized["env_vars"] = normalized.get("env_vars") or {}
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
        # Captain style: list of variant groups, each a list of service dicts.
        if service_variants and all(isinstance(g, list) for g in service_variants):
            flat = [item for group in service_variants for item in group]
        else:
            flat = list(service_variants)
        for item in flat:
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


# --------------------------------------------------------------------------- #
# Plan generation
# --------------------------------------------------------------------------- #
def _enumerate_plans(
    fixed_services: Any,
    service_variants: Any,
    max_plans: int | None,
    strict: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    normalized_fixed = _normalize_fixed_services(fixed_services)
    normalized_variants = _normalize_service_variants(service_variants)
    logger.debug("fixed=%s variants=%s", normalized_fixed, normalized_variants)
    if not normalized_variants:
        return [], normalized_fixed, normalized_variants

    scenario = _derive_scenario(normalized_fixed, normalized_variants)
    if not scenario["types"] or not scenario["zones"] or not scenario["need"]:
        return [], normalized_fixed, normalized_variants

    valid_instances = find_valid_instances(
        scenario["types"], scenario["zones"], scenario["need"], strict=strict
    )
    raw_plans = find_all_plans(valid_instances, scenario["need"], max_plans)
    logger.info(
        "%d plans generated from %d valid instances",
        len(raw_plans),
        len(valid_instances),
    )

    generated: list[dict[str, Any]] = []
    for index, plan in enumerate(raw_plans, start=1):
        plan_services = [
            {
                "service_name": entry["service_name"],
                "action": entry.get("action", ACTION_NEW),
                "env_vars": copy.deepcopy(entry.get("env_vars", {})),
            }
            for entry in normalized_fixed
        ]
        for inst in plan:
            plan_services.append(
                {
                    "service_name": DETECTION_SERVICE,
                    "action": ACTION_NEW,
                    "env_vars": {
                        "OBJECT_TYPE_FILTER": ",".join(sorted(inst["types"])),
                        "ZONES_OF_INTEREST": ",".join(sorted(inst["zones"])),
                    },
                }
            )
        plan_services.sort(
            key=lambda item: (item["service_name"], _stable_json(item["env_vars"]))
        )
        generated.append(
            {
                "plan_name": f"Alternative {index}",
                "services": plan_services,
                "_spurious_pairs": sum(inst["spurious"] for inst in plan),
            }
        )
    return generated, normalized_fixed, normalized_variants


def generate_deployment_plans(
    fixed_services: list[dict[str, Any]] | None = None,
    service_variants: dict[str, list[dict[str, Any]]] | None = None,
    max_plans: int | None = DEFAULT_MAX_PLANS,
    strict: bool = False,
) -> dict[str, Any]:
    """Generate every exact-cover deployment plan for the required type/zone pairs.

    - enumerate every valid, non-vacuous instance shape
    - every plan covers all required (type, zone) pairs exactly once
    - ``strict=True`` also rejects instances that would detect unrequested pairs
      (e.g. a CAR+PERSON instance on L1+C1 also watches CAR@C1 and PERSON@L1)

    Accepts the canonical dict form or the captain's list-based form.
    """
    plans, _, _ = _enumerate_plans(fixed_services, service_variants, max_plans, strict)
    for plan in plans:
        plan.pop("_spurious_pairs", None)
    return {"plans": plans}


# --------------------------------------------------------------------------- #
# Running services
# --------------------------------------------------------------------------- #
def _parse_env(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):  # Docker style ["KEY=value", ...]
        env: dict[str, Any] = {}
        for item in raw:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                env[key] = value
            elif isinstance(item, dict) and "name" in item:  # k8s style
                env[item["name"]] = item.get("value")
        return env
    return {}


def _status_of(raw: dict[str, Any]) -> str | None:
    status = raw.get("status", raw.get("state", raw.get("State")))
    if isinstance(status, dict):
        status = status.get("status") or status.get("Status") or status.get("state")
    return str(status).strip().lower() if status is not None else None


def _is_running(status: str | None) -> bool:
    if status is None:  # endpoint only lists live instances
        return True
    return status in RUNNING_STATUSES or status.startswith("up")


def _extract_service_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("services", "instances", "data", "items", "results"):
            if key in payload:
                return _extract_service_list(payload[key])
        # {"name": {...}, "name2": {...}}
        if payload and all(isinstance(v, dict) for v in payload.values()):
            return [{"service_name": k, **v} for k, v in payload.items()]
    raise RunningServicesError(
        f"Unrecognised running-services payload: {type(payload).__name__}"
    )


def parse_running_services(
    payload: Any, include_stopped: bool = False
) -> list[dict[str, Any]]:
    """Normalise an API payload into [{service_name, instance_id, env_vars, status}]."""
    services: list[dict[str, Any]] = []
    for raw in _extract_service_list(payload):
        if isinstance(raw, str):
            raw = {"service_name": raw}
        if not isinstance(raw, dict):
            continue
        name = raw.get("service_name") or raw.get("name") or raw.get("service")
        if not name:
            logger.debug("Skipping running entry without a name: %s", raw)
            continue
        status = _status_of(raw)
        if not include_stopped and not _is_running(status):
            continue
        env = _parse_env(
            raw.get(
                "env_vars", raw.get("env", raw.get("environment", raw.get("config")))
            )
        )
        services.append(
            {
                "service_name": str(name),
                "instance_id": raw.get("instance_id")
                or raw.get("id")
                or raw.get("container_id")
                or raw.get("uuid"),
                "env_vars": env,
                "status": status or "running",
            }
        )
    return services


def fetch_running_services(
    base_url: str,
    endpoint: str = "services",
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    headers: dict[str, str] | None = None,
    include_stopped: bool = False,
) -> list[dict[str, Any]]:
    """GET the list of running services from the orchestrator API."""
    url = urljoin(base_url.rstrip("/") + "/", endpoint.lstrip("/"))
    request = Request(
        url, headers={"Accept": "application/json", **(headers or {})}, method="GET"
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read().decode(charset)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise RunningServicesError(f"GET {url} -> HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RunningServicesError(f"GET {url} failed: {exc.reason}") from exc
    except OSError as exc:  # timeouts, connection resets
        raise RunningServicesError(f"GET {url} failed: {exc}") from exc

    try:
        payload = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as exc:
        raise RunningServicesError(f"GET {url} returned invalid JSON: {exc}") from exc
    return parse_running_services(payload, include_stopped=include_stopped)


# --------------------------------------------------------------------------- #
# Plan vs. running-state diff and optimisation
# --------------------------------------------------------------------------- #
def _normalize_env_value(key: str, value: Any) -> str:
    if key.upper() in LIST_ENV_KEYS:
        return ",".join(sorted(_upper_set(value)))
    return str(value).strip()


def _env_matches(planned: dict[str, Any], running: dict[str, Any], exact: bool) -> bool:
    """True if every planned env var has the same value on the running instance."""
    running_norm = {k: _normalize_env_value(k, v) for k, v in running.items()}
    for key, value in planned.items():
        if running_norm.get(key) != _normalize_env_value(key, value):
            return False
    if exact:
        # Detection instances must not carry extra type/zone filters.
        for key in LIST_ENV_KEYS & set(running_norm):
            if key not in planned and running_norm[key]:
                return False
    return True


def diff_plan_against_running(
    plan_services: list[dict[str, Any]],
    running_services: list[dict[str, Any]],
    managed_service_names: set[str],
) -> dict[str, Any]:
    """Mark reusable instances and list running managed instances to remove."""
    used = [False] * len(running_services)
    actions: list[dict[str, Any]] = []

    for service in plan_services:
        entry = copy.deepcopy(service)
        if entry.get("action", ACTION_NEW) == ACTION_NEW:
            exact = entry["service_name"] == DETECTION_SERVICE
            for idx, running in enumerate(running_services):
                if (
                    not used[idx]
                    and running["service_name"] == entry["service_name"]
                    and _env_matches(entry["env_vars"], running["env_vars"], exact)
                ):
                    used[idx] = True
                    entry["action"] = ACTION_REUSE
                    entry["instance_id"] = running.get("instance_id")
                    break
        actions.append(entry)

    removals = [
        {
            "service_name": running["service_name"],
            "action": ACTION_REMOVE,
            "instance_id": running.get("instance_id"),
            "env_vars": running["env_vars"],
        }
        for idx, running in enumerate(running_services)
        if not used[idx] and running["service_name"] in managed_service_names
    ]
    return {"services": actions, "removals": removals}


def _plan_cost(diffed: dict[str, Any], spurious: int) -> dict[str, int]:
    services = diffed["services"]
    return {
        "new_instances": sum(1 for s in services if s["action"] == ACTION_NEW),
        "reused_instances": sum(1 for s in services if s["action"] == ACTION_REUSE),
        "removed_instances": len(diffed["removals"]),
        "total_instances": len(services),
        "spurious_pairs": spurious,
    }


def _cost_key(cost: dict[str, int]) -> tuple[int, ...]:
    # Fewest deployments, then fewest teardowns, then smallest footprint,
    # then least over-detection.
    return (
        cost["new_instances"] + cost["removed_instances"],
        cost["new_instances"],
        cost["total_instances"],
        cost["spurious_pairs"],
    )


def get_optimized_deployment_plan(
    fixed_services: Any = None,
    service_variants: Any = None,
    *,
    running_services: list[dict[str, Any]] | None = None,
    base_url: str | None = None,
    endpoint: str = "services",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    headers: dict[str, str] | None = None,
    max_plans: int | None = DEFAULT_MAX_PLANS,
    strict: bool = False,
    top_n: int | None = 5,
) -> dict[str, Any]:
    """Pick the deployment plan that changes the least, given what is running now.

    Running services come from ``running_services`` if given, otherwise from
    ``fetch_running_services(base_url, endpoint)``. If neither is available the
    plans are ranked as a fresh deployment.
    """
    warnings: list[str] = []
    if running_services is None and base_url:
        try:
            running_services = fetch_running_services(
                base_url, endpoint, timeout=timeout, headers=headers
            )
        except RunningServicesError as exc:
            logger.warning("Could not fetch running services: %s", exc)
            warnings.append(
                f"Running services unavailable, planned from scratch: {exc}"
            )
    running = (
        parse_running_services(running_services) if running_services is not None else []
    )

    plans, fixed, variants = _enumerate_plans(
        fixed_services, service_variants, max_plans, strict
    )
    if max_plans is not None and len(plans) >= max_plans:
        warnings.append(
            f"Plan search stopped at {max_plans}; result may not be optimal."
        )

    managed = {DETECTION_SERVICE} | {s["service_name"] for s in fixed} | set(variants)

    ranked: list[dict[str, Any]] = []
    for plan in plans:
        diffed = diff_plan_against_running(plan["services"], running, managed)
        cost = _plan_cost(diffed, plan["_spurious_pairs"])
        ranked.append(
            {
                "plan_name": plan["plan_name"],
                "services": sorted(
                    diffed["services"] + diffed["removals"],
                    key=lambda s: (
                        s["service_name"],
                        s["action"],
                        _stable_json(s["env_vars"]),
                    ),
                ),
                "cost": cost,
            }
        )
    ranked.sort(key=lambda p: (_cost_key(p["cost"]), p["plan_name"]))

    return {
        "optimal_plan": ranked[0] if ranked else None,
        "ranked_plans": ranked[:top_n] if top_n else ranked,
        "total_plans_considered": len(ranked),
        "running_services": running,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# Example / command line
# --------------------------------------------------------------------------- #
# 1. What you want deployed --------------------------------------------------
# Services that are always part of the deployment.
FIXED_SERVICES = [
    {"service_name": "camera_ingest", "env_vars": {"CAM": "1"}},
]

# Detection requirements. The planner reads the object types and zones from
# these and works out every way to cover them with object_in_zone_detection
# instances. Zones starting with "C" are crosswalks (PERSON); others are lanes.
SERVICE_VARIANTS = {
    "object_in_zone_detection": [
        {
            "service_name": "object_in_zone_detection",
            "env_vars": {
                "OBJECT_TYPE_FILTER": "CAR,TRUCK",
                "ZONES_OF_INTEREST": "L1,L2",
            },
        },
        {
            "service_name": "object_in_zone_detection",
            "env_vars": {"OBJECT_TYPE_FILTER": "PERSON", "ZONES_OF_INTEREST": "C1"},
        },
    ]
}

# 2. Sample of what is "already running" (only used when no --url is given) ---
SAMPLE_RUNNING = [
    {
        "service_name": "camera_ingest",
        "id": "cam-1",
        "status": "running",
        "env_vars": {"CAM": "1"},
    },
    {
        "service_name": "object_in_zone_detection",
        "id": "det-1",
        "status": "running",
        "env_vars": {"OBJECT_TYPE_FILTER": "CAR,TRUCK", "ZONES_OF_INTEREST": "L1,L2"},
    },
    {
        "service_name": "object_in_zone_detection",
        "id": "det-old",
        "status": "running",
        "env_vars": {"OBJECT_TYPE_FILTER": "BUS", "ZONES_OF_INTEREST": "L9"},
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Optimal deployment plan")
    parser.add_argument(
        "--url", help="Orchestrator base URL, e.g. http://host:8080/api"
    )
    parser.add_argument(
        "--endpoint", default="services", help="Path listing running services"
    )
    parser.add_argument("--token", help="Bearer token, if the API needs one")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Reject instances that detect pairs nobody asked for",
    )
    parser.add_argument(
        "--top", type=int, default=5, help="How many ranked plans to show"
    )
    parser.add_argument("--out", help="Write the optimal plan to this JSON file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    kwargs = {"strict": args.strict, "top_n": args.top}
    if args.url:
        kwargs["base_url"] = args.url
        kwargs["endpoint"] = args.endpoint
        if args.token:
            kwargs["headers"] = {"Authorization": f"Bearer {args.token}"}
    else:
        print("No --url given: using SAMPLE_RUNNING as the current state.\n")
        kwargs["running_services"] = SAMPLE_RUNNING

    result = get_optimized_deployment_plan(FIXED_SERVICES, SERVICE_VARIANTS, **kwargs)

    for warning in result["warnings"]:
        print(f"WARNING: {warning}")

    print(f"Running now: {len(result['running_services'])} services")
    print(f"Plans considered: {result['total_plans_considered']}\n")

    print("Top plans (new + removed = changes):")
    for plan in result["ranked_plans"]:
        c = plan["cost"]
        print(
            f"  {plan['plan_name']:<16} new={c['new_instances']} reuse={c['reused_instances']} "
            f"remove={c['removed_instances']} total={c['total_instances']} "
            f"extra_pairs={c['spurious_pairs']}"
        )

    best = result["optimal_plan"]
    if best is None:
        print("\nNo valid plan found - check SERVICE_VARIANTS.")
        return

    print(f"\nOptimal plan: {best['plan_name']}")
    for s in best["services"]:
        env = ", ".join(f"{k}={v}" for k, v in s["env_vars"].items())
        inst = f" [{s['instance_id']}]" if s.get("instance_id") else ""
        print(f"  {s['action']:<16} {s['service_name']}{inst}  {env}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(best, fh, indent=2)
        print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()
