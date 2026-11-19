"""Plan generation must not depend on the hash seed.

Python randomises string hashes per process, which changes set and frozenset
iteration order. run_python starts a fresh interpreter for every snippet, so
an ordering that varies between processes means the user approves
"Alternative 3" from one ordering and a later snippet deploys plans[2] from a
different one. These tests run the generator in subprocesses with explicit,
differing PYTHONHASHSEED values and require identical output.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UTIL_PATH = REPO_ROOT / "workspace" / "script" / "util.py"

SCRIPT = """
import importlib.util, json, os
os.environ["K8S_MANAGER_DEBUG"] = "0"
spec = importlib.util.spec_from_file_location("u", r"{util}")
u = importlib.util.module_from_spec(spec); spec.loader.exec_module(u)
variants = [
    {{"service_name": "object-in-zone-detection",
      "env_vars": {{"OBJECT_TYPE_FILTER": "PERSON", "ZONES_OF_INTEREST": "C5_w"}}}},
    {{"service_name": "object-in-zone-detection",
      "env_vars": {{"OBJECT_TYPE_FILTER": "TRUCK,CAR,TWO-WHEELER,UNKNOWN",
                   "ZONES_OF_INTEREST": "L1_n_ws,L2_n_s"}}}},
]
plans = u.generate_deployment_plans(["crosswalk-safety-alert"], variants)["plans"]
print(json.dumps([p["services"] for p in plans], sort_keys=True))
"""


def run_with_seed(seed):
    env = dict(os.environ, PYTHONHASHSEED=str(seed), K8S_MANAGER_DEBUG="0")
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT.format(util=UTIL_PATH)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    return json.loads(proc.stdout)


class PlanDeterminismTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = run_with_seed(1)
        cls.b = run_with_seed(999)

    def test_same_plans_in_the_same_order_under_different_hash_seeds(self):
        self.assertEqual(len(self.a), len(self.b))
        self.assertEqual(self.a, self.b, "plan ordering depends on the hash seed")

    def test_alternative_n_is_the_same_plan_in_both_runs(self):
        for index in (0, 1, len(self.a) // 2, len(self.a) - 1):
            self.assertEqual(
                self.a[index], self.b[index], f"Alternative {index + 1} differs"
            )

    def test_plans_are_ranked_fewest_instances_first(self):
        def detections(services):
            return sum(
                1 for s in services if s["service_name"] == "object-in-zone-detection"
            )

        counts = [detections(services) for services in self.a]
        self.assertEqual(counts, sorted(counts), "plans are not ordered by size")
        self.assertEqual(counts[0], 1, "the single-instance plan should rank first")


if __name__ == "__main__":
    unittest.main()
