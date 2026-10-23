import unittest

from smp_extractor.cli import _validate_codes
from smp_extractor.collector import DEFAULT_CODES


class CLITests(unittest.TestCase):
    def test_scope_defaults_to_the_six_configured_codes(self):
        self.assertEqual(_validate_codes(list(DEFAULT_CODES)), list(DEFAULT_CODES))

    def test_subset_is_allowed_and_duplicates_are_removed(self):
        self.assertEqual(
            _validate_codes(["32230155", "32230155", "32230183"]),
            ["32230155", "32230183"],
        )

    def test_code_outside_configured_scope_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the six-code scope"):
            _validate_codes(["99999999"])


if __name__ == "__main__":
    unittest.main()
