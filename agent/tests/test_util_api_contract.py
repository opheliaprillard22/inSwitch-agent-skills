"""Contract tests for workspace/script/util.py.

Based on the responses in k8s-manager-api-reference/outputs/
"""

import importlib.util
import io
import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUTS = REPO_ROOT / "k8s-manager-api-reference" / "outputs"
UTIL_PATH = REPO_ROOT / "workspace" / "script" / "util.py"


def _load_util():
    spec = importlib.util.spec_from_file_location("skill_util", UTIL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


util = _load_util()


def captured(name):
    with io.open(OUTPUTS / name, encoding="utf-8") as handle:
        return json.load(handle)


class RecordingTransport:
    """Stand-in for util._request that records calls and replays payloads."""

    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = []

    def __call__(self, method, endpoint, path, params=None, body=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "params": params or {},
                "body": body,
                "url": util._build_url(endpoint, path, params),
            }
        )
        if self.error is not None:
            raise self.error
        return self.responses.get((method, path), self.responses.get(path))


class UrlBuildingTests(unittest.TestCase):
    def test_status_filter_uses_the_only_param_the_api_defines(self):
        url = util._build_url("http://h:8000", "status", {"name": "object-speeds"})
        self.assertEqual(url, "http://h:8000/status?name=object-speeds")

    def test_booleans_render_lowercase(self):
        url = util._build_url("http://h:8000", "info/services", {"short_info": True})
        self.assertEqual(url, "http://h:8000/info/services?short_info=true")

    def test_none_params_are_dropped(self):
        url = util._build_url("http://h:8000", "status", {"name": None})
        self.assertEqual(url, "http://h:8000/status")

    def test_trailing_slash_does_not_eat_the_path(self):
        self.assertEqual(
            util._build_url("http://h:8000/", "info/object-speeds"),
            "http://h:8000/info/object-speeds",
        )


class EnvCanonicalizationTests(unittest.TestCase):
    """Real /status.env_vars values are all strings, encoded inconsistently."""

    def test_json_array_string_matches_python_list(self):
        self.assertTrue(
            util.env_values_match(["TRUCK", "CAR"], '["CAR","TRUCK"]')
        )

    def test_comma_separated_matches_list(self):
        self.assertTrue(
            util.env_values_match(["L1_n_ws", "L2_n_s"], "L1_N_WS,L2_N_S")
        )

    def test_zone_case_is_ignored(self):
        self.assertTrue(util.env_values_match("C5_w", "C5_W"))

    def test_scalar_matches_single_element_list(self):
        self.assertTrue(util.env_values_match("CAR", '["CAR"]'))

    def test_numbers_and_bools_as_strings(self):
        self.assertTrue(util.env_values_match(5.0, "5.0"))
        self.assertTrue(util.env_values_match(1, "1"))
        self.assertTrue(util.env_values_match(False, "False"))

    def test_different_values_do_not_match(self):
        self.assertFalse(util.env_values_match("CAR", "TRUCK"))
        self.assertFalse(util.env_values_match(["CAR"], ["CAR", "TRUCK"]))


class StatusShapeTests(unittest.TestCase):
    def test_wrapped_shape(self):
        payload = captured("status_all.json")
        self.assertEqual(len(util._instances_from_payload(payload)), payload["total"])

    def test_bare_instance_shape_is_not_silently_empty(self):
        # GET /status/{id} has no instances/total wrapper. Reading it with
        # .get("instances", []) would yield [] and read as "nothing deployed".
        bare = captured("status_single_instance.json")
        instances = util._instances_from_payload(bare)
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["instance_name"], bare["instance_name"])

    def test_readiness_follows_the_real_status_values(self):
        payload = captured("status_all.json")
        ready = [i for i in payload["instances"] if util.instance_is_ready(i)]
        scaled_to_zero = [i for i in payload["instances"] if not i["pods"]]
        self.assertTrue(ready)
        self.assertTrue(scaled_to_zero)
        for instance in scaled_to_zero:
            self.assertFalse(util.instance_is_ready(instance))

    def test_degraded_is_not_ready(self):
        self.assertFalse(
            util.instance_is_ready(captured("status_single_instance.json"))
        )


class IsServiceDeployedTests(unittest.TestCase):
    def setUp(self):
        self.status = captured("status_all.json")
        self._real_request = util._request

    def tearDown(self):
        util._request = self._real_request

    def _run(self, expected, payload=None):
        transport = RecordingTransport({("GET", "status"): payload or self.status})
        util._request = transport
        result = util.is_service_deployed("http://h:8000", expected)
        return result, transport

    def test_calls_status_with_name_filter_once(self):
        _, transport = self._run(
            {"service_name": "object-speeds", "env_vars": {"STRIDE": "1"}}
        )
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["path"], "status")
        self.assertEqual(call["params"], {"name": "object-speeds"})
        self.assertNotIn("service_name=", call["url"])

    def test_matches_a_real_instance_across_encodings(self):
        instance = next(
            i
            for i in self.status["instances"]
            if i["service_name"] == "object-speeds" and "OBJECT_TYPE_FILTER" in i["env_vars"]
        )
        deployed = json.loads(instance["env_vars"]["OBJECT_TYPE_FILTER"])
        result, _ = self._run(
            {
                "service_name": "object-speeds",
                # a Python list where the API stores a JSON array string
                "env_vars": {"OBJECT_TYPE_FILTER": deployed},
            }
        )
        self.assertTrue(result["is_deployed"])

    def test_no_match_reports_false(self):
        result, _ = self._run(
            {
                "service_name": "object-speeds",
                "env_vars": {"OBJECT_TYPE_FILTER": ["NOT-A-REAL-TYPE"]},
            }
        )
        self.assertFalse(result["is_deployed"])
        self.assertIsNone(result["matched_instance"])

    def test_matched_instance_is_compact(self):
        result, _ = self._run(
            {"service_name": "object-speeds", "env_vars": {"STRIDE": "1"}}
        )
        self.assertTrue(result["is_deployed"])
        matched = result["matched_instance"]
        # The full object carries kafka_topics and 11-field frequency_metrics.
        self.assertNotIn("frequency_metrics", matched)
        self.assertNotIn("kafka_topics", matched)
        self.assertEqual(list(matched["env_vars"]), ["STRIDE"])

    def test_bare_instance_payload_still_matches(self):
        bare = captured("status_single_instance.json")
        result, _ = self._run(
            {"service_name": bare["service_name"], "env_vars": {}}, payload=bare
        )
        self.assertTrue(result["is_deployed"])

    def test_requires_service_name(self):
        with self.assertRaises(ValueError):
            util.is_service_deployed("http://h:8000", {"env_vars": {}})


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self._real_request = util._request

    def tearDown(self):
        util._request = self._real_request

    def test_env_schema_surfaces_frozen_vars(self):
        payload = captured("info_single_service.json")
        transport = RecordingTransport({("GET", "info/object-speeds"): payload})
        util._request = transport
        schema = util.get_service_env_schema("http://h:8000", "object-speeds")
        self.assertEqual(transport.calls[0]["path"], "info/object-speeds")
        self.assertEqual(
            [v["name"] for v in schema["frozen_env_vars"]],
            [v["name"] for v in payload["frozen_env_vars"]],
        )
        self.assertTrue(schema["required_env_vars"])
        self.assertTrue(schema["optional_env_vars"])

    def test_env_schema_raises_value_error_on_404(self):
        util._request = RecordingTransport(
            error=util.ApiError(404, "Service 'nope' not found.", "http://h/info/nope")
        )
        with self.assertRaises(ValueError):
            util.get_service_env_schema("http://h:8000", "nope")

    def test_catalogue_uses_short_info(self):
        payload = captured("info_services_short.json")
        transport = RecordingTransport({("GET", "info/services"): payload})
        util._request = transport
        result = util.get_deployable_services_basic("http://h:8000")
        self.assertEqual(transport.calls[0]["params"], {"short_info": True})
        self.assertEqual(result["total"], payload["total"])
        self.assertEqual(
            sorted(result["services"][0]),
            ["dependencies", "description", "name"],
        )


class WriteSurfaceTests(unittest.TestCase):
    def setUp(self):
        self._real_request = util._request

    def tearDown(self):
        util._request = self._real_request

    def test_create_instance_body(self):
        transport = RecordingTransport(
            {("POST", "instances"): captured("instances_create.json")}
        )
        util._request = transport
        util.create_instance(
            "http://h:8000",
            "object-speeds",
            env_vars={"OBJECT_TYPE_FILTER": ["CAR"], "STRIDE": 1},
            node_name="node1-k8s",
            instance_prefix="doctest",
        )
        body = transport.calls[0]["body"]
        self.assertEqual(transport.calls[0]["method"], "POST")
        # Lists go out as compact JSON, the way GET /status reports them back.
        self.assertEqual(body["env_vars"]["OBJECT_TYPE_FILTER"], '["CAR"]')
        self.assertEqual(body["env_vars"]["STRIDE"], "1")
        self.assertTrue(body["metrics_trackable"])
        self.assertEqual(body["node_name"], "node1-k8s")
        self.assertEqual(body["instance_prefix"], "doctest")

    def test_update_instance_without_env_is_a_pure_restart(self):
        transport = RecordingTransport(
            {("PUT", "instances/x"): captured("instances_update_restart_only.json")}
        )
        util._request = transport
        util.update_instance("http://h:8000", "x")
        self.assertEqual(transport.calls[0]["method"], "PUT")
        self.assertEqual(transport.calls[0]["body"], {})

    def test_delete_tolerates_empty_body(self):
        transport = RecordingTransport({("DELETE", "instances/x"): None})
        util._request = transport
        result = util.delete_instance("http://h:8000", "x")
        self.assertEqual(transport.calls[0]["method"], "DELETE")
        self.assertTrue(result["deleted"])


if __name__ == "__main__":
    unittest.main()
