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
        self.assertLess(len(compact["plans"]), len(big_payload["plans"]))


if __name__ == "__main__":
    unittest.main()
