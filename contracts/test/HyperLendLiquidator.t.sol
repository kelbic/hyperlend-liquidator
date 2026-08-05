// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

import {Test, console2} from "forge-std/Test.sol";
import {HyperLendLiquidator, IERC20} from "../src/HyperLendLiquidator.sol";

// --- minimal mocks -------------------------------------------------------------------------

contract MockERC20 {
    string public name;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    constructor(string memory n) {
        name = n;
    }

    function mint(address to, uint256 a) external {
        balanceOf[to] += a;
    }

    function approve(address s, uint256 a) external returns (bool) {
        allowance[msg.sender][s] = a;
        return true;
    }

    function transfer(address to, uint256 a) external returns (bool) {
        balanceOf[msg.sender] -= a;
        balanceOf[to] += a;
        return true;
    }

    function transferFrom(address f, address to, uint256 a) external returns (bool) {
        allowance[f][msg.sender] -= a;
        balanceOf[f] -= a;
        balanceOf[to] += a;
        return true;
    }
}

/// @dev Mock Aave Pool: flashLoanSimple sends the debt, runs the callback, pulls debt+premium.
/// liquidationCall pulls `debtToCover` of the debt token from the caller and sends it seized
/// collateral (= debtToCover value * bonus, expressed 1:1 here for simplicity).
contract MockPool {
    uint256 public constant PREMIUM_BPS = 4; // 0.04% like HyperLend
    MockERC20 public immutable debt;
    MockERC20 public immutable coll;
    uint256 public seizePerDebt; // collateral units seized per 1 debt unit repaid (bonus baked in)

    constructor(MockERC20 _debt, MockERC20 _coll, uint256 _seizePerDebt) {
        debt = _debt;
        coll = _coll;
        seizePerDebt = _seizePerDebt;
    }

    function flashLoanSimple(address receiver, address asset, uint256 amount, bytes calldata params, uint16)
        external
    {
        MockERC20(asset).transfer(receiver, amount); // fund the receiver
        uint256 premium = (amount * PREMIUM_BPS) / 10000;
        (bool ok,) = receiver.call(
            abi.encodeWithSignature(
                "executeOperation(address,uint256,uint256,address,bytes)", asset, amount, premium, receiver, params
            )
        );
        require(ok, "callback failed");
        // pull principal + premium back
        MockERC20(asset).transferFrom(receiver, address(this), amount + premium);
    }

    function liquidationCall(address, address debtAsset, address, uint256 debtToCover, bool) external {
        MockERC20(debtAsset).transferFrom(msg.sender, address(this), debtToCover); // repay
        coll.mint(msg.sender, debtToCover * seizePerDebt); // seize collateral + bonus
    }
}

/// @dev Mock LiquidSwap Router. swapCallData encodes exchange(tokenIn, tokenOut, amtIn, amtOut):
/// pulls amtIn of tokenIn from the caller and sends amtOut of tokenOut back.
contract MockSwap {
    function exchange(address tokenIn, address tokenOut, uint256 amtIn, uint256 amtOut) external {
        MockERC20(tokenIn).transferFrom(msg.sender, address(this), amtIn);
        MockERC20(tokenOut).mint(msg.sender, amtOut);
    }
}

/// @dev Mock RedStone price-feed adapter: counts pushes; `breakNext` makes the next push revert
/// (the "someone else's push landed first this block -> not-newer" case).
contract MockOracleAdapter {
    uint256 public pushes;
    bool public breakNext;

    function setBreakNext(bool b) external {
        breakNext = b;
    }

    function updateDataFeedsValuesPartial(bytes32[] calldata) external {
        if (breakNext) revert("DataTimestampMustBeNewer");
        pushes += 1;
    }
}

// --- tests ---------------------------------------------------------------------------------

contract HyperLendLiquidatorTest is Test {
    MockERC20 debt;
    MockERC20 coll;
    MockPool pool;
    MockSwap swapr;
    HyperLendLiquidator liq;

    address owner = address(this);
    address user = address(0xB0B); // the borrower being liquidated

    function setUp() public {
        debt = new MockERC20("USDT0");
        coll = new MockERC20("WHYPE");
        pool = new MockPool(debt, coll, 11); // 11 collateral units per debt unit (~"10% bonus"-ish)
        swapr = new MockSwap();
        liq = new HyperLendLiquidator(address(pool));
        // seed the pool with debt liquidity to lend, and the swap with nothing (it mints output)
        debt.mint(address(pool), 1_000_000e6);
    }

    /// swapCallData: sell ALL seized collateral for `amountOut` debt tokens.
    function _swapData(uint256 seizedColl, uint256 amountOut) internal view returns (bytes memory) {
        return abi.encodeWithSelector(
            MockSwap.exchange.selector, address(coll), address(debt), seizedColl, amountOut
        );
    }

    function test_flashLiquidationIsProfitableAndSweeps() public {
        uint256 debtToCover = 1_000e6; // repay 1000 USDT0
        uint256 seized = debtToCover * 11; // pool seizes 11,000 WHYPE units
        uint256 swapOut = 1_100e6; // exit yields 1100 USDT0 (10% gross bonus realized)

        uint256 ownerBefore = debt.balanceOf(owner);
        uint256 profit = liq.liquidate(
            address(coll), address(debt), user, debtToCover, true, address(swapr), _swapData(seized, swapOut), 50e6
        );

        // owed = 1000 + 0.04% = 1000.4; profit = 1100 - 1000.4 = 99.6 USDT0
        assertEq(profit, swapOut - debtToCover - (debtToCover * 4) / 10000, "profit math");
        assertEq(debt.balanceOf(owner) - ownerBefore, profit, "profit swept to owner");
        assertEq(debt.balanceOf(address(liq)), 0, "contract holds no debt asset after sweep");
        assertEq(coll.balanceOf(address(liq)), 0, "no leftover collateral (all swapped)");
    }

    function test_minProfitGateReverts() public {
        uint256 debtToCover = 1_000e6;
        uint256 seized = debtToCover * 11;
        uint256 swapOut = 1_100e6; // ~99.6 profit
        vm.expectRevert(); // ProfitTooLow: floor 500 > 99.6
        liq.liquidate(
            address(coll), address(debt), user, debtToCover, true, address(swapr), _swapData(seized, swapOut), 500e6
        );
    }

    function test_badExitCannotRepayReverts() public {
        uint256 debtToCover = 1_000e6;
        uint256 seized = debtToCover * 11;
        uint256 swapOut = 500e6; // swap yields too little to repay flash -> CannotRepay
        vm.expectRevert();
        liq.liquidate(
            address(coll), address(debt), user, debtToCover, true, address(swapr), _swapData(seized, swapOut), 1
        );
    }

    function test_onlyOwnerCanLiquidate() public {
        vm.prank(address(0xdead));
        vm.expectRevert(HyperLendLiquidator.NotOwner.selector);
        liq.liquidate(address(coll), address(debt), user, 1e6, true, address(swapr), _swapData(1, 1), 0);
    }

    function test_onlyPoolCanCallback() public {
        vm.prank(address(0xdead));
        vm.expectRevert(HyperLendLiquidator.NotPool.selector);
        liq.executeOperation(address(debt), 1e6, 0, address(liq), "");
    }

    function test_capitalPathNoFlashloan() public {
        uint256 debtToCover = 1_000e6;
        uint256 seized = debtToCover * 11;
        uint256 swapOut = 1_100e6;
        // pre-fund the contract with the debt asset (operator-supplied capital)
        debt.mint(address(liq), debtToCover);
        uint256 profit = liq.liquidate(
            address(coll), address(debt), user, debtToCover, false, address(swapr), _swapData(seized, swapOut), 50e6
        );
        // capital path pays no premium: profit = 1100 - 1000 = 100 USDT0
        assertEq(profit, swapOut - debtToCover, "capital-path profit");
    }

    function test_sweepOnlyOwner() public {
        coll.mint(address(liq), 123);
        vm.prank(address(0xdead));
        vm.expectRevert(HyperLendLiquidator.NotOwner.selector);
        liq.sweep(address(coll));
        liq.sweep(address(coll)); // owner ok
        assertEq(coll.balanceOf(owner), 123);
    }

    // --- v2: coll == debt (04.08 — 55 events / ~$44k bonus were structurally unreachable) ----

    /// The seized collateral IS the repayment asset: no swap exists or is needed. swapTarget = 0.
    function test_sameAssetFlashPathSkipsSwap() public {
        MockPool p2 = new MockPool(debt, debt, 11); // seizes the DEBT token itself
        HyperLendLiquidator l2 = new HyperLendLiquidator(address(p2));
        debt.mint(address(p2), 1_000_000e6);

        uint256 debtToCover = 1_000e6;
        uint256 ownerBefore = debt.balanceOf(owner);
        uint256 profit =
            l2.liquidate(address(debt), address(debt), user, debtToCover, true, address(0), "", 50e6);

        // seized = 11x, repaid 1x, premium 0.04%: profit = 10x - premium, no router involved
        uint256 expected = debtToCover * 10 - (debtToCover * 4) / 10000;
        assertEq(profit, expected, "same-asset profit");
        assertEq(debt.balanceOf(owner) - ownerBefore, profit, "swept to owner");
        assertEq(debt.balanceOf(address(l2)), 0, "nothing left on the contract");
    }

    function test_sameAssetCapitalPath() public {
        MockPool p2 = new MockPool(debt, debt, 11);
        HyperLendLiquidator l2 = new HyperLendLiquidator(address(p2));
        uint256 debtToCover = 1_000e6;
        debt.mint(address(l2), debtToCover); // operator pre-funded, no premium
        uint256 profit =
            l2.liquidate(address(debt), address(debt), user, debtToCover, false, address(0), "", 50e6);
        assertEq(profit, debtToCover * 10, "capital-path same-asset profit");
    }

    // --- v2: atomic oracle push (04.08 — the field leader's technique, permissionless) --------

    function _pushData() internal pure returns (bytes memory) {
        bytes32[] memory ids = new bytes32[](1);
        ids[0] = bytes32("HYPE");
        return abi.encodeWithSelector(MockOracleAdapter.updateDataFeedsValuesPartial.selector, ids);
    }

    function test_pushRunsBeforeLiquidation() public {
        MockOracleAdapter oracle = new MockOracleAdapter();
        uint256 debtToCover = 1_000e6;
        uint256 seized = debtToCover * 11;
        uint256 swapOut = 1_100e6;
        uint256 profit = liq.liquidateWithPush(
            address(oracle), _pushData(),
            address(coll), address(debt), user, debtToCover, true, address(swapr), _swapData(seized, swapOut), 50e6
        );
        assertEq(oracle.pushes(), 1, "oracle push must run");
        assertEq(profit, swapOut - debtToCover - (debtToCover * 4) / 10000, "profit unchanged by push");
    }

    /// A rejected push (someone else's landed first this block) must NOT block the liquidation:
    /// the price is already fresh, the shot still fires.
    function test_failedPushDoesNotBlockLiquidation() public {
        MockOracleAdapter oracle = new MockOracleAdapter();
        oracle.setBreakNext(true);
        uint256 debtToCover = 1_000e6;
        uint256 profit = liq.liquidateWithPush(
            address(oracle), _pushData(),
            address(coll), address(debt), user, debtToCover, true, address(swapr),
            _swapData(debtToCover * 11, 1_100e6), 50e6
        );
        assertEq(oracle.pushes(), 0, "push was rejected");
        assertGt(profit, 0, "liquidation must still succeed");
    }

    function test_zeroOracleTargetSkipsPush() public {
        uint256 debtToCover = 1_000e6;
        uint256 profit = liq.liquidateWithPush(
            address(0), "",
            address(coll), address(debt), user, debtToCover, true, address(swapr),
            _swapData(debtToCover * 11, 1_100e6), 50e6
        );
        assertGt(profit, 0);
    }

    function test_liquidateWithPushOnlyOwner() public {
        vm.prank(address(0xdead));
        vm.expectRevert(HyperLendLiquidator.NotOwner.selector);
        liq.liquidateWithPush(
            address(0), "", address(coll), address(debt), user, 1e6, true, address(swapr), _swapData(1, 1), 0
        );
    }
}

/// @notice Две ноги выхода: залог -> промежуточный актив -> долг.
///
/// Зачем эта ветка вообще есть. Самая дорогая позиция книги ($7.7M долга) держит залог целиком
/// в PT-kHYPE, а прямой маршрут PT->WHYPE у Pendle тянет вложенный своп агрегатора и не влезает
/// в 3M газа малого блока HyperEVM (форк-экзамен 05.08: out of gas во вложенном вызове даже при
/// 2.9M, и это не нехватка залога — проверено запасом haircut 6%). Лёгкая ветка PT->kHYPE в
/// бюджет влезает, но даёт не тот актив. Отсюда второй хоп.
contract TwoLegTest is Test {
    MockERC20 debt;
    MockERC20 coll;
    MockERC20 mid;
    MockPool pool;
    MockSwap swapr;
    HyperLendLiquidator liq;

    address user = address(0xB0B);

    function setUp() public {
        debt = new MockERC20("WHYPE");
        coll = new MockERC20("PT-kHYPE");
        mid = new MockERC20("kHYPE");
        pool = new MockPool(debt, coll, 11);
        swapr = new MockSwap();
        liq = new HyperLendLiquidator(address(pool));
        debt.mint(address(pool), 1_000_000e18);
    }

    function _leg(MockERC20 tin, MockERC20 tout, uint256 amtIn, uint256 amtOut)
        internal
        view
        returns (bytes memory)
    {
        return abi.encodeWithSelector(
            MockSwap.exchange.selector, address(tin), address(tout), amtIn, amtOut
        );
    }

    function test_twoLegSwapReachesDebtAssetAndSweeps() public {
        uint256 cover = 1_000e18;
        uint256 seized = cover * 11;                   // мок отдаёт seizePerDebt=11 за единицу долга
        uint256 midOut = 1_090e18;                     // PT -> kHYPE
        uint256 debtOut = 1_085e18;                    // kHYPE -> WHYPE (покрывает cover+премию)

        uint256 before = debt.balanceOf(address(this));
        uint256 profit = liq.liquidateTwoLeg(
            address(coll), address(debt), user, cover, true,
            address(swapr), _leg(coll, mid, seized, midOut),
            address(mid), address(swapr), _leg(mid, debt, midOut, debtOut),
            1
        );
        assertGt(profit, 0, "two legs must land proceeds in the debt asset");
        assertEq(debt.balanceOf(address(this)) - before, profit, "proceeds swept to owner");
        assertEq(coll.balanceOf(address(liq)), 0, "no collateral stuck");
        assertEq(mid.balanceOf(address(liq)), 0, "no mid asset stuck");
    }

    function test_secondLegFailureRevertsWholeTx() public {
        uint256 cover = 1_000e18;
        uint256 seized = cover * 11;
        // вторая нога просит больше, чем пришло с первой -> transferFrom не пройдёт
        vm.expectRevert();
        liq.liquidateTwoLeg(
            address(coll), address(debt), user, cover, true,
            address(swapr), _leg(coll, mid, seized, 1_090e18),
            address(mid), address(swapr), _leg(mid, debt, 9_999e18, 1_085e18),
            1
        );
    }

    function test_emptySecondLegBehavesExactlyLikeSingleSwap() public {
        uint256 cover = 1_000e18;
        uint256 seized = cover * 11;
        uint256 profit = liq.liquidateTwoLeg(
            address(coll), address(debt), user, cover, true,
            address(swapr), _leg(coll, debt, seized, 1_085e18),
            address(0), address(0), "",
            1
        );
        assertGt(profit, 0, "empty second leg == legacy single-swap path");
    }

    function test_minProfitStillGuardsTheTwoLegPath() public {
        uint256 cover = 1_000e18;
        uint256 seized = cover * 11;
        // выход в убыток: гейт обязан сработать так же, как на одной ноге
        vm.expectRevert();
        liq.liquidateTwoLeg(
            address(coll), address(debt), user, cover, true,
            address(swapr), _leg(coll, mid, seized, 1_090e18),
            address(mid), address(swapr), _leg(mid, debt, 1_090e18, 1_085e18),
            100e18
        );
    }

    function test_twoLegOnlyOwner() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert(HyperLendLiquidator.NotOwner.selector);
        liq.liquidateTwoLeg(
            address(coll), address(debt), user, 1e18, true,
            address(swapr), "", address(mid), address(swapr), "", 0
        );
    }
}
