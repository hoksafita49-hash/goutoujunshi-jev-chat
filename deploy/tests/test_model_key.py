import importlib.util
from pathlib import Path
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / "set-model-key.py"
if HELPER.exists():
    spec = importlib.util.spec_from_file_location("model_key", HELPER)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)


class ModelKeyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(HELPER.exists(), "secure model-key installer is not implemented")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = Path(self.directory.name) / "gateway.env"
        self.original = (b"# private configuration\nGOUTOU_OPERATOR_TOKEN=preserve-this-token\n"
                         b"GOUTOU_MODEL_CONFIG=/etc/goutou/models.yaml\n"
                         b"DEEPSEEK_API_KEY=\nOTHER_API_KEY=preserve-other-key\n")
        self.env.write_bytes(self.original)

    def test_only_provider_key_changes_and_activation_sees_new_configuration(self):
        def activate():
            content = self.env.read_bytes()
            self.assertIn(b"DEEPSEEK_API_KEY=sk-local-test-only-123\n", content)
            self.assertIn(b"GOUTOU_OPERATOR_TOKEN=preserve-this-token\n", content)
            self.assertIn(b"OTHER_API_KEY=preserve-other-key\n", content)
            return True
        helper.configure_key(self.env, "sk-local-test-only-123", activate)
        self.assertEqual(self.env.read_bytes(), self.original.replace(
            b"DEEPSEEK_API_KEY=\n", b"DEEPSEEK_API_KEY=sk-local-test-only-123\n"))
        self.assertEqual(list(self.env.parent.glob(".gateway-env-*")), [])

    def test_invalid_input_never_changes_file_or_activates_service(self):
        for key in ("", "short", "sk-unsafe\nGOUTOU_OPERATOR_TOKEN=evil", "sk-unsafe\r", "sk-unsafe\x00", "sk-unsafe'quote", "sk-unsafe\\slash", "sk-unsafe space"):
            with self.subTest(case=len(key)):
                with self.assertRaises(ValueError):
                    helper.configure_key(self.env, key, lambda: self.fail("invalid key activated"))
                self.assertEqual(self.env.read_bytes(), self.original)

    def test_failed_restart_restores_previous_configuration(self):
        def activate():
            return self.env.read_bytes() == self.original
        with self.assertRaises(helper.ConfigurationError) as caught:
            helper.configure_key(self.env, "sk-never-print-this-123", activate)
        self.assertEqual(self.env.read_bytes(), self.original)
        self.assertNotIn("sk-never-print-this", str(caught.exception))
        self.assertEqual(list(self.env.parent.glob(".gateway-env-*")), [])

    def test_duplicate_key_assignments_are_rejected_without_mutation(self):
        duplicate = self.original + b"DEEPSEEK_API_KEY=another-value\n"
        self.env.write_bytes(duplicate)
        with self.assertRaises(ValueError):
            helper.configure_key(self.env, "sk-local-test-only-123", lambda: True)
        self.assertEqual(self.env.read_bytes(), duplicate)

    def test_alias_validation_failure_restores_previous_file_after_healthy_restart(self):
        with self.assertRaises(helper.ConfigurationError):
            helper.configure_key(self.env, "sk-local-test-only-123", lambda: True,
                                 verify=lambda: False)
        self.assertEqual(self.env.read_bytes(), self.original)

    def test_input_contract_rejects_unexpected_fields(self):
        with self.assertRaises(ValueError):
            helper.parse_input(b'{"key":"sk-local-test-only-123","GOUTOU_OPERATOR_TOKEN":"override"}')
        with self.assertRaises(ValueError):
            helper.parse_input(b"x" * 8193)


if __name__ == "__main__":
    unittest.main()
