import unittest

from src.cart import Coupon, Item, subtotal, total


class CartTest(unittest.TestCase):
    def test_subtotal(self):
        self.assertEqual(subtotal([Item("笔", 300, 2), Item("本", 1250, 1)]), 1850)

    def test_coupon_applies_above_threshold(self):
        self.assertEqual(total([Item("书", 6000, 2)], Coupon(10000, 1500)), 10500)

    def test_coupon_applies_at_exactly_the_threshold(self):
        #  "满 100 减 15"：正好 100 也算满
        self.assertEqual(total([Item("书", 5000, 2)], Coupon(10000, 1500)), 8500)

    def test_coupon_ignored_below_threshold(self):
        self.assertEqual(total([Item("书", 4000, 2)], Coupon(10000, 1500)), 8000)

    def test_total_never_negative(self):
        self.assertEqual(total([Item("糖", 100, 1)], Coupon(50, 500)), 0)
