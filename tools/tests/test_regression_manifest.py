"""Protect required PR regressions from accidental profile/discovery omissions."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PATHS = (
    "tests/unit_tests/harness_providers/test_native_goal_readmission.py",
    "tests/unit_tests/harness/prompts/test_context_file_cache.py",
    "tests/unit_tests/harness/subagent_runtime/test_owned_operation_exit.py",
    "tests/unit_tests/agent_teams/harness/test_mandatory_authority.py",
    "tests/unit_tests/harness/security/test_mandatory_authorization.py",
    "tests/unit_tests/core/foundation/tool/test_final_authority.py",
    "tests/unit_tests/core/foundation/llm/test_model_request_authority.py",
    "tests/unit_tests/harness/test_model_materializer.py",
    "tests/unit_tests/agent_teams/harness/test_execution_origin.py",
    "tests/unit_tests/agent_teams/harness/test_inner_event_origin.py",
    "tests/unit_tests/agent_teams/test_member_record_authority.py",
    "tests/unit_tests/agent_teams/test_member_effect_authority.py",
    "tests/unit_tests/harness/tools/test_builtin_final_authority.py",
    "tests/unit_tests/harness_providers/test_tool_authorizer.py",
    "tests/unit_tests/harness_providers/test_native_host.py",
    "tests/unit_tests/harness_providers/test_opencode.py",
    "tests/unit_tests/harness_providers/test_opencode_preflight.py",
    "tests/unit_tests/harness_providers/test_opencode_model_gateway.py",
    "tests/unit_tests/harness_protocol",
    "tests/unit_tests/harness/goal",
    "tests/unit_tests/harness/test_task_completion_extensions.py",
    "tests/unit_tests/harness/test_deep_agent_interaction.py",
    "tests/unit_tests/harness/test_deep_agent_round_stop.py",
    "tests/unit_tests/harness/test_deep_agent_round_origin.py",
    "tests/unit_tests/harness/test_deep_agent_event_executor.py",
    "tests/unit_tests/harness/test_deep_agent_rail_event_routing.py",
    "tests/unit_tests/harness/test_deep_agent_stream_aclose.py",
    "tests/unit_tests/harness_providers/test_codex.py",
    "tests/unit_tests/harness_providers/test_codex_security.py",
    "tests/unit_tests/harness_providers/test_codex_stop_confirmation.py",
    "tests/unit_tests/harness_providers/test_codex_process_scope.py",
)


class TestRegressionManifest(unittest.TestCase):
    """Check the shipping manifest, independently of the test runner."""

    def setUp(self) -> None:
        manifest = json.loads((ROOT / "test-manifest.json").read_text())
        suites = {suite["id"]: suite for suite in manifest["suites"]}
        self.suites = [suites[name] for name in manifest["profiles"]["pr-stable"]["suites"]]

    def test_required_regressions_are_discovered_and_executed(self) -> None:
        """Both commands must include each required regression exactly once."""
        for path in REQUIRED_PATHS:
            with self.subTest(path=path):
                self.assertTrue((ROOT.parent / path).exists())
                matches = [suite for suite in self.suites if path in suite["command"]]
                self.assertEqual(len(matches), 1, f"missing or duplicate execution entry: {path}")
                self.assertIn(path, matches[0]["discover"]["command"])
                self.assertTrue(matches[0]["required"])

    def test_stable_keeps_local_service_and_system_tests_separate(self) -> None:
        """Optional SDK and loopback qualification must stay outside stable."""
        for suite in self.suites:
            with self.subTest(suite=suite["id"]):
                self.assertFalse(suite.get("services"))
                self.assertNotIn("local_service", suite.get("capabilities", []))
                for command in (suite["command"], suite["discover"]["command"]):
                    self.assertFalse(any(arg.startswith("tests/system_tests") for arg in command))


if __name__ == "__main__":
    unittest.main()
