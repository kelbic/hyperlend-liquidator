// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

import {Test} from "forge-std/Test.sol";

/// Fork tests (STATE.md risk #2): exercise a REAL liquidation path against the deployed
/// HyperLend Pool — with the liquidationProtocolFee and MustNotLeaveDust rule active — instead
/// of the mocked unit tests. No archive node needed: we fork LATEST, build a real position
/// (wrap native -> supply WHYPE -> borrow USDT0), then mock ONLY the AaveOracle's WHYPE price
/// (vm.mockCall) to push HF where each test needs it. Everything else (Pool, LiquidationLogic
/// lib 0x8dc095f2…8104, interest math, aTokens) is the live deployed code.
///
/// Pins the four behaviours the bot's sizing (analysis/aave.py) relies on:
///   1. liquidator RECEIVES collateral net of the protocol fee (10% of the bonus):
///      received/repaid = 1.090 at bonus 1.10 — the empirical anchor from 16 real liquidations;
///   2. debtToCover overshoot is clamped by the Pool (full close, no revert) — the bot's
///      full-close +1% buffer depends on this;
///   3. a partial that leaves debt leftover < $1000 (while collateral is also partial)
///      reverts (MustNotLeaveDust); the same shape leaving > $1000 on both legs succeeds;
///   4. with HF in (0.95, 1) and both legs >= $2000, the close factor caps the pull at 50%
///      of the total debt (per-reserve rules).
///
/// Run:  HYPE_RPC=https://rpc.hyperliquid.xyz/evm forge test --match-path test/ForkLiquidation.t.sol -vv
interface IPoolFull {
    function supply(address asset, uint256 amount, address onBehalfOf, uint16 referralCode) external;
    function borrow(address asset, uint256 amount, uint256 interestRateMode, uint16 referralCode, address onBehalfOf) external;
    function liquidationCall(address collateralAsset, address debtAsset, address user, uint256 debtToCover, bool receiveAToken) external;
    function getUserAccountData(address user) external view returns (uint256, uint256, uint256, uint256, uint256, uint256 healthFactor);
}

interface IDataProvider {
    function getReserveTokensAddresses(address asset) external view returns (address aToken, address stableDebt, address variableDebt);
    function getLiquidationProtocolFee(address asset) external view returns (uint256);
}

interface IOracleGetter {
    function getAssetPrice(address asset) external view returns (uint256);
}

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
    function transfer(address, uint256) external returns (bool);
}

interface IWHYPE {
    function deposit() external payable;
}

contract ForkLiquidationTest is Test {
    address constant POOL = 0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b;
    address constant ORACLE = 0xC9Fb4fbE842d57EAc1dF3e641a281827493A630e;
    address constant DATA_PROVIDER = 0x4f4d4cA1e0a8A21FE0B460613bEbe917f2eb4326;
    address constant WHYPE = 0x5555555555555555555555555555555555555555;
    address constant USDT0 = 0xB8CE59FC3717ada4C02eaDF9682A9e934F625ebb;
    uint256 constant BONUS_BPS = 11000; // WHYPE liquidationBonus (verified on-chain)

    address borrower = makeAddr("borrower");
    address liquidator = makeAddr("liquidator");
    uint256 whypePrice; // real oracle price at fork time (1e8)

    // --- Aave PercentageMath (half-up), for exact expected values -------------------------
    function pctMul(uint256 x, uint256 bps) internal pure returns (uint256) {
        return (x * bps + 5000) / 10000;
    }

    function pctDiv(uint256 x, uint256 bps) internal pure returns (uint256) {
        return (x * 10000 + bps / 2) / bps;
    }

    function setUp() public {
        vm.createSelectFork(vm.envString("HYPE_RPC"));
        whypePrice = IOracleGetter(ORACLE).getAssetPrice(WHYPE);
        assertGt(whypePrice, 0, "no WHYPE price on fork");
        // the on-chain fee the whole model is calibrated to
        assertEq(IDataProvider(DATA_PROVIDER).getLiquidationProtocolFee(WHYPE), 1000, "fee moved");

        // fund the liquidator with USDT0 BEFORE any price mock: wrap native, supply as
        // collateral, borrow USDT0 from the live pool (no deal() into a proxy token)
        _mintUsdt0(liquidator, 30_000e6, 1500 ether);
    }

    /// wrap native -> supply WHYPE -> borrow USDT0 (real pool liquidity, real interest state)
    function _mintUsdt0(address who, uint256 usdtOut, uint256 whypeIn) internal {
        vm.deal(who, whypeIn + 1 ether);
        vm.startPrank(who);
        IWHYPE(WHYPE).deposit{value: whypeIn}();
        IERC20(WHYPE).approve(POOL, type(uint256).max);
        IPoolFull(POOL).supply(WHYPE, whypeIn, who, 0);
        IPoolFull(POOL).borrow(USDT0, usdtOut, 2, 0, who);
        vm.stopPrank();
    }

    /// build the target position (collateral sized in USD off the LIVE price so the borrow
    /// clears the 60% LTV) and mock the WHYPE price so its HF lands on `targetHf` (1e18).
    /// HF scales linearly in the collateral price for a single-collateral position.
    function _makeUnderwater(uint256 usdtDebt, uint256 collUsd, uint256 targetHf) internal {
        uint256 whypeColl = collUsd * 1e8 * 1e18 / whypePrice;
        _mintUsdt0(borrower, usdtDebt, whypeColl);
        (,,,,, uint256 hf) = IPoolFull(POOL).getUserAccountData(borrower);
        assertGt(hf, 1e18, "position born unhealthy");
        uint256 newPrice = whypePrice * targetHf / hf;
        vm.mockCall(ORACLE, abi.encodeWithSelector(IOracleGetter.getAssetPrice.selector, WHYPE),
                    abi.encode(newPrice));
        (,,,,, uint256 hf2) = IPoolFull(POOL).getUserAccountData(borrower);
        assertApproxEqRel(hf2, targetHf, 0.01e18, "price mock missed the target HF");
        whypePrice = newPrice; // expected-value math below uses the mocked price
    }

    function _debtOf(address user) internal view returns (uint256) {
        (,, address vDebt) = IDataProvider(DATA_PROVIDER).getReserveTokensAddresses(USDT0);
        return IERC20(vDebt).balanceOf(user);
    }

    /// 1+2: full close via overshot debtToCover; received = gross - 10% of the bonus portion.
    function test_protocolFeeAndOvershootClampOnRealLiquidation() public {
        _makeUnderwater(2500e6, 5000, 0.90e18); // HF<0.95 -> 100% closable
        uint256 debt = _debtOf(borrower);
        uint256 before = IERC20(WHYPE).balanceOf(liquidator);

        vm.startPrank(liquidator);
        IERC20(USDT0).approve(POOL, type(uint256).max);
        // overshoot 2x: the Pool must clamp to the actual full debt (bot's full-close buffer)
        IPoolFull(POOL).liquidationCall(WHYPE, USDT0, borrower, debt * 2, false);
        vm.stopPrank();

        assertEq(_debtOf(borrower), 0, "overshoot was not clamped to a full close");
        uint256 got = IERC20(WHYPE).balanceOf(liquidator) - before;

        // expected: gross = pctMul(base, 11000); fee = 10% of the bonus part; received = gross-fee
        uint256 usdtPrice = IOracleGetter(ORACLE).getAssetPrice(USDT0);
        uint256 base = usdtPrice * debt * 1e18 / (whypePrice * 1e6);
        uint256 gross = pctMul(base, BONUS_BPS);
        uint256 fee = pctMul(gross - pctDiv(gross, BONUS_BPS), 1000);
        assertApproxEqRel(got, gross - fee, 0.0001e18, "received != fee-adjusted bonus");
        assertLt(got, gross, "protocol fee was not charged");
        // the empirical anchor: received/repaid = 1.090 at bonus 1.10
        uint256 repaidValue = usdtPrice * debt / 1e6;          // USD*1e8
        uint256 gotValue = whypePrice * got / 1e18;            // USD*1e8
        assertApproxEqRel(gotValue * 1e18 / repaidValue, 1.09e18, 0.0005e18, "not 1.090x");
    }

    /// 3: partial-partial leaving <$1000 of debt reverts; leaving >$1000 on both legs passes.
    function test_dustRuleOnRealLiquidation() public {
        _makeUnderwater(2600e6, 5200, 0.90e18); // 100% closable; both legs > $2000
        uint256 debt = _debtOf(borrower);
        vm.startPrank(liquidator);
        IERC20(USDT0).approve(POOL, type(uint256).max);

        // leave ~$500 of debt (collateral also partial) -> MustNotLeaveDust
        vm.expectRevert();
        IPoolFull(POOL).liquidationCall(WHYPE, USDT0, borrower, debt - 500e6, false);

        // same shape leaving ~$1100 of debt (and >$1000 of collateral) -> fine
        IPoolFull(POOL).liquidationCall(WHYPE, USDT0, borrower, debt - 1100e6, false);
        vm.stopPrank();
        assertApproxEqAbs(_debtOf(borrower), 1100e6, 5e6, "leftover debt not ~$1100");
    }

    /// 4: HF in (0.95,1), both legs >= $2000 -> pull capped at 50% of total debt.
    function test_closeFactor50PercentOnRealLiquidation() public {
        _makeUnderwater(6000e6, 12_000, 0.97e18);
        uint256 debt = _debtOf(borrower);
        vm.startPrank(liquidator);
        IERC20(USDT0).approve(POOL, type(uint256).max);
        IPoolFull(POOL).liquidationCall(WHYPE, USDT0, borrower, type(uint256).max, false);
        vm.stopPrank();
        // half the debt must remain (per-reserve close factor, uint.max clamped to 50%)
        assertApproxEqRel(_debtOf(borrower), debt / 2, 0.001e18, "close factor != 50%");
    }
}
