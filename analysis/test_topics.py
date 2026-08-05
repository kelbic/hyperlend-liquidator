"""Гигиена сигнатур событий: топики прибиты к КАНОНИЧЕСКИМ хэшам Aave v3.

Зачем отдельный тест. 04.08 сканер соседнего бота дал «ноль ликвидаций за 5.8 суток» —
и это был ЛОЖНЫЙ ноль: в фильтре стояла сигнатура другой версии протокола (v2, с хвостом
uint16). Правильная v3-сигнатура на том же окне дала 20 событий. Отказ молчаливый: getLogs
на несуществующий топик возвращает пустой список, неотличимый от «рынка нет», и весь вывод
о потоке денег строится на пустоте.

Тесты ниже сравнивают вычисленные топики с хэшами, снятыми с реальных логов Aave v3
(кросс-проверка по эксплореру). Опечатка в типе аргумента, лишний indexed или подсунутая
версия события ломают тест, а не тихо обнуляют выручку.
"""
import unittest

from analysis.protocols import (SIG_BORROW, SIG_LIQUIDATION_CALL, SIG_SUPPLY, TOPIC_BORROW,
                                TOPIC_LIQUIDATION_CALL, TOPIC_SUPPLY)

# Снято с живых логов Aave v3 (не вычислено этим же кодом — иначе тест проверял бы сам себя).
CANON = {
    "LiquidationCall": "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286",
    "Borrow": "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0",
    "Supply": "0x2b627736bca15cd5381dcf80b0bf11fd197d01a037c52b927a881a10fb73ba61",
}


class TopicHygiene(unittest.TestCase):
    def test_liquidation_call_is_v3_not_v2(self):
        self.assertEqual(TOPIC_LIQUIDATION_CALL.lower(), CANON["LiquidationCall"])
        # хвост v2 (…,uint16) даёт ДРУГОЙ топик и тихий ноль на getLogs
        self.assertNotIn("uint16", SIG_LIQUIDATION_CALL)

    def test_borrow_topic(self):
        self.assertEqual(TOPIC_BORROW.lower(), CANON["Borrow"])
        self.assertTrue(SIG_BORROW.endswith("uint16)"))   # v3 Borrow РЕФЕРАЛ несёт, в отличие от…

    def test_supply_topic(self):
        self.assertEqual(TOPIC_SUPPLY.lower(), CANON["Supply"])




class AddressChecksums(unittest.TestCase):
    """Все адреса в карте протокола обязаны нести валидную контрольную сумму EIP-55.

    05.08: пять из шестнадцати её не несли, и это была не косметика. HTTP-квотеры
    ВАЛИДИРУЮТ контрольную сумму: liqd.ag отвечал 400 на beHYPE и PT-19MAR2026, что в
    preflight читалось как «роута нет вообще». Отказ молчаливый и неотличим от честного
    отсутствия ликвидности — ровно тот класс, который уже стоил нам ложного нуля по
    сигнатурам событий. Замок держит его закрытым.
    """

    def test_all_token_addresses_are_valid_eip55(self):
        from analysis.keccak import keccak
        from analysis.protocols import TOKENS

        def eip55(addr: str) -> str:
            a = addr.lower().replace("0x", "")
            h = keccak(a.encode()).hex()
            return "0x" + "".join(c.upper() if c.isalpha() and int(h[i], 16) >= 8 else c
                                  for i, c in enumerate(a))

        bad = {s: (t["address"], eip55(t["address"])) for s, t in TOKENS.items()
               if t["address"] != eip55(t["address"])}
        self.assertEqual(bad, {}, f"битая контрольная сумма: {bad}")

if __name__ == "__main__":
    unittest.main()
