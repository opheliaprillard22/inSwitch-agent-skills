import json
import unittest

from core.runner import _compact_tool_output


class RunnerContextCompactionTests(unittest.TestCase):
    def test_compacts_large_plan_payloads(self):
        big_payload = {
            "plans": [
                {
                    "plan_name": f"Alternative {i}",
                    "services": [
                        {
                            "service_name": "object_in_zone_detection",
                            "action": "new_instance",
                            "env_vars": {
                                "OBJECT_TYPE_FILTER": "PERSON",
                                "ZONES_OF_INTEREST": "C5_W",
                            },
                        }
                        for _ in range(50)
                    ],
                }
                for i in range(1, 60)
            ]
        }

        compact = _compact_tool_output(big_payload)

        self.assertIn("summary", compact)
        self.assertEqual(compact["summary"]["total_plans"], 59)
        self.assertLess(len(str(compact)), 12000)
        self.assertLess(len(str(compact["plans"])), len(str(big_payload["plans"])))
        for preview in compact["plans"]:
            self.assertNotIn("services", preview)
            self.assertIn("service_count", preview)

    def test_run_python_output_is_kept_not_dropped(self):
        """run_python returns {stdout, stderr, exit_code}.

        None of those are the structured keys this function reduces, so an
        oversized result used to collapse to the summary alone. The model then
        got no output at all and went hunting for a log file that does not
        exist.
        """
        stdout = "".join("line %d: %s\n" % (i, "x" * 80) for i in range(400))
        result = {"stdout": stdout, "stderr": "", "exit_code": 0}

        compact = _compact_tool_output(result)

        self.assertIn("stdout", compact)
        self.assertEqual(compact["exit_code"], 0)
        self.assertTrue(compact["summary"]["truncated"])
        self.assertGreater(compact["summary"]["stdout_truncated_chars"], 0)
        # head and tail both survive, so the model can see how it started and ended
        self.assertTrue(compact["stdout"].startswith("line 0:"))
        self.assertIn("line 399:", compact["stdout"])
        self.assertIn("characters dropped", compact["stdout"])
        self.assertLess(len(json.dumps(compact)), 12000)

    def test_small_run_python_output_is_untouched(self):
        result = {"stdout": "hello\n", "stderr": "", "exit_code": 0}
        self.assertIs(_compact_tool_output(result), result)


if __name__ == "__main__":
    unittest.main()
