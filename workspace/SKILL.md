---
name: deploy-service-dfs
description: Execute intent-driven service operations in k8s_manager. Main flow is dependency-first deployment from natural language intent; also supports listing running services and listing available deployable services. Only using the agent to find alternative plans and suggestions. 
---

# Deploy Service DFS

Execute service-management intents directly using the `k8s_manager` HTTP API.

Do not generate code for the user. Execute the workflow.
When a predefined function exists (those functions will be listed below), import and use it instead of directly calling the API.

DEBUG mode is OFF!

These environment variables are set for you as default choices: "KAFKA_SOURCE_OBJECT_IN_ZONE_DETECTION_BOOTSTRAP_SERVER":"10.2.0.163:29092","KAFKA_SOURCE_OBJECT_SPEEDS_BOOTSTRAP_SERVER":"10.2.0.163:29092","KAFKA_DEST_BOOTSTRAP_SERVER":"10.2.0.163:29092",
"KAFKA_SCHEMA_REGISTRY_URL":"http://10.2.0.164:8081"
"OSEF_SOURCE":"tcp://192.168.2.10"

## Capabilities

Understand natural-language intent and choose one capability:

- `Deploy Services` with dependency resolution and user confirmation
- `Show Running Services` inspect deployed instances according to the user's intent
- `Inspect Deployable Services`: inspect deployable services, and show the information relevant to the user's intent

## Predefined Python Utilities

Use these functions from `/workspace/script/util.py` whenever applicable. These functions will be used by the generated Python code. Do not try to read this python file directly.

Import example:

```python
import sys
sys.path.append("/workspace/script")

from util import get_deployable_services_basic
from util import get_service_env_schema
from util import get_running_instances
from util import is_service_deployed
from util import create_instance
from util import update_instance
from util import delete_instance
```

<!-- ### `generate_deployment_plans(fixed_services: list, service_variants: list) -> dict`

Purpose:

- Generate all deployment plans based on the fixed services and service variants.
- Keep the full canonical set of deployment alternatives internally, but only show a shortlist of up to 4 alternatives to the user (show the list of instances for these alternative plans, including full list of env vars for each instance).
- The planner must be driven only by the actual selected service env values that were resolved from the user intent; it must never invent default object types, zones, or fallback scenarios.

Input:
- `fixed_services`: list of services that must be included in every plan
- `service_variants`: list of service variants to consider for deployment

No-default rule:

- Never populate `OBJECT_TYPE_FILTER`, `ZONES_OF_INTEREST`, or any equivalent object/zone inputs from a hard-coded fallback list.
- If the selected service env data does not contain real object types and zone values, return an empty plan set rather than generating a fake scenario.
- The planner should only generate alternatives from actual resolved service variants and actual env values.

Output JSON shape:

```json
{
  "plans": [
    {
      "plan_name": "Alternative 1",
      "services": [
        {"service_name": "service-a", "action": "new_instance", "env_vars": {"KEY_1": "value1"}},
        {"service_name": "service-b", "action": "new_instance", "env_vars": {"KEY_2": "value2"}}
      ]
    }
  ]
}
``` -->

### `get_deployable_services_basic(service_endpoint: str) -> dict`

Purpose:

- Fetch all deployable services from `GET /info/services`.
- Return only `name`, `description`, and `dependencies` for each service.

Input:

- `service_endpoint` (example: `http://test-api:8001`)

Output JSON shape:

```json
{
  "services": [
    {
      "name": "service-a",
      "description": "Service description",
      "dependencies": ["service-b"]
    }
  ],
  "total": 1
}
```

Rule:

- If this function satisfies the current need, use it instead of issuing direct API calls in ad-hoc Python code.

### `get_service_env_schema(service_endpoint: str, service_name: str) -> dict`

Purpose:

- Fetch env schema for one service from `GET /info/{service_name}`.
- Return all three env var arrays for that service.

Input:

- `service_endpoint`
- `service_name`

Output JSON shape:

```json
{
  "name": "service-a",
  "required_env_vars": [],
  "optional_env_vars": [],
  "frozen_env_vars": []
}
```

Each env var entry has `name`, `type` (a Python type name as a string),
`description`, `value` (the default), `required`, and `possible_values`
(a list, or `null` for free-form values).

Rules:

- `frozen_env_vars` are set by the chart. Never submit them in `POST /instances`.
  They do appear in `GET /status` env vars, so treat them as explained extras
  rather than unexpected values.
- `required` is `false` on both optional and frozen entries. Array membership is
  the only way to tell the two apart.
- When `possible_values` is a list, only use values from that list.

### `is_service_deployed(service_endpoint: str, expected: dict) -> dict`

Purpose:

- Check whether at least one deployed instance matches all expected env var values.

Input JSON shape:

```json
{
  "service_name": "service-a",
  "env_vars": {
    "KEY_1": "value1"
  }
}
```

Status lookup:

- Calls `GET /status?name=<service_name>`. That is the only filter the API
  defines: do not use `/status/name=<service_name>` or `?service_name=`.

Comparison semantics:

- Only the keys present in `expected.env_vars` are compared. This is deliberate:
  `POST /instances` merges what you submit with the chart defaults and frozen
  vars, so `GET /status` reports a superset of what was requested.
- Values are compared after canonicalization, so a Python list, a JSON array
  string (`'["CAR","TRUCK"]'`) and a comma-separated string (`CAR,TRUCK`) all
  match, and zone IDs match regardless of case (`C5_w` vs `C5_W`).
- An env var whose catalogue default is `null` is omitted from the deployed env
  entirely, so never put such a key in `expected.env_vars`.

Output JSON shape:

```json
{
  "service_name": "service-a",
  "is_deployed": true,
  "matched_instance": {
    "instance_name": "service-a-1a2b3c4d",
    "service_name": "service-a",
    "deployment_status": "Available",
    "node": "node1-k8s",
    "ready": true,
    "env_vars": {}
  }
}
```

### `get_running_instances(service_endpoint: str, service_name: str | None = None, short_info: bool = False) -> dict`

Purpose:

- List the instances currently deployed, from `GET /status`, optionally filtered
  to one service.
- Use this for "Show Running Services" and whenever the workflow needs the
  current cluster state.

Output JSON shape:

```json
{
  "instances": [
    {
      "instance_name": "service-a-1a2b3c4d",
      "service_name": "service-a",
      "deployment_status": "Available",
      "ready": true,
      "env_vars": {}
    }
  ],
  "total": 1
}
```

### `create_instance(service_endpoint, service_name, env_vars=None, node_name=None, instance_prefix=None, metrics_trackable=True) -> dict`

Purpose:

- Deploy one instance via `POST /instances`.
- Returns `instance_name`, `service_name`, `namespace`, `image`,
  `metrics_trackable`.

Rules:

- Leave `metrics_trackable` as `true`. It injects the metrics sidecar, without
  which nothing downstream can observe the instance.
- Never submit `frozen_env_vars`.

### `update_instance(service_endpoint, instance_name, env_vars=None) -> dict`

Purpose:

- Reconfigure an instance in place via `PUT /instances/{instance_name}`, with a
  rolling restart. Omit `env_vars` for a pure restart.

Rule:

- Prefer this over `delete_instance` + `create_instance` when changing the
  configuration of a running instance: there is no window in which the service
  is absent, and the instance name survives.

### `delete_instance(service_endpoint, instance_name) -> dict`

Purpose:

- Remove an instance via `DELETE /instances/{instance_name}`.

### `get_health(service_endpoint) -> dict`

Purpose:

- Pre-flight check via `GET /health`. Returns `{"status": "ok"}`.

## Workflows by Capability

### Capability: Deploy Services (Main)

Inputs:

- `user_intent: string`
- Optional user-provided env hints (key/value pairs)
- `node_name: string | null` (optional)

Keep the feedback to the user concise.

Workflow:

1. Call `get_deployable_services_basic(service_endpoint)` from `/workspace/script/util.py` to get the service catalog, descriptions, and dependencies.
2. Select the essential initial services based on the user intent and the service descriptions. If no clear match exists, stop and ask the user to clarify.
3. Resolve the dependency graph and build the ordered service set. For each service:
   - call `get_service_env_schema(service_endpoint, service_name)`
   - fill in the required env values from the user intent and the service metadata
   - if zone-based values are involved and the intent is not already canonical
     (canonical zone IDs match `^[LCISBE]+\d+_`), delegate to the
     `gate-spatial-reasoning` sub-agent before continuing:
     - Folder path to delegate to: exactly `gate-spatial-reasoning`. It sits
       directly under `/workspace`. Never prefix it with this skill's own name
       and never invent a nested path.
     - Pass the original user intent verbatim.
     - The sub-agent returns a JSON object mapping spatial roles to canonical
       zone IDs, plus ranked alternatives and a rationale.
     - If it returns ambiguous candidates, surface them to the user as a single
       multiple-choice question rather than asking for raw zone IDs.
     - Use the resolved zone IDs to fill the env plan.
4. Get the current cluster state. Call `get_running_instances(service_endpoint)`
   from `/workspace/script/util.py`. Never assume or invent what is running, and
   never reuse a list of instances from an earlier turn: fetch it.
   - For each candidate service plus env set, confirm with
     `is_service_deployed(service_endpoint, expected)`. Put only the essential
     variables in `expected` - skip anything with a default value, anything you
     are unsure about, and anything `KAFKA`-related.
5. Use the actual object-type and zone values extracted from the user intent as the source of truth and imagine all instances can be deployed separately or combined in all types of combinations. Keep the full set internally, compare them against reuse of the instances already running retrieved in step 4, CPU, memory, overlap, and complexity, and choose the different alternative deployment plans that meet the different criteria, but also note which is the strongest on in all aspects.
   - If no actual object-type or zone values were resolved, do not invent defaults; stop and report that the required intent-backed values are missing.
   - If there is only one valid candidate, say so clearly.
6. Present the alternatives to the user and ask to choose one to proceed.
7. Show the final to-be-deployed list and ask: `Proceed with deployment? (yes/no)`.
8. If confirmed, deploy each service in order with `create_instance(...)`.
9. Stop on the first failure and return any partial results.

Determinism rules:

1. Sort dependency names alphabetically before traversal.
2. Do not hand-roll env var comparison. `is_service_deployed` canonicalizes
   both sides: list encodings (Python list, JSON array string, comma-separated)
   and zone ID casing are all normalized before comparing.

Readiness criteria:

- `deployment_status == "Available"` (the API reports `Available` or `Degraded`,
  never `deployed`)
- `pods` is non-empty; an instance scaled to zero reports `pods: []`
- all pods have `ready == true`
- all pod phases are `Running`

`instance_is_ready(instance)` in `/workspace/script/util.py` applies all of these.

Failure handling:

- dependency cycle during DFS: stop and report the cycle
- if any deployment fails, stop immediately and return the partial result

Output:

- intent decision result
- selected initial service pool
- ordered deployment plan
- confirmation prompt
- deployment result (including partial result on failure)

Final report format:

1. `Deployed` (`service_name`, `instance_name`)
2. `Failed` (first failing item and reason), if any
3. `Not executed due to failure` remaining items, if any

### Capability: Show Current Running Services

Workflow:

1. Call `get_running_instances(service_endpoint)` from `/workspace/script/util.py`,
   passing `service_name` when the intent is specific to one service. The API's
   only filter is `?name=`; there is no `?service_name=` param.
2. Return concise per-instance list:
   - `instance_name`
   - `service_name`
   - `deployment_status`
   - `ready` (pod readiness summary)
   - all env vars
3. If `total == 0`, report no running/deployed services.

Output:

- Current deployed/running service instances

### Capability: List Deployable Services

Workflow:

1. Call `get_deployable_services_basic(service_endpoint)` from `/workspace/script/util.py`.
2. Return concise per-service catalog:
   - `name`
   - `description`
   - direct `dependencies`
3. Include required/optional env schema summary when present.
4. Return `total`.

Output:

- Available deployable services with metadata

## API Reference

### Endpoint Configuration

Default base URL:

- `http://test-api:8001` - the compose service DNS name for the local mock.

The functions in `/workspace/script/util.py` already default to this, read from
the `K8S_MANAGER_URL` environment variable, so `service_endpoint` can be passed
as `None`. The real k8s-manager listens on port `8000`, not `8001`; switching to
it is a change to `K8S_MANAGER_URL` only.

### Request Defaults

Use these defaults unless user specifies otherwise:

- `Content-Type: application/json` for request bodies
- `Accept: application/json`
- Timeout per request: `30s`
- Poll interval: `5s`
- Readiness timeout per instance: `300s`

Implementation rule:

- Use Python for API calls and response parsing.
- Do not use raw `curl` output as final response.

### Response Size Control

Required for all API calls:

- Parse JSON and extract only fields needed for the current step.
- Do not return or store full raw payload in agent output.
- Keep output compact and task-focused.

### Endpoints

#### `GET /info/services`

Purpose:

- Discover deployable services and metadata.

Use in workflows:

- Intent-to-service mapping
- Dependency graph creation
- Deployable catalog listing

Expected response fields:

- `services[].name`
- `services[].description`
- `services[].dependencies`
- `services[].required_env_vars`
- `services[].optional_env_vars`
- `services[].frozen_env_vars` (chart-set; never submit these)

With `?short_info=true` only `name`, `description`, `versions` and
`dependencies` are returned, and `versions` comes back empty.

NOTE: when you call this API in python, always add post-processing in your python code to extract only the fields or items that are relevant to the current step in the workloads. Do not return the full payload.

#### `GET /status`

Purpose:

- List current instances.

Use in workflows:

- Show running services
- Precheck whether an instance with matching `env_vars` already exists

Query params (these are the only ones the API defines):

- `name` - filter to instances of one service name
- `short_info` - omit `env_vars`, `kafka_topics`, `ram_memory_usage_mb`,
  `frequency_metrics`
- `window_seconds` - frequency-metrics window

Relevant response fields:

- `instances[].instance_name`
- `instances[].service_name`
- `instances[].deployment_status` (`Available` | `Degraded`)
- `instances[].node` (may be `null`)
- `instances[].pods[].ready`
- `instances[].pods[].phase`
- `instances[].env_vars` - a flat object of strings, **merged** with the chart
  defaults and frozen vars, so it is a superset of what was submitted. List
  values appear as compact JSON; `APPROACH_ZONES` is comma-separated; deployed
  zone IDs are uppercased (`C5_W`) while the catalogue publishes them mixed-case
  (`C5_w`). There is no `env_hash` field.
- `total`

#### `GET /status/{instance_id}`

Returns a **bare instance object** with no `instances`/`total` wrapper. A missing
instance gives `404` with body `{"detail": "Instance '<name>' not found."}`.

#### `POST /instances`

Purpose:

- Create a new service instance.

Body fields: `service_name` (required), `env_vars`, `node_name`,
`instance_prefix`, `metrics_trackable` (default `true`).

Example:

```json
{
  "service_name": "object-counting",
  "node_name": "node1-k8s",
  "env_vars": {
    "OBJECT_TYPE_FILTER": "[\"CAR\"]",
    "ZONES_OF_INTEREST": "[\"L10_all_e\"]"
  }
}
```

Returns `instance_name`, `service_name`, `namespace`, `image`,
`metrics_trackable`. Prefer `create_instance(...)` over writing this call by hand.

#### `PUT /instances/{instance_name}`

Update env vars and rolling-restart, or restart only if `env_vars` is omitted.
Use this instead of `DELETE` + `POST` to reconfigure a running instance.

#### `DELETE /instances/{instance_name}`

Delete a service instance by name. Answers `204` with no body.

## What we already know:

- Skip the variable checks for lidar-osef-data-streamer, osef-tracked-objects, osef-zones, because they are always deployed with default variables. Tell the users that they are deployed, and do not need to be re-deployed.
- No need to show or check env vars related to `KAFKA` in the env resolution or user confirmation steps, but use default values for these variables when actual deploy the services.
- All object types supported: "TRUCK","CAR","TWO-WHEELER","UNKNOWN", "PERSON". For pedestrian, use "PERSON".
- if service `object-in-zone-detection` is recently deployed by you, always consider re-deploy it with new variable values. If this is the case, tell the user you can re-deploy, and if confirmed, prefer `update_instance(service_endpoint, instance_name, env_vars=...)` to change it in place; only fall back to deploying a new instance and removing the old one if the configuration cannot be updated
- no need to wait and check the deployment results, we have dedicated workflow for that. Just trigger the deployment and return.
- for intents involving pedistrian walking cross a zone, use object-in-zone-detection with `OBJECT_TYPE_FILTER=["PERSON"]`
- use 6 m/s as default high-speed.
- crosswalk-safety-alert needs two instances of object-in-zone-detection with different env values: one for pedestrian with `OBJECT_TYPE_FILTER=["PERSON"]` and another for vehicles with `OBJECT_TYPE_FILTER` set to "TRUCK","CAR","TWO-WHEELER","UNKNOWN", each with relevant zones.
- when the intent is about illegal pedestrian, no need for corss-walk-safety-alert. Also try to reuse object-in-zone-detection witht he same zone, and add PERSON into the object types.
- For zone-related env vars on the Sofia intersection, never ask the user for raw zone IDs. Always delegate to the `gate-spatial-reasoning` folder first (that exact folder path, directly under `/workspace`).
- `read_file`, `write_file` and `list_dir` resolve relative paths from the calling agent's own folder. Pass a bare filename, never a path that repeats the folder name, or it will be appended twice and fail.
