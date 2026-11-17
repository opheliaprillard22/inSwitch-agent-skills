---
name: deploy-service-dfs
description: Execute intent-driven service operations in k8s_manager. Main flow is dependency-first deployment from natural language intent; also supports listing running services and listing available deployable services. Alternative deployment plans are enumerated programmatically with generate_deployment_plans, not by the agent reasoning them out.
---

# Deploy Service DFS

Execute service-management intents directly using the `k8s_manager` HTTP API.

Do not generate code for the user. Execute the workflow.
When a predefined function exists (see "Predefined Python Utilities" at the bottom of this file), import and use it instead of directly calling the API.

<!-- DEBUG mode is OFF!

Do not use hardcoded Kafka or OSEF addresses. The values that used to be listed
here (`10.2.0.163:29092`, `http://10.2.0.164:8081`, `tcp://192.168.2.10`) belong to the local mock and are wrong for the real cluster, which publishes its own addresses in `possible_values`. Resolve these values per service at deploy time from `get_service_env_schema`, as described under "Required env vars" below. -->

## Capabilities

Understand natural-language intent and choose one capability:

- `Deploy Services` with dependency resolution and user confirmation
- `Show Running Services` inspect deployed instances according to the user's intent
- `Inspect Deployable Services`: inspect deployable services, and show the information relevant to the user's intent

## Workflows by Capability

### Capability: Deploy Services (Main)

#### Inputs

- `user_intent: string`
- Optional user-provided env hints (key/value pairs)
- `node_name: string | null` (optional)

Keep the feedback to the user concise.

#### Workflow

1. Call `get_deployable_services_basic()` from `/workspace/script/util.py` to get service names, descriptions, and dependencies.
2. Select one or more initial services based on the relevance between user intent and service descriptions. If no clear match, stop and ask user to clarify. Select only the essential services. Show the list of initial services, then ask user to confirm before proceeding.
3. Resolve the dependency graph and generate a list of services. For each of these services:

- Build env plan: Call `get_service_env_schema(service_name)` from `/workspace/script/util.py`, then use the metadata to fill env values based on user intent, defaults, and common sense. If key required env values are not resolvable, ask user to provide them before proceeding.
- When an env var publishes `possible_values`, only use values from that list. Never put anything from `frozen_env_vars` into an env plan.
- `possible_values` outranks anything you see on a running instance. Other instances legitimately run with values outside their published list - `KAFKA_DEST_BOOTSTRAP_SERVER=localhost:19092` is a per-pod sidecar address, correct for those services and wrong for a new one. Copying it points the new instance at a broker that does not exist for it.
- Match the catalogue's casing character for character. Zone IDs are mixed-case (`C5_w`, `L1_n_ws`, `L10_all_e`) and the services compare them literally, so `C5_W` is a different, non-existent zone. Never upper-case or lower-case a published value.
- Respect the declared `type`. `OBJECT_TYPE_FILTER` and `ZONES_OF_INTEREST` are `type: list`: pass a real list such as `["PERSON"]` or `["L1_n_ws", "L2_n_s"]`. A bare `PERSON`, or a comma-joined `L1_n_ws,L2_n_s`, is not a list. The helper functions encode lists correctly for the API, so hand them the list and do not pre-format it.
4. Generate a sensible deployment plan and check against the current cluster state. Fill out all the required env values from the plan. If 
5. Print out the final deployment plan, then ask user to confirm before proceeding. If confirmed, deploy each service in order with `create_instance(service_name, env_vars=...)`. Do not hand-write `POST /instances`.
6. Stop on first failure and return partial results.


<!-- 4. **Spatial reasoning (conditional).** If the selected service(s) accept zone-typed env vars (e.g. `FROM_ZONE`, `TO_ZONE`, `CROSSWALK_ZONE`, `APPROACH_ZONES`, `ZONES_OF_INTEREST`) AND the user intent does not contain canonical zone IDs (matching `^[LCISBE]+\d+_`), delegate to the `gate-spatial-reasoning` sub-agent.

   - Folder path to delegate to: exactly `gate-spatial-reasoning`. It sits
     directly under `/workspace`. Never prefix it with this skill's own name and
     never invent a nested path.
   - Pass the original user intent verbatim.
   - The sub-agent returns a JSON object mapping spatial roles to canonical zone IDs, plus ranked alternatives and a rationale.
   - If the sub-agent returns ambiguous candidates, surface them to the user as a single multiple-choice question instead of asking for raw zone IDs.
   - Use the resolved zone IDs in step 3.

5. Before calling `generate_deployment_plans(fixed_services, service_variants)`, convert the user-facing intent selections into the exact planner payload that the function expects.

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

6. Generate all deployment plans using `generate_deployment_plans(fixed_services, service_variants)` from `/workspace/script/util.py`. Do not recommend one over the other.

Background rule: build the full canonical set internally from the actual selected service instances and their resolved env values only. Do not create a plan from defaults, synthetic object types, or synthetic zones. If the selected service env data does not contain a real object-type set and a real zone set, do not generate a fake scenario; keep the plan set empty and report that the required intent-driven inputs are missing.

- For `object-in-zone-detection`, enumerate the full canonical set of alternatives internally, including every valid combination of object-type subsets and zone subsets that are actually selected from the service env configuration.
- Preserve the complete internal set of valid plans, do not collapse to a single plan, and do not recommend one over the other.
- When you present the result to the user, if more than four alternatives exist, show 4 user-facing alternatives, and explicitly label them as a shortlist drawn from the full valid set.
- If there are more than 4 plans generated, present 4 of the alternative plans to the user. Keep the complete canonical set internally, and only show the shortlist in the user-facing response. Ask the user whether they would like to investigate which of the plans has the best performance. If yes, tell them that this function is currently a work-in-progress and ask which of the four alternatives they would like to proceed with. If no, ask them to select one of the four alternatives to proceed.
- If there is only one valid plan, tell that to the user and proceed to step 7.
- If no valid plan exists because the required selected env values were never resolved, say so clearly and stop before suggesting a default scenario.
- The internal set must remain complete. Never write it to a file in `/workspace` to work around its size; keep it in memory for the turn.
- The output should be a structure like:
  ```json
  {
    "plans": [
      {
        "plan_name": "Alternative 1",
        "services": [
          {"service_name": "object-in-zone-detection", "action": "new_instance", "env_vars": {"OBJECT_TYPE_FILTER": "PERSON", "ZONES_OF_INTEREST": "C5_W"}},
          {"service_name": "object-in-zone-detection", "action": "new_instance", "env_vars": {"OBJECT_TYPE_FILTER": "CAR,TRUCK,TWO-WHEELER", "ZONES_OF_INTEREST": "L1_N_WS,L2_N_S"}}
        ]
      }
    ]
  }
  ``` -->

<!-- 7. Get the current cluster state, then compare the plans against it. Call `get_running_instances()` from `/workspace/script/util.py` first. Never assume or invent what is running, and never reuse a list of instances from an earlier turn: fetch it.

- Ask the user if they want to proceed to check all the alternative deployment plans against services that are already deployed. If yes, for each service in a plan, check whether an instance with matching env is already deployed: call `is_service_deployed(expected)` from `/workspace/script/util.py`. Only include the essential variables in the expected dict - ignore those with default values, those you are not certain about, and those related to `KAFKA`. If no instance is found, add the current service to `to_deploy` in post-order.
- `is_service_deployed` reports a match on env values only. Check `matched_instance.ready` before counting an instance as reusable: an instance scaled to zero still matches on env but is not running.

8. Based on the information from step 7, use the full internal set of alternative deployment plans and find the 3 best alternatives - meaning the plans that will re-use the most deployed instances. Ask the user to select one of the alternatives to proceed. If there is only one valid plan, tell that to the user and proceed to step 9. If no valid plan exists because the required selected env values were never resolved, say so clearly and stop before suggesting a default scenario.
9. Show to-be-deployed lists, then ask: `Proceed with deployment? (yes/no)`. 
10. If confirmed, deploy each service in order with `create_instance(service_name, env_vars=...)`. Do not hand-write `POST /instances`.
11. Stop on first failure and return partial results. -->

<!-- If DEBUG mode is ON: stop at step 3 to show inital services, at each iteration of DFS to show current node, env plan, and satisfiability check result, and ask users to continue. -->

#### Determinism rules

1. Sort dependency names alphabetically before traversal.
2. Do not hand-roll env var comparison. `is_service_deployed` canonicalizes
   both sides: list encodings (Python list, JSON array string, comma-separated)
   and zone ID casing are all normalized before comparing.

#### Readiness criteria

- `deployment_status == "Available"` (the API reports `Available` or `Degraded`,
  never `deployed`)
- `pods` is non-empty; an instance scaled to zero reports `pods: []`
- all pods have `ready == true`
- all pod phases are `Running`

`instance_is_ready(instance)` in `/workspace/script/util.py` applies all of these.

#### Failure handling

- dependency cycle during DFS: stop and report the cycle
- if any deployment fails, stop immediately and return the partial result

#### Output

- intent decision result
- selected initial service pool
- ordered deployment plan
- confirmation prompt
- deployment result (including partial result on failure)

#### Final report format

1. `Deployed` (`service_name`, `instance_name`)
2. `Failed` (first failing item and reason), if any
3. `Not executed due to failure` remaining items, if any

### Capability: Show Current Running Services

#### Workflow

1. Call `get_running_instances()` from `/workspace/script/util.py`,
   passing `service_name` when the intent is specific to one service. The API's
   only filter is `?name=`; there is no `?service_name=` param.
2. Return concise per-instance list:
   - `instance_name`
   - `service_name`
   - `deployment_status`
   - `ready` (pod readiness summary)
   - all env vars
3. If `total == 0`, report no running/deployed services.

#### Output

- Current deployed/running service instances

### Capability: List Deployable Services

#### Workflow

1. Call `get_deployable_services_basic()` from `/workspace/script/util.py`.
2. Return concise per-service catalog:
   - `name`
   - `description`
   - direct `dependencies`
3. Return `total`.

Rules:

- `get_deployable_services_basic` already requests `short_info=true`, so one
  call returns the whole catalogue. Do not loop.
- Do NOT call `get_service_env_schema` for every service to enrich this list.
  That is one extra HTTP round trip per service, and the combined output is
  large enough to be truncated before it reaches you. Fetch the env schema only
  for the services actually selected for deployment, or when the user asks
  about one specific service.
- If a tool result comes back marked truncated, print less rather than
  re-running the same code: select the fields you need, or print one item at a
  time. The output is not written to a file anywhere, so do not go looking for
  one.

#### Output

- Available deployable services with metadata

## What we already know:

- Skip the variable checks for lidar-osef-data-streamer, osef-tracked-objects, osef-zones, because they are always deployed with default variables. Tell the users that they are deployed, and do not need to be re-deployed.
<!-- - `KAFKA`-related env vars do not need to be shown to the user or compared when checking for existing instances. They still have to be RESOLVED and submitted: most of them are required, their catalogue defaults are empty, and an instance deployed without them crash-loops. Fill them from `possible_values` per the "Required env vars" section. -->
- All object types supported: "TRUCK","CAR","TWO-WHEELER","UNKNOWN", "PERSON". For pedestrian, use "PERSON".
<!-- - if service `object-in-zone-detection` is recently deployed by you, always consider re-deploy it with new variable values. If this is the case, tell the user you can re-deploy, and if confirmed, prefer `update_instance(instance_name, env_vars=...)` to change it in place; only fall back to deploying a new instance and removing the old one if the configuration cannot be updated -->
- no need to wait and check the deployment results, we have dedicated workflow for that. Just trigger the deployment and return.
- for intents involving pedistrian walking cross a zone, use object-in-zone-detection with `OBJECT_TYPE_FILTER=["PERSON"]`
- use 6 m/s as default high-speed.
- crosswalk-safety-alert needs two instances of object-in-zone-detection with different env values: one for pedestrian with `OBJECT_TYPE_FILTER=["PERSON"]` and another for vehicles with `OBJECT_TYPE_FILTER` set to "TRUCK","CAR","TWO-WHEELER","UNKNOWN", each with relevant zones.
- when the intent is about illegal pedestrian, no need for cross-walk-safety-alert. Also try to reuse object-in-zone-detection witht he same zone, and add PERSON into the object types.
<!-- - For zone-related env vars on the Sofia intersection, never ask the user for raw zone IDs. Always delegate to the `gate-spatial-reasoning` folder first (that exact folder path, directly under `/workspace`). -->
- `read_file`, `write_file` and `list_dir` resolve relative paths from the calling agent's own folder. Pass a bare filename, never a path that repeats the folder name, or it will be appended twice and fail.

## Predefined Python Utilities

Use these functions from `/workspace/script/util.py` whenever applicable. Do not read this file directly; everything you need is documented below.

Import example:

```python
import sys
sys.path.append("/workspace/script")

from util import generate_deployment_plans
from util import get_deployable_services_basic
from util import get_service_env_schema
from util import get_running_instances
from util import is_service_deployed
from util import create_instance
from util import update_instance
from util import delete_instance
from util import get_health
```

### Which API call function for which endpoint

| Endpoint | Function |
| --- | --- |
| `GET /health` | `get_health()` |
| `GET /info/services` | `get_deployable_services_basic()` |
| `GET /info/{service_name}` | `get_service_env_schema(service_name)` |
| `GET /status` | `get_running_instances()` |
| `GET /status?name=` | `is_service_deployed(expected)` |
| `POST /instances` | `create_instance(service_name, ...)` |
| `PUT /instances/{instance_name}` | `update_instance(instance_name, ...)` |
| `DELETE /instances/{instance_name}` | `delete_instance(instance_name)` |

You never choose, and never need to know, which API you are talking to. The
endpoint and its credentials are resolved from the environment by `util.py`:
whether that is the local mock or a real cluster is decided outside this skill.

- Never pass a `service_endpoint` argument.
- Never hardcode or guess a host, port, URL, token or password.
- Never put credentials in a command, a URL or anything you print.

### Response size

The functions already return reduced payloads. Extract only the fields the
current step needs rather than printing whole objects; large output is
truncated before you see it.

<!-- #### `generate_deployment_plans(fixed_services: list, service_variants: list) -> dict`

Purpose:

- Generate all deployment plans based on the fixed services and service variants.
- Keep the full canonical set of deployment alternatives internally, but only show a shortlist of up to 4 alternatives to the user (show the list of instances for these alternative plans, including full list of env vars for each instance).
- The planner must be driven only by the actual selected service env values that were resolved from the user intent; it must never invent default object types, zones, or fallback scenarios.

Input:
- `fixed_services`: list of services that must be included in every plan
- `service_variants`: the service variants to consider. Either a flat list of
  service entries (the form step 5 builds) or a dict keyed by service name;
  both are normalized internally and give identical results.

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

Rules:

- Service names in the returned plans are hyphenated, matching the catalogue
  (`object-in-zone-detection`, never `object_in_zone_detection`).
- The planner enumerates every exact-cover combination, so the set grows fast:
  the two-variant crosswalk scenario in step 5 alone produces over 2000 plans.
  Never print, echo, or serialize the full set, and never write it to a file in
  `/workspace`. Read `len(result["plans"])` for the count and index the few you
  intend to show. -->

#### `get_deployable_services_basic() -> dict`

Purpose:

- Fetch all deployable services from `GET /info/services`.
- Return only `name`, `description`, and `dependencies` for each service.

Input:

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

#### `get_service_env_schema(service_name: str) -> dict`

Purpose:

- Fetch env schema for one service from `GET /info/{service_name}`.
- Return all three env var arrays for that service.

Input:

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

#### `is_service_deployed(expected: dict) -> dict`

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

#### `get_running_instances(service_name: str | None = None, short_info: bool = False) -> dict`

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

#### `create_instance(service_name, env_vars=None, node_name=None, instance_prefix=None, metrics_trackable=True) -> dict`

Purpose:

- Deploy one instance via `POST /instances`.
- Returns `instance_name`, `service_name`, `namespace`, `image`,
  `metrics_trackable`.

Rules:

- Leave `metrics_trackable` as `true`. It injects the metrics sidecar, without
  which nothing downstream can observe the instance.
- Never submit `frozen_env_vars`.
- Instances are tagged automatically with this session's experiment prefix, so
  names come back as `<experiment>-<service>-<hash>`. Do not pass
  `instance_prefix` and do not invent one. Report the returned `instance_name`
  verbatim: it is how this run's instances are found and cleaned up later.

#### `update_instance(instance_name, env_vars=None) -> dict`

Purpose:

- Reconfigure an instance in place via `PUT /instances/{instance_name}`, with a
  rolling restart. Omit `env_vars` for a pure restart.

Rule:

- Prefer this over `delete_instance` + `create_instance` when changing the
  configuration of a running instance: there is no window in which the service
  is absent, and the instance name survives.

#### `delete_instance(instance_name) -> dict`

Purpose:

- Remove an instance via `DELETE /instances/{instance_name}`.

#### `get_health() -> dict`

Purpose:

- Pre-flight check via `GET /health`. Returns `{"status": "ok"}`.
