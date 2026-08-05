"""eMode-бонус: чей бонус применит пул к паре (заёмщик, залог).

Числа взяты из форк-экзамена 05.08 (блок 42352741, заёмщик 0x1d7afab9…, категория 5):
модель на резервном бонусе 11000 ожидала 9,750.38 kHYPE, пул отдал 9,347.84 — ровно столько,
сколько даёт бонус категории 10500. Тесты фиксируют именно этот выбор, потому что цена ошибки
не «неточность», а гарантированный реверт свопа на завышенном amountIn.
"""
import unittest

from analysis.aave import emode_bonus, size_liquidation

KHYPE = "0xfd739d4e423301ce9385c1fb8850539d657c296d"
WSTHYPE = "0x94e8396e0869c9f2200760af0621afd240e1cf38"
# категория 5 «плечевой стейкинг»: маска залогов = биты {8,10,16}, kHYPE = индекс 8
CAT5 = {"ltv": 8150, "liquidation_threshold": 8700, "liquidation_bonus": 10500,
        "collateral_bitmap": 0x10500}
RIDX = {KHYPE: 8, WSTHYPE: 9}


class EModeBonus(unittest.TestCase):
    def test_emode_overrides_reserve_bonus(self):
        self.assertEqual(emode_bonus(5, KHYPE, 11000, {5: CAT5}, RIDX), 10500)

    def test_no_emode_keeps_reserve_bonus(self):
        self.assertEqual(emode_bonus(0, KHYPE, 11000, {5: CAT5}, RIDX), 11000)

    def test_collateral_outside_category_keeps_reserve_bonus(self):
        # wstHYPE (индекс 9) не входит в маску {8,10,16} — на него eMode не распространяется
        self.assertEqual(emode_bonus(5, WSTHYPE, 11500, {5: CAT5}, RIDX), 11500)

    def test_unknown_category_falls_back(self):
        self.assertEqual(emode_bonus(7, KHYPE, 11000, {5: CAT5}, RIDX), 11000)

    def test_unknown_reserve_index_falls_back(self):
        self.assertEqual(emode_bonus(5, KHYPE, 11000, {5: CAT5}, {}), 11000)

    def test_string_keyed_cache_from_json(self):
        # книга сериализуется в JSON, где ключи словаря становятся строками
        self.assertEqual(emode_bonus(5, KHYPE, 11000, {"5": CAT5}, RIDX), 10500)


class SeizeMatchesChain(unittest.TestCase):
    """Замер с форка: 7,768.9615 WHYPE долга -> пул изъял 9,347.8395 kHYPE (событие
    LiquidationCall, tx 0xfc2f3afe…). Цены оракула на том блоке: WHYPE 57.08803492,
    kHYPE 49.58075173 (kHYPE уронен мок-фасадом ради ухода под воду)."""

    def test_bonus_10500_reproduces_chain(self):
        sz = size_liquidation(
            debt_wei=10 ** 30, debt_dec=18, debt_price=5708803492,
            coll_wei=10 ** 30, coll_dec=18, coll_price=4958075173,
            bonus_bps=10500, fee_bps=1000,
            total_debt_base=10 ** 18, hf_1e18=9 * 10 ** 17)
        ratio = sz["seized"] / sz["debt_pulled"] * (4958075173 / 5708803492)
        self.assertAlmostEqual(ratio, 1.045, places=3)     # 1 + 5% * (1 - 10%)

    def test_bonus_11000_would_overstate(self):
        sz = size_liquidation(
            debt_wei=10 ** 30, debt_dec=18, debt_price=5708803492,
            coll_wei=10 ** 30, coll_dec=18, coll_price=4958075173,
            bonus_bps=11000, fee_bps=1000,
            total_debt_base=10 ** 18, hf_1e18=9 * 10 ** 17)
        ratio = sz["seized"] / sz["debt_pulled"] * (4958075173 / 5708803492)
        self.assertAlmostEqual(ratio, 1.09, places=3)
        # завышение ровно то, что уронило своп: 1.09/1.045 - 1 = 4.3%
        self.assertGreater(1.09 / 1.045 - 1, 0.04)


if __name__ == "__main__":
    unittest.main()
