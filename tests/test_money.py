import unittest
from fractions import Fraction

from helpers import ROOT  # noqa: F401  (sys.path setup)

from recon.money import div_half_even, parse_int_text, to_usd_micro


class HalfEvenRounding(unittest.TestCase):
    def test_worked_example_1_rate_one_half(self):
        # +1,+3,-1,-3 at 1/2 -> 0, 2, 0, -2 ; sum 0 (never round the total)
        vals = [to_usd_micro(a, 1, 2) for a in (1, 3, -1, -3)]
        self.assertEqual(vals, [0, 2, 0, -2])
        self.assertEqual(sum(vals), 0)

    def test_ties_to_even_positive_and_negative_symmetric(self):
        cases = {(1, 2): 0, (3, 2): 2, (5, 2): 2, (7, 2): 4, (-1, 2): 0, (-3, 2): -2, (-5, 2): -2, (-7, 2): -4}
        for (n, d), want in cases.items():
            self.assertEqual(div_half_even(n, d), want, (n, d))
            self.assertEqual(div_half_even(-n, d), -want, (n, d))

    def test_non_ties_round_to_nearest(self):
        self.assertEqual(div_half_even(7, 4), 2)      # 1.75
        self.assertEqual(div_half_even(-7, 4), -2)
        self.assertEqual(div_half_even(5, 4), 1)      # 1.25
        self.assertEqual(div_half_even(-5, 4), -1)
        self.assertEqual(div_half_even(1, 4), 0)
        self.assertEqual(div_half_even(-1, 4), 0)
        self.assertEqual(div_half_even(99 * 3, 2), 148)  # 148.5 -> 148 (even)
        self.assertEqual(div_half_even(99, 2), 50)       # 49.5 -> 50 (even)

    def test_matches_fraction_round_on_random_grid(self):
        import random

        rnd = random.Random(1234)
        for _ in range(20000):
            n = rnd.randint(-10**7, 10**7)
            d = rnd.randint(1, 10**4)
            self.assertEqual(div_half_even(n, d), round(Fraction(n, d)), (n, d))

    def test_exact_above_double_precision(self):
        big = 9_007_199_254_740_993  # 2**53 + 1
        self.assertEqual(to_usd_micro(big, 1, 1), big)
        self.assertEqual(to_usd_micro(big, 123457, 7), round(Fraction(big * 123457, 7)))
        huge = 10**40 + 1
        self.assertEqual(to_usd_micro(huge, 3, 1), 3 * huge)
        self.assertEqual(to_usd_micro(-huge, 1, 1), -huge)

    def test_denominator_must_be_positive(self):
        with self.assertRaises(ValueError):
            div_half_even(1, 0)


class IntegerTextParsing(unittest.TestCase):
    def test_accepts_signed_base10(self):
        self.assertEqual(parse_int_text("99"), 99)
        self.assertEqual(parse_int_text("-0"), 0)
        self.assertEqual(parse_int_text("+42"), 42)
        self.assertEqual(parse_int_text("-9007199254740993"), -9007199254740993)
        self.assertEqual(parse_int_text("00012"), 12)

    def test_rejects_everything_else(self):
        for bad in ("", " 1", "1 ", "1_000", "1e3", "0x10", "1.0", "abc", None, 5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_int_text(bad)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
