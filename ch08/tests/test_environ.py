import unittest

from lib import data, environ


class TestEnv(unittest.TestCase):
    def test_simple(self):
        prices = data.load_relative("ch08/data/YNDX_160101_161231.csv")
        env = environ.StocksEnv({"YNDX": prices})
        s = env.reset()
        print(s)
