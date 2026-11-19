import unittest

from src.stock import put_back, take


class StockTest(unittest.TestCase):
    def test_take_and_put_back(self):
        stock = {"笔": 3}
        take(stock, "笔", 2)
        put_back(stock, "笔", 1)
        self.assertEqual(stock, {"笔": 2})

    def test_take_more_than_there_is(self):
        with self.assertRaises(ValueError):
            take({"笔": 1}, "笔", 2)
