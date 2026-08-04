"""RedStone self-push: fetch signed price packages from the public gateways and build calldata for
MultiFeedAdapter.updateDataFeedsValuesPartial(bytes32[]) — the atomic push-and-liquidate technique
the 04.08 race analysis proved the field leader uses (100% of his wins carry lag=0: he never waits
for the relayer).

Wire format reverse-engineered from the operator's own transactions and verified byte-exactly:
recovering the signature over OUR serialization of his packages yields the canonical
redstone-primary-prod signers (0x8BB8F32D…, 0xdEB22f54…, 0x51Ce04Be…). Layout per package:

    [dataPoints: feedId(32, utf8 right-padded) + value(32, big-endian, price*1e8)] * n
    timestampMilliseconds(6) + valueByteSize(4) + dataPointsCount(3) + signature(65)

payload = packages + packagesCount(2) + unsignedMetadata + unsignedMetadataSize(3) + MARKER(9)

The adapter proxy (0xe4ae8874… HYPE/BTC/USDT/kHYPE_FUNDAMENTAL, 0x24c89643… ETH) requires
getUniqueSignersThreshold()=3 unique authorised signers per feed and a data timestamp newer than
the stored one (~3 min freshness window both ways). The push is PERMISSIONLESS — proven both by
the operator's EOA (not a relayer) and by our own eth_call.
"""
from __future__ import annotations

import base64
import gzip
import json
import time
import urllib.request

from analysis.keccak import keccak, selector

# Public gateways, tried in order. Same data, independent operators.
GATEWAYS = [
    "https://oracle-gateway-1.a.redstone.finance/data-packages/latest/redstone-primary-prod",
    "https://oracle-gateway-2.a.redstone.finance/data-packages/latest/redstone-primary-prod",
]

# redstone-primary-prod authorised signers — verified on-chain 04.08 via
# getAuthorisedSignerIndex(address) on the HyperLend adapter (indexes 0..4, threshold 3).
AUTHORISED_SIGNERS = {
    "0x8bb8f32df04c8b654987daaed53d6b6091e3b774",
    "0xdeb22f54738d54976c4c0fe5ce6d408e40d88499",
    "0x51ce04be4b3e32572c4ec9135221d0691ba7d202",
    "0xdd682daec5a90dd295d14da4b0bec9281017b5be",
    "0x9c5ae89c4af6aa32ce58588dbaf90d18a855b6de",
}
UNIQUE_SIGNERS_THRESHOLD = 3

MARKER = bytes.fromhex("000002ed57011e0000")
VALUE_BS = 32
SEL_UPDATE_PARTIAL = selector("updateDataFeedsValuesPartial(bytes32[])")


class RedstoneError(RuntimeError):
    pass


def _http_get_json(url: str, timeout: float) -> dict:
    # gzip: гейтвей отдаёт полный снапшот всех фидов (~1.7MB), сжатие режет его до ~350KB —
    # на каденсе spec-вотчера это разница между 15 и 3 GB/день трафика VPS.
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw)


def fetch_latest_all(timeout: float = 3.0) -> dict:
    """Один GET = весь снапшот redstone-primary-prod (фильтр-параметры гейтвей игнорирует —
    проверено 04.08, dataFeedIds в query не сужает ответ). Кормит кэш bot/spec.py; для
    разовых нужд одного фида есть fetch_feed_packages."""
    last_err: Exception | None = None
    for gw in GATEWAYS:
        try:
            return _http_get_json(gw, timeout)
        except Exception as e:
            last_err = e
    raise RedstoneError(f"all gateways failed: {last_err}")


def _recover_signer(body: bytes, sig: bytes) -> str | None:
    """Recover the signer of a package body (the exact bytes that precede the signature)."""
    try:
        from eth_account import Account
        return Account._recover_hash(keccak(body), signature=sig).lower()
    except Exception:
        return None


def _serialize_package(pkg: dict) -> tuple[bytes, bytes] | None:
    """gateway package dict -> (body, signature). None if the package is malformed."""
    try:
        points = pkg["dataPoints"]
        body = b""
        for dp in points:
            fid = dp["dataFeedId"].encode()
            if len(fid) > 32:
                return None
            # gateway values are decimal prices; on-chain representation is price * 1e8. round()
            # is mandatory: float noise (59064.207000000006) must not shift the signed integer.
            val = int(round(float(dp["value"]) * 1e8))
            body += fid.ljust(32, b"\0") + val.to_bytes(VALUE_BS, "big")
        body += int(pkg["timestampMilliseconds"]).to_bytes(6, "big")
        body += VALUE_BS.to_bytes(4, "big")
        body += len(points).to_bytes(3, "big")
        sig = base64.b64decode(pkg["signature"])
        if len(sig) != 65:
            return None
        return body, sig
    except Exception:
        return None


def build_payload(packages: list[dict], require_signers: bool = True) -> bytes:
    """Serialize gateway packages into the RedStone payload tail. Each package is validated by
    signature recovery against the authorised signer set — a package that does not recover to a
    known signer is DROPPED (never silently included: a bad byte in one package reverts the whole
    push on-chain, and the failure would be indistinguishable from a lost race)."""
    blobs: list[bytes] = []
    seen: set[str] = set()
    for pkg in packages:
        ser = _serialize_package(pkg)
        if ser is None:
            continue
        body, sig = ser
        signer = _recover_signer(body, sig)
        if require_signers:
            if signer is None or signer not in AUTHORISED_SIGNERS or signer in seen:
                continue
            seen.add(signer)
        blobs.append(body + sig)
    if require_signers and len(blobs) < UNIQUE_SIGNERS_THRESHOLD:
        raise RedstoneError(f"only {len(blobs)} valid unique-signer packages, "
                            f"need {UNIQUE_SIGNERS_THRESHOLD}")
    unsig = f"{int(time.time()*1000)}#0.9.0#hl-bot".encode()
    return (b"".join(blobs) + len(blobs).to_bytes(2, "big")
            + unsig + len(unsig).to_bytes(3, "big") + MARKER)


def fetch_feed_packages(feed_id: str, timeout: float = 1.5,
                        max_age_ms: int = 120_000) -> list[dict]:
    """Fetch the latest signed packages for one feed from the first gateway that answers. Packages
    older than max_age_ms are rejected outright — the adapter enforces a freshness window and a
    stale push burns the whole shot."""
    last_err: Exception | None = None
    for gw in GATEWAYS:
        try:
            data = _http_get_json(gw, timeout)
            pkgs = data.get(feed_id) or []
            now_ms = time.time() * 1000
            fresh = [p for p in pkgs
                     if now_ms - float(p.get("timestampMilliseconds", 0)) <= max_age_ms]
            if fresh:
                return fresh
            last_err = RedstoneError(f"{gw}: no fresh {feed_id} packages")
        except Exception as e:
            last_err = e
    raise RedstoneError(f"all gateways failed for {feed_id}: {last_err}")


def build_push_calldata(feed_ids: list[str], packages: list[dict],
                        require_signers: bool = True) -> bytes:
    """Full calldata for adapter.updateDataFeedsValuesPartial(bytes32[]) with the RedStone payload
    appended — ready to be sent to the adapter proxy (or handed to the liquidator contract as
    `oracleCalldata`).

    require_signers=False ОБЯЗАТЕЛЕН для мульти-фидового пуша с ПРЕДВАРИТЕЛЬНО проверенными
    пакетами: build_payload считает unique-подписантов на весь payload, а каждый фид подписан
    одними и теми же пятью нодами — строгий режим выбросил бы все пакеты второго фида и пуш
    ревертнулся бы порогом адаптера (он считает порог пер-фид сам)."""
    ids = [f.encode().ljust(32, b"\0") for f in feed_ids]
    if any(len(f.encode()) > 32 for f in feed_ids):
        raise RedstoneError("feed id longer than 32 bytes")
    head = bytes.fromhex(SEL_UPDATE_PARTIAL[2:])
    # abi: offset(32) + len(32) + ids
    abi = (32).to_bytes(32, "big") + len(ids).to_bytes(32, "big") + b"".join(ids)
    return head + abi + build_payload(packages, require_signers=require_signers)


def latest_price(feed_id: str, timeout: float = 1.5) -> tuple[float, int]:
    """(price, timestamp_ms) straight off the gateway — the off-chain leading signal the watcher
    polls. Uses the median of the valid packages' values (they can differ by a tick)."""
    pkgs = fetch_feed_packages(feed_id, timeout)
    vals = sorted(float(dp["value"]) for p in pkgs for dp in p["dataPoints"]
                  if dp.get("dataFeedId") == feed_id)
    if not vals:
        raise RedstoneError(f"no values for {feed_id}")
    ts = max(int(p["timestampMilliseconds"]) for p in pkgs)
    return vals[len(vals) // 2], ts
