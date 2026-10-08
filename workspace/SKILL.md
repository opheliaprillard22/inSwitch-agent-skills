---
name: deploy-service-dfs
description: Execute intent-driven service operations in k8s_manager. Main flow is dependency-first deployment from natural language intent; also supports listing running services and listing available deployable services.
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

from util import generate_deployment_plans
from util import get_deployable_services_basic
from util import get_service_env_schema
from util import is_service_deployed
```

### `generate_deployment_plans(fixed_services: list, service_variants: list) -> dict`

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
```

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

Keep the feedbacks to the user concise.

Workflow:

1. Call `get_deployable_services_basic(service_endpoint)` from `/workspace/script/util.py` to get service names, descriptions, and dependencies.
2. Select one or more initial services based on the relevance between user intent and service descriptions. If no clear match, stop and ask user to clarify. Select only the essential services. Show the list of initial services, then ask user to confirm before proceeding.
3. Resolve the dependency graph and generate a list of services. For each of these services:

- Build env plan: Call `get_service_env_schema(service_endpoint, service_name)` from `/workspace/script/util.py`, then use the metadata to fill env values based on user intent, defaults, and common sense. If key required env values are not resolvable, ask user to provide them before proceeding.
<!-- 2. Describe the parsed intent details including the zone IDs, the object types, and the env vars. Ask user to confirm before proceeding to step 3. If the user wants to change any of the parsed details, allow them to do so before proceeding. -->

3.5. **Spatial reasoning (conditional).** If the selected service(s) accept zone-typed env vars (e.g. `FROM_ZONE`, `TO_ZONE`, `CROSSWALK_ZONE`, `APPROACH_ZONES`, `ZONES_OF_INTEREST`) AND the user intent does not contain canonical zone IDs (matching `^[LCISBE]+\d+_`), delegate to the `gate-spatial-reasoning` sub-agent.

   - Folder path to delegate to: `gate-spatial-reasoning`
   - Pass the original user intent verbatim.
   - The sub-agent returns a JSON object mapping spatial roles to canonical zone IDs.
   - If the sub-agent returns ambiguous candidates, surface them to the user as a single multiple-choice question instead of asking for raw zone IDs.
   - Use the resolved zone IDs in step 3.

3a. Before calling `generate_deployment_plans(fixed_services, service_variants)`, convert the user-facing intent selections into the exact planner payload that the function expects.

   - The values already shown to the user are the source of truth: extract the actual object-type selections and the actual zone selections from those resolved intent values. If the intent was resolved by the `gate-spatial-reasoning` sub-agent, use the resolved zone IDs.
   - Build `service_variants` as a list of service entries whose `env_vars` contain the exact selected keys, for example:
     ```python
     service_variants = [
         {
             "service_name": "object-in-zone-detection",
             "env_vars": {
                 "OBJECT_TYPE_FILTER": "PERSON",
                 "ZONES_OF_INTEREST": "C5_W"
             }
         },
         {
             "service_name": "object-in-zone-detection",
             "env_vars": {
                 "OBJECT_TYPE_FILTER": "TRUCK,CAR,TWO-WHEELER,UNKNOWN",
                 "ZONES_OF_INTEREST": "L1_N_WS,L2_N_S"
             }
         }
     ]
     ```
   - Do not invent values, do not use defaults, and do not re-derive from a generic template when the actual intent values are already visible.
   - If the intent has not produced actual object-type and zone selections, stop and return an empty plan set instead of calling the planner with placeholders.

3b. Generate all deployment plans using `generate_deployment_plans(fixed_services, service_variants)` from `/workspace/script/util.py`. Do not recommend one over the other.

Background rule: build the full canonical set internally from the actual selected service instances and their resolved env values only. Do not create a plan from defaults, synthetic object types, or synthetic zones. If the selected service env data does not contain a real object-type set and a real zone set, do not generate a fake scenario; keep the plan set empty and report that the required intent-driven inputs are missing.

- For `object_in_zone_detection`, enumerate the full canonical set of alternatives internally, including every valid combination of object-type subsets and zone subsets that are actually selected from the service env configuration.
- Preserve the complete internal set of valid plans, do not collapse to a single plan, and do not recommend one over the other.
- When you present the result to the user, if more than four alternatives exist, show 4 user-facing alternatives, and explicitly label them as a shortlist drawn from the full valid set.
- If there are more than 4 plans generated, present 4 of the alternative plans to the user. Keep the complete canonical set internally, and only show the shortlist in the user-facing response. Ask the user whether they would like to investigate which of the plans has the best performance. If yes, tell them that this function is currently a work-in-progress and ask which of the four alternatives they would like to proceed with. If no, ask them to select one of the four alternatives to proceed.
- If there is only one valid plan, tell that to the user and proceed to step 4.
- If no valid plan exists because the required selected env values were never resolved, say so clearly and stop before suggesting a default scenario.
- The internal set must remain complete.
- The output should be a structure like:
  ```json
  {
    "plans": [
      {
        "plan_name": "Alternative 1",
        "services": [
          {"service_name": "object_in_zone_detection", "action": "new_instance", "env_vars": {"OBJECT_TYPE_FILTER": "PERSON", "ZONES_OF_INTEREST": "C5_W"}},
          {"service_name": "object_in_zone_detection", "action": "new_instance", "env_vars": {"OBJECT_TYPE_FILTER": "CAR,TRUCK,TWO-WHEELER", "ZONES_OF_INTEREST": "L1_N_WS,L2_N_S"}}
        ]
      }
    ]
  }
  ```


<!-- 4. Resolve the dependency graph for the selected deployment plan and generate a list of services. For each of these services:

- Build env plan: Call `get_service_env_schema(service_endpoint, service_name)` from `/workspace/script/util.py`, then use the metadata to fill env values based on user intent, defaults, and common sense. If key required env values are not resolvable, ask user to provide them before proceeding.
- Show the deployment plan (in a concise way), and ask user to confirm continue. -->

5. For each service in the selected plan, check if an instance with matching env is already deployed:

- Find a running instance for the service and expected env values: Call `is_service_deployed(service_endpoint, expected)` from `/workspace/script/util.py`. Only include the essentional variables into the expected dict - ignore those with default values, those you are not certain about, and those related to `KAFKA`. If no instance is found, add the current service to `to_deploy` in post-order.

6. Show to-be-deployed lists, then ask: `Proceed with deployment? (yes/no)`. Again, ignore KAFKA-related env vars in the confirmation step.
7. If confirmed, deploy each service in order using `POST /instances`.
8. Stop on first failure and return partial results.

If DEBUG mode is ON: stop at step 3 to show inital services, at each iteration of DFS to show current node, env plan, and satisfiability check result, and ask users to continue.

Determinism rules:

1. Sort dependency names alphabetically before traversal.
2. Canonicalize env vars before comparison:
   - Sort keys ascending.
   - Convert all values to string.
   - Build stable JSON object.

Readiness criteria:

- `deployment_status == "deployed"`
- all pods have `ready == true`
- all pod phases are `Running`

Failure handling:

- Dependency cycle during DFS: stop and report cycle.

Output:

- Intent decision result
- Selected initial service pool
- Ordered deployment plan
- Confirmation prompt
- Deployment result (including partial result on failure)

Final report format: 2. `Deployed` (`service_name`, `instance_name`) 3. `Failed` (first failing item and reason), if any 4. `Not executed due to failure` remaining items, if any

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
