#!/usr/bin/env python3
"""Свежий подписанный пакет RedStone -> calldata адаптера -> fork-тест ForkSelfPush.

Отвечает на один вопрос: пуск цены в адаптер ПЕРМИШЕНЛЕСС для нас или нет. Проверка идёт
дельтой состояния на форке последнего блока (не кодом возврата: адаптер глотает отказ в
событие и возвращает success — именно так родился неверный вывод 04.08).

Пакеты живут ~3 минуты, поэтому fetch и forge запускаются одной командой:
    python3 -m analysis.selfpush_probe [FEED] [ADAPTER]
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from analysis import redstone

ADAPTERS = {
    "A": "0xe4ae88743c3834d0c492eabc47384c84bcadc6a6",  # HYPE/BTC/USDT/kHYPE_FUNDAMENTAL/...
    "B": "0x24c8964338deb5204b096039147b8e8c3aea42cc",  # ETH
}
RPC = os.environ.get("HYPE_RPC", "https://rpc.hyperliquid.xyz/evm")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    feed = sys.argv[1] if len(sys.argv) > 1 else "HYPE"
    adapter = sys.argv[2] if len(sys.argv) > 2 else ADAPTERS["A"]

    pkgs = redstone.fetch_feed_packages(feed, timeout=5.0, max_age_ms=60_000)
    cd = redstone.build_push_calldata([feed], pkgs)
    ts = max(int(p["timestampMilliseconds"]) for p in pkgs)
    age = time.time() * 1000 - ts
    signers = sorted({redstone._recover_signer(*redstone._serialize_package(p)) for p in pkgs})
    print(f"фид {feed}: {len(pkgs)} пакетов, ts={ts} (возраст {age/1000:.1f}с), "
          f"calldata {len(cd)}Б, адаптер {adapter}")
    print(f"подписанты: {len(signers)} уник., все авторизованы: "
          f"{all(s in redstone.AUTHORISED_SIGNERS for s in signers)}")

    env = dict(os.environ,
               HYPE_RPC=RPC,
               PUSH_ADAPTER=adapter,
               PUSH_CALLDATA="0x" + cd.hex(),
               PUSH_FEED=feed,
               PUSH_PKG_TS_MS=str(ts))
    return subprocess.run(
        ["forge", "test", "--match-path", "test/ForkSelfPush.t.sol", "-vv"],
        cwd=os.path.join(ROOT, "contracts"), env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
