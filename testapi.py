"""Probe the k8s-manager API with whatever is configured in .env.

Run from the repo root:  python testapi.py

ORDER MATTERS: .env must be loaded before `import util`, because util.py reads
its configuration into module globals at import time. Importing it first leaves
the credentials empty and every call returns 401.

Two independent auth gates:
  - HTTP Basic at the ingress -> needed for every request
  - X-Admin-Key in the app    -> writes only (POST/PUT/DELETE /instances,
    PATCH /kafka/topics/config, POST /snapshots, GET /admin/whoami)

Read-only: this script never deploys, updates or deletes anything.
"""

import sys

import dotenv

dotenv.load_dotenv()

sys.path.insert(0, "workspace/script")
import util  # noqa: E402  - must come after load_dotenv()


def show(label, fn):
    try:
        print("  %-32s OK    %s" % (label, fn()))
    except util.ApiError as exc:
        print("  %-32s %-5s %s" % (label, exc.status, exc.detail[:95]))
    except Exception as exc:  # pragma: no cover - diagnostic script
        print("  %-32s ERR   %s: %s" % (label, type(exc).__name__, str(exc)[:80]))


print("endpoint   :", util.DEFAULT_SERVICE_ENDPOINT)
print("basic auth :", bool(util.K8S_MANAGER_USERNAME and util.K8S_MANAGER_PASSWORD))
print("admin key  :", bool(util.K8S_MANAGER_ADMIN_KEY))

print("\nreads (Basic auth only):")
show("GET /health", util.get_health)
show(
    "GET /info/services",
    lambda: "%d services" % util.get_deployable_services_basic()["total"],
)
show(
    "GET /status",
    lambda: "%d instances" % util.get_running_instances()["total"],
)
show(
    "GET /info/object-speeds",
    lambda: "%d required env vars"
    % len(util.get_service_env_schema(None, "object-speeds")["required_env_vars"]),
)

print("\nwrites (also need X-Admin-Key):")
show("GET /admin/whoami", lambda: util._request("GET", None, "admin/whoami"))
print("  no write is attempted; whoami only reports whether the key is accepted")
