from collections import UserDict
from unittest import TestCase

from scheduler.policies import POLICIES, REQUIRED_KEYS, get_policy_weights


class TestPolicyWeightsAcceptance(TestCase):
    def test_custom_weights_are_required_before_mapping_validation(self):
        for weights in (None, {}, []):
            with self.subTest(weights=weights):
                with self.assertRaises(ValueError) as raised:
                    get_policy_weights("custom", weights)
                self.assertEqual(
                    str(raised.exception),
                    "custom_weights required for 'custom' policy",
                )

    def test_custom_weights_must_be_a_mapping(self):
        with self.assertRaises(ValueError) as raised:
            get_policy_weights("custom", [1])
        self.assertIn("custom_weights must be a mapping", str(raised.exception))

    def test_unknown_keys_are_reported_before_invalid_values_and_sorted_by_string(self):
        keys = tuple(REQUIRED_KEYS)
        weights = {"w_carbn": "0.5", 10: 1, keys[0]: 1}

        with self.assertRaises(ValueError) as raised:
            get_policy_weights("custom", weights)

        message = str(raised.exception)
        self.assertIn("Unknown weight keys:", message)
        self.assertIn("w_carbn", message)
        self.assertLess(message.index("10"), message.index("w_carbn"))
        self.assertNotIn("must be a number", message)

    def test_custom_values_must_be_numbers_and_booleans_are_rejected(self):
        key = tuple(REQUIRED_KEYS)[0]
        for value in ("0.5", True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    get_policy_weights("custom", {key: value})
                message = str(raised.exception)
                self.assertIn("must be a number", message)
                self.assertIn(str(key), message)

    def test_custom_values_must_be_finite(self):
        key = tuple(REQUIRED_KEYS)[0]
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    get_policy_weights("custom", {key: value})
                message = str(raised.exception)
                self.assertIn("must be finite", message)
                self.assertIn(str(key), message)

    def test_negative_values_keep_the_existing_error(self):
        key = tuple(REQUIRED_KEYS)[0]
        with self.assertRaises(ValueError) as raised:
            get_policy_weights("custom", {key: -1})
        self.assertEqual(str(raised.exception), "Weights cannot be negative")

    def test_zero_total_keeps_the_existing_error(self):
        key = tuple(REQUIRED_KEYS)[0]
        with self.assertRaises(ValueError) as raised:
            get_policy_weights("custom", {key: 0})
        self.assertEqual(str(raised.exception), "Weights must not all be zero")

    def test_all_zero_required_values_keep_the_existing_error(self):
        weights = {key: 0 for key in REQUIRED_KEYS}
        with self.assertRaises(ValueError) as raised:
            get_policy_weights("custom", weights)
        self.assertEqual(str(raised.exception), "Weights must not all be zero")

    def test_missing_required_keys_default_to_zero_and_results_follow_required_key_order(self):
        keys = tuple(REQUIRED_KEYS)
        first_key = keys[0]
        result = get_policy_weights("custom", {first_key: 4})

        self.assertIs(type(result), dict)
        self.assertEqual(tuple(result), keys)
        self.assertEqual(
            result,
            {key: (1.0 if key == first_key else 0.0) for key in keys},
        )
        self.assertTrue(all(type(value) is float for value in result.values()))

    def test_mixed_integer_and_float_values_normalize_to_one(self):
        keys = tuple(REQUIRED_KEYS)
        first_key, second_key = keys[:2]
        weights = UserDict({first_key: 1, second_key: 2.0})

        result = get_policy_weights("custom", weights)

        expected = {
            key: (1 / 3 if key == first_key else 2.0 / 3 if key == second_key else 0.0)
            for key in keys
        }
        self.assertIs(type(result), dict)
        self.assertIsNot(result, weights)
        self.assertEqual(tuple(result), keys)
        self.assertEqual(result, expected)
        self.assertEqual(sum(result.values()), 1.0)
        self.assertTrue(all(type(value) is float for value in result.values()))

    def test_named_policies_return_equal_independent_dicts(self):
        for name in ("speed", "eco", "balanced"):
            with self.subTest(policy=name):
                result = get_policy_weights(name)
                self.assertIs(type(result), dict)
                self.assertEqual(result, POLICIES[name])
                self.assertIsNot(result, POLICIES[name])

    def test_mutating_eco_result_does_not_change_shared_policy(self):
        original = POLICIES["eco"].copy()
        result = get_policy_weights("eco")
        result[tuple(REQUIRED_KEYS)[0]] = object()
        self.assertEqual(POLICIES["eco"], original)

    def test_unknown_policy_keeps_the_named_error(self):
        with self.assertRaises(ValueError) as raised:
            get_policy_weights("unsupported")
        message = str(raised.exception)
        self.assertTrue(message.startswith("Unknown policy: "))
        self.assertIn("unsupported", message)
