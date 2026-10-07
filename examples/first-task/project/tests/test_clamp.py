import unittest

from mathutil import clamp


class ClampTest(unittest.TestCase):
    def test_value_inside_range_is_unchanged(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_value_below_low_snaps_to_low(self):
        self.assertEqual(clamp(-3, 0, 10), 0)

    def test_value_above_high_snaps_to_high(self):
        self.assertEqual(clamp(42, 0, 10), 10)


if __name__ == "__main__":
    unittest.main()
