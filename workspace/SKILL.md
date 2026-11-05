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
from util import is_service_deployed
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

- Fetch env schema for one service from `GET /info/services`.
- Return `required_env_vars` and `optional_env_vars` for that service.

Input:

- `service_endpoint`
- `service_name`

Output JSON shape:

```json
{
  "name": "service-a",
  "required_env_vars": [],
  "optional_env_vars": []
}
```

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

- Uses `/status/name=<service_name>` first.
- Falls back to query-based variants if needed.

Output JSON shape:

```json
{
  "service_name": "service-a",
  "is_deployed": true,
  "matched_instance": {}
}
```

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
   - if zone-based values are involved and the intent is not already canonical, delegate to the `gate-spatial-reasoning` sub-agent before continuing
5. Here is the list of current services running: 
        {
          "instances": [
            {
              "instance_name": "crosswalk-safety-alert-person-c5w",
              "service_name": "crosswalk-safety-alert",
              "deployment_status": "deployed",
              "pods": [{"ready": True, "phase": "Running"}],
              "env_vars": {
                "OBJECT_TYPE_FILTER": "PERSON",
                "ZONES_OF_INTEREST": "C5_W"
              }
            },
            {
              "instance_name": "object-in-zone-detection-vehicles-l1l2",
              "service_name": "object-in-zone-detection",
              "deployment_status": "deployed",
              "pods": [{"ready": True, "phase": "Running"}],
              "env_vars": {
                "OBJECT_TYPE_FILTER": "TWO-WHEELER,UNKNOWN",
                "ZONES_OF_INTEREST": "L1_N_WS,L2_N_S"
              }
            },
            {
              "instance_name": "object-in-zone-detection-vehicles-tc",
              "service_name": "object-in-zone-detection",
              "deployment_status": "deployed",
              "pods": [{"ready": True, "phase": "Running"}],
              "env_vars": {
                "OBJECT_TYPE_FILTER": "TRUCK, CAR",
                "ZONES_OF_INTEREST": "L1_N_WS, L2_N_S"
              }
            }
          ],
          "total": 3
        }
<!-- Get the instances already running by calling `is_service_deployed(service_endpoint, expected)`. -->
6. Use the actual object-type and zone values extracted from the user intent as the source of truth and imagine all instances can be deployed separately or combined in all types of combinations. Keep the full set internally, compare them against reuse of the instances already running retrieved in step 5, CPU, memory, overlap, and complexity, and choose the different alternative deployment plans that meet the different criteria, but also note which is the strongest on in all aspects. 
   - If no actual object-type or zone values were resolved, do not invent defaults; stop and report that the required intent-backed values are missing.
   - If there is only one valid candidate, say so clearly.
6. Present the alternatives to the user and ask to choose one to proceed.
7. Show the final to-be-deployed list and ask: `Proceed with deployment? (yes/no)`.
8. If confirmed, deploy each service in order using `POST /instances`.
9. Stop on the first failure and return any partial results.

Determinism rules:

1. Sort dependency names alphabetically before traversal.
2. Canonicalize env vars before comparison:
   - sort keys ascending
   - convert values to strings
   - build a stable JSON object

Readiness criteria:

- `deployment_status == "deployed"`
- all pods have `ready == true`
- all pod phases are `Running`

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

1. Call `GET /status`, or `GET /status?service_name={service_name}` if user intent is specific to a service.
2. Return concise per-instance list:
   - `instance_name`
   - `service_name`
   - `deployment_status`
   - pod readiness summary
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

- `http://host.docker.internal:8001`

Treat base URL as runtime configuration. If endpoint changes, update before any call.

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

NOTE: when you call this API in python, always add post-processing in your python code to extract only the fields or items that are relevant to the current step in the workloads. Do not return the full payload.

#### `GET /status`

Purpose:

- List current instances.

Use in workflows:

- Show running services
- Precheck env identity support (`env_hash` or `env_vars`)

Relevant response fields:

- `instances[].instance_name`
- `instances[].service_name`
- `instances[].deployment_status`
- `instances[].pods[].ready`
- `instances[].pods[].phase`
- `instances[].env
- `total`

#### `GET /status?name={service_name}`

Get the status instances filtered by service name. Useful for pre-checking if an instance with matching env already exists.

#### `POST /instances`

Purpose:

- Create a new service instance.

Example:

```json
{
  "service_name": "lidar-object-counting",
  "node_name": "node1-k8s",
  "env_vars": {
    "OBJECT_TYPE": "CAR",
    "ZONE_FILTER": "L10_all_e"
  }
}
```

##### `DELETE /instances/{instance_name}`

Delete a service instance by name

## What we already know:

- Skip the variable checks for lidar-osef-data-streamer, osef-tracked-objects, osef-zones, because they are always deployed with default variables. Tell the users that they are deployed, and do not need to be re-deployed.
- No need to show or check env vars related to `KAFKA` in the env resolution or user confirmation steps, but use default values for these variables when actual deploy the services.
- All object types supported: "TRUCK","CAR","TWO-WHEELER","UNKNOWN", "PERSON". For pedestrian, use "PERSON".
- if service `object-in-zone-detection` is recently deployed by you, always consider re-deploy it with new variable values. If this is the case, tell the user you can re-deploy, and if confirmed, in addition to deploying the new one, remove the existing instance
- no need to wait and check the deployment results, we have dedicated workflow for that. Just trigger the deployment and return.
- for intents involving pedistrian walking cross a zone, use object-in-zone-detection with `OBJECT_TYPE=PERSON`
- use 6 m/s as default high-speed.
- crosswalk-safety-alert needs two instances of object-in-zone-detection with different env values: one for pedestrian with `OBJECT_TYPE=PERSON` and another for vehicles with `OBJECT_TYPE` set to "TRUCK","CAR","TWO-WHEELER","UNKNOWN", each with relevant zones.
- when the intent is about illegal pedestrian, no need for corss-walk-safety-alert. Also try to reuse object-in-zone-detection witht he same zone, and add PERSON into the object types.
- For zone-related env vars on the Sofia intersection, never ask the user for raw zone IDs. Always delegate to gate-spatial-reasoning first.
