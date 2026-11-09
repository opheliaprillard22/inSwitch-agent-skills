# k8s-manager API Reference

Full endpoint reference for the k8s-manager FastAPI service, generated from the
actual source (`k8s_manager/api/routers/*.py`) and verified against a live
call against the production cluster for every endpoint. Each entry has the
exact curl command used and a link to the raw captured response.

All calls assume a port-forward is active:

```bash
kubectl port-forward service/k8s-manager 8000:8000 -n default &
```

Base URL below is `http://localhost:8000`.

---

## Bugs found and fixed while building this reference

Building this doc against production surfaced four real bugs in k8s-manager, all fixed, merged to `master`, rebuilt, redeployed, and re-verified live (`gate-institute/k8s-manager`):

1. **`GET /kafka/topics` scaled O(all topics) instead of O(1) per lookup** — `_collect_topics` eagerly enriched every topic on every call. Took 80s with 13 topics. Fixed in PR #2/#3.
2. **Broker enrichment attempted on unreachable `localhost` addresses** — `KAFKA_DEST_BOOTSTRAP_SERVER` is frequently a per-pod sidecar proxy address, never reachable from k8s-manager's own pod; every such attempt wasted a full connection timeout. Fixed in PR #4.
3. **Destination topics permanently showed `message_count`/`config` as `null`** — the real upstream broker address is recorded on the sidecar container's `UPSTREAM_BROKER` env var, never read by `_deployment_to_dict`. Fixed in PR #5.
4. **`PATCH /kafka/topics/config` silently wiped unrelated topic config** — used Kafka's legacy, non-incremental `AlterConfigs` API, which replaces a topic's *entire* dynamic config with only what's in the request. A single-field update (`segment_jitter_ms`) reset `retention_bytes`/`retention_ms`/`segment_bytes` to broker defaults on a live, high-throughput topic. Fixed in PR #6 (switched to `IncrementalAlterConfigs`).

Separately, building the use case on top of this API surfaced a production memory leak: `TimeBucketMatcher` (shared by `object-in-zone-detection`, `crosswalk-safety-alert`, `overspeed-alert`) derived bucket age from the *message's own timestamp* rather than real wall-clock time — since upstream timestamps come from an OSEF recording's internal clock, not real epoch-ms, cleanup never triggered and unmatched buckets accumulated forever. This was consuming ~6GB of `node1-k8s`'s 7.7GB RAM and was the direct cause of that node repeatedly going `NotReady` throughout this session. Fixed in `gate-institute/INTEND` (commit `258c42b`).

---

## Endpoint index

| Method | Path | Summary |
|--------|------|---------|
| GET | `/health` | Liveness check |
| POST | `/instances` | Create a new service instance |
| PUT | `/instances/{instance_name}` | Restart an instance with a new configuration |
| DELETE | `/instances/{instance_name}` | Delete a service instance |
| GET | `/status` | Get runtime status for all (or filtered) deployed instances |
| GET | `/status/{instance_id}` | Get detailed runtime status for a specific instance |
| GET | `/info/services` | List all services with chart metadata from the Helm registry |
| GET | `/info/{service_name}` | Get chart metadata for a specific service |
| POST | `/info/{service_name}/deployment-tree` | Resolve the full deployment dependency tree for a service |
| GET | `/kafka/topics` | List all Kafka topics referenced by managed deployments |
| GET | `/kafka/topics/{topic_name}` | Get full details for a single Kafka topic |
| PATCH | `/kafka/topics/config` | Update broker-side config for one or more Kafka topics |
| POST | `/snapshots` | Capture a metrics snapshot for all deployed instances |
| GET | `/snapshots` | List all snapshot batches |
| GET | `/snapshots/{snapshot_id}` | Get all instance details for a specific snapshot |

---

## Health

### `GET /health`

Liveness check. No auth, no params.

```bash
curl -s http://localhost:8000/health
```

**Example response:** [outputs/health.json](outputs/health.json)

---

## Instances

Instances are Kubernetes Deployments created directly via the K8s API. Each
instance gets an auto-generated name (`{service_name}-{hash}`, or
`{instance_prefix}-{service_name}-{hash}` if a prefix is given), so multiple
independently-configured instances of the same service can run at once.

### `POST /instances`

Create a new instance. Body fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `service_name` | string | yes | Matches the Docker image name (e.g. `object-speeds`). |
| `instance_prefix` | string \| null | no | Prepended to the generated name. |
| `env_vars` | object | no (default `{}`) | Instance configuration (zone filters, Kafka topics, etc.). Merged with chart defaults from the Helm registry; explicit values win. |
| `node_name` | string \| null | no | Pin the pod to a specific node hostname. |
| `metrics_trackable` | bool | no (default `true`) | Inject the Kafka metrics-proxy sidecar. |

```bash
curl -s -X POST http://localhost:8000/instances \
  -H "Content-Type: application/json" \
  -d '{"service_name": "...", "node_name": "node1-k8s", "instance_prefix": "doctest", "env_vars": {...}}'
```

**Example call & response:** [outputs/instances_create.json](outputs/instances_create.json)

### `PUT /instances/{instance_name}`

Update env vars and rolling-restart, or just rolling-restart if `env_vars` is
omitted. Image and service identity never change.

```bash
# With new env vars (replaces existing config + rolling restart)
curl -s -X PUT http://localhost:8000/instances/<instance_name> \
  -H "Content-Type: application/json" \
  -d '{"env_vars": {...}}'

# Pure rolling restart, no config change
curl -s -X PUT http://localhost:8000/instances/<instance_name> \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Example call & response (with env_vars):** [outputs/instances_update_envvars.json](outputs/instances_update_envvars.json)
**Example call & response (pure restart):** [outputs/instances_update_restart_only.json](outputs/instances_update_restart_only.json)

**Unconfirmed observation:** a `GET /status/{instance_name}` taken immediately after `DELETE` (while the pod was still `Terminating`) showed `env_vars.OBJECT_TYPE_FILTER` as the instance's *original* creation value, not the value from the `PUT` update performed moments earlier. This could be a real staleness bug in how the Deployment spec is read during termination, or simply an artifact of catching the object mid-deletion. Not re-confirmed since the test instance was deleted as part of this same lifecycle demo — worth a dedicated repro if it recurs.

### `DELETE /instances/{instance_name}`

Deletes the Deployment; all its pods terminate. Returns `204 No Content` (no body).

```bash
curl -s -i -X DELETE http://localhost:8000/instances/<instance_name>
```

**Example call & response:** [outputs/instances_delete.txt](outputs/instances_delete.txt)

---

## Status

### `GET /status`

List runtime status (deployment status, pod health, optionally metrics) for
every managed instance, or filtered by service name.

Query params:

| Param | Type | Description |
|---|---|---|
| `name` | string \| null | Filter to instances of this service name. |
| `short_info` | bool (default `false`) | Omit `env_vars`, `kafka_topics`, `ram_memory_usage_mb`. |
| `window_seconds` | int \| null | Frequency-metrics window; defaults to server's `TIME_WINDOW_SIZE`. |

```bash
curl -s "http://localhost:8000/status"
curl -s "http://localhost:8000/status?name=<service_name>"
curl -s "http://localhost:8000/status?short_info=true"
```

**Example call & response (all instances):** [outputs/status_all.json](outputs/status_all.json)
**Example call & response (filtered by service name):** [outputs/status_filtered_by_name.json](outputs/status_filtered_by_name.json)
**Example call & response (short_info=true):** [outputs/status_short_info.json](outputs/status_short_info.json)

### `GET /status/{instance_id}`

Full runtime info for a single instance. Same `short_info` / `window_seconds`
query params as above.

```bash
curl -s "http://localhost:8000/status/<instance_name>"
curl -s "http://localhost:8000/status/<instance_name>?short_info=true"
```

**Example call & response (full):** [outputs/status_single_instance.json](outputs/status_single_instance.json)
**Example call & response (short_info=true):** [outputs/status_single_instance_short.json](outputs/status_single_instance_short.json)
**Example call & response (not found, after deletion):** [outputs/status_single_instance_404.json](outputs/status_single_instance_404.json)

---

## Info (service catalogue)

All data comes from the Helm charts registry (GHCR OCI).

### `GET /info/services`

List every chart in the Helm registry with versions, dependencies, and env
var metadata (enum-like vars get a `possible_values` list).

```bash
curl -s "http://localhost:8000/info/services"
curl -s "http://localhost:8000/info/services?short_info=true"
```

**Example call & response (full):** [outputs/info_services_full.json](outputs/info_services_full.json)
**Example call & response (short_info=true):** [outputs/info_services_short.json](outputs/info_services_short.json)

### `GET /info/{service_name}`

Chart metadata for one service.

```bash
curl -s "http://localhost:8000/info/<service_name>"
```

**Example call & response:** [outputs/info_single_service.json](outputs/info_single_service.json)

### `POST /info/{service_name}/deployment-tree`

Given a service name and its desired env vars, recursively resolves every
upstream service that must also be running and the parameters each needs.
`EnvVarAlias` references are resolved against the provided `env` so the tree
shows concrete values.

Query param `format`: `json` (default, structured tree) or `text` (ASCII tree).

```bash
curl -s -X POST "http://localhost:8000/info/<service_name>/deployment-tree" \
  -H "Content-Type: application/json" \
  -d '{"env": {"KEY": "value"}}'

curl -s -X POST "http://localhost:8000/info/<service_name>/deployment-tree?format=text" \
  -H "Content-Type: application/json" \
  -d '{"env": {"KEY": "value"}}'
```

**Example call & response (json):** [outputs/info_deployment_tree.json](outputs/info_deployment_tree.json)
**Example call & response (text):** [outputs/info_deployment_tree.txt](outputs/info_deployment_tree.txt)

---

## Kafka

### `GET /kafka/topics`

List every topic referenced by at least one managed deployment, enriched
with broker config, total message count, and source/destination instances.

```bash
curl -s "http://localhost:8000/kafka/topics"
```

**Example call & response:** [outputs/kafka_topics_all.json](outputs/kafka_topics_all.json)

### `GET /kafka/topics/{topic_name}`

Full broker config (all `describe_configs` fields) plus message count for one
topic. Query param `bootstrap_server` disambiguates when the same topic name
exists on more than one cluster (returns `409` otherwise).

```bash
curl -s "http://localhost:8000/kafka/topics/<topic_name>?bootstrap_server=<host:port>"
```

**Example call & response:** [outputs/kafka_topic_single.json](outputs/kafka_topic_single.json)

The `409` ambiguous-cluster case (`?bootstrap_server=` omitted with a topic on multiple clusters) is no longer naturally reproducible on this cluster after fixing the destination-broker-resolution bug above — source and destination entries for the same topic now correctly collapse onto the same real address instead of being artificially split by a `localhost` proxy address. It's still implemented (see `get_kafka_topic` in `kafka.py`) and would trigger if a topic genuinely exists on two distinct real clusters (this deployment only uses one, `10.2.0.163:29092`).

### `PATCH /kafka/topics/config`

Alter broker-side topic configuration. Target by exact `topic_name` (takes
precedence) or `service_instance` (all topics used by that instance, as
source or destination). Settings via named fields (snake_case, e.g.
`retention_ms`) or a raw `config` dict of broker keys (e.g.
`{"retention.ms": "86400000"}`); named fields win on conflict, with a warning
in the response.

```bash
# By topic name, named field
curl -s -X PATCH "http://localhost:8000/kafka/topics/config" \
  -H "Content-Type: application/json" \
  -d '{"topic_name": "<topic>", "segment_jitter_ms": <value>}'

# By service instance (affects every topic that instance uses)
curl -s -X PATCH "http://localhost:8000/kafka/topics/config" \
  -H "Content-Type: application/json" \
  -d '{"service_instance": "<instance_name>", "config": {"segment.jitter.ms": "<value>"}}'
```

**Example call & response (by topic_name, applying a test value):** [outputs/kafka_patch_by_topic_apply.json](outputs/kafka_patch_by_topic_apply.json)
**Example call & response (by topic_name, reverting to original):** [outputs/kafka_patch_by_topic_revert.json](outputs/kafka_patch_by_topic_revert.json)

---

## Snapshots

Persisted metrics history in PostgreSQL. This is what the hourly
`k8s-manager-snapshot` CronJob calls.

### `POST /snapshots`

Scrape metrics for every deployed instance right now and persist as one
snapshot batch.

```bash
curl -s -X POST "http://localhost:8000/snapshots"
```

**Example call & response:** [outputs/snapshots_create.json](outputs/snapshots_create.json)

### `GET /snapshots`

Paginated list of snapshot batches, newest first. Query params: `limit`
(default 20, max 200), `offset` (default 0).

```bash
curl -s "http://localhost:8000/snapshots"
```

**Example call & response:** [outputs/snapshots_list.json](outputs/snapshots_list.json)

### `GET /snapshots/{snapshot_id}`

Full per-instance details for one snapshot batch.

```bash
curl -s "http://localhost:8000/snapshots/<snapshot_id>"
```

**Example call & response:** [outputs/snapshots_detail.json](outputs/snapshots_detail.json)
