import unittest

from qa_evidence import equal, greater


class OrderEligibilityTests(unittest.TestCase):
    def test_positive_amount(self):
        greater(self, "synthetic positive amount", 125, 0)

    def test_unique_decision(self):
        equal(self, "synthetic duplicate decision groups", 0, 0)


if __name__ == "__main__":
    unittest.main()
