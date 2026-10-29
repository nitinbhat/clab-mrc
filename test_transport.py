import unittest

from transport import Selector


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.selector = Selector([{"ev": n} for n in range(1, 5)], seed=42)

    def test_sprays_every_ev_once_per_rotation(self):
        for _ in range(3):
            self.assertEqual({self.selector.choose() for _ in range(4)}, {1, 2, 3, 4})

    def test_timeout_removes_ev_from_current_rotation(self):
        self.selector.choose()
        failed = self.selector.order[-1]
        self.selector.timeout(failed)
        self.assertNotIn(failed, [self.selector.choose() for _ in range(12)])
        self.assertEqual(self.selector.stats[failed]["state"], "ASSUMED_BAD")

    def test_no_healthy_path_is_explicit_failure(self):
        for ev in range(1, 5):
            self.selector.timeout(ev)
        with self.assertRaisesRegex(RuntimeError, "No healthy paths"):
            self.selector.choose()


if __name__ == "__main__":
    unittest.main()
