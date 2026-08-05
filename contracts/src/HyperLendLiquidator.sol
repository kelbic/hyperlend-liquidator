// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

/// @notice Minimal Aave-v3 Pool surface used by the liquidator (HyperLend is an Aave-v3.6 fork).
interface IPool {
    /// @dev Simple single-asset flash loan. The Pool sends `amount` of `asset` to `receiverAddress`,
    /// calls receiver.executeOperation(...), then pulls `amount + premium` back via transferFrom.
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;

    /// @dev Liquidate `user`: repay up to `debtToCover` of `debtAsset`, seize `collateralAsset`
    /// (plus the liquidation bonus). receiveAToken=false => the underlying collateral is sent here.
    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;
}

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
}

/// @title HyperLendLiquidator — zero-capital Aave-v3 liquidations on HyperEVM (chainId 999).
/// @notice The Morpho analogue of our wc/katana bots, ported to Aave-v3 mechanics. Flow (flash path):
///   1. liquidate() flash-loans `debtToCover` of the debt asset from HyperLend's own Pool via
///      flashLoanSimple (premium 0.04%, verified on-chain).
///   2. executeOperation callback: approve+call Pool.liquidationCall(collateral, debt, user,
///      debtToCover, receiveAToken=false) — the Pool pulls the debt repayment and sends the seized
///      collateral (+ liquidation bonus) to this contract.
///   3. Swap the seized collateral -> debt asset via the LiquidSwap Router (generic swapTarget /
///      swapCallData built off-chain by the bot from api.liqd.ag — same idiom as the katana Sushi
///      integration).
///   4. Approve the Pool to pull `amount + premium`; whatever debt asset is left is the profit,
///      swept to the owner. No standing capital: the seized collateral funds the repayment.
///
/// Capital fallback (useFlashloan=false): if the Pool's flash loan is ever paused/unavailable, the
/// operator can pre-fund this contract with the debt asset and liquidate directly (no premium). The
/// same balance-based profit accounting applies. Zero-capital (flash) is the default.
///
/// Safety (layered): onlyOwner entry (swap calldata is always our own), onlyPool callback with
/// initiator==this, nonReentrant, minProfit gate (reverts a losing liquidation even on a stale
/// quote), can-repay check, return-data-checked ERC20 ops, force-approve with allowance reset, and
/// market params passed as arguments (never hardcoded). The contract holds no standing funds
/// (not a honeypot); the hot wallet holds only gas.
contract HyperLendLiquidator {
    address public immutable POOL;
    address public owner;
    uint256 private _locked = 1; // 1 = unlocked, 2 = locked (nonzero init saves gas)

    /// @dev Context handed to the flash-loan callback via `params`.
    /// Вторая нога (midAsset/swapTarget2/swapCallData2) появилась 05.08 и по умолчанию пуста —
    /// путь с одним свопом байт-в-байт прежний. Зачем она: у самой дорогой позиции книги
    /// ($7.7M долга) залог целиком в PT-kHYPE, и прямой маршрут PT->WHYPE у Pendle тянет за
    /// собой вложенный своп агрегатора, который не влезает в 3M газа малого блока HyperEVM
    /// (форк-экзамен: out of gas во вложенном вызове даже при 2.9M, при этом ни залога, ни
    /// лимита не хватало — проверено запасом haircut 6%). Лёгкая ветка PT->kHYPE в бюджет
    /// влезает, но даёт не тот актив: долг номинирован в WHYPE. Отсюда две ноги.
    /// @dev Ноги выхода одним значением: иначе _liquidate уходит в stack-too-deep, а дробить
    /// её ради компилятора значило бы размазать один смысловой шаг по нескольким функциям.
    struct Legs {
        address swapTarget;
        bytes swapCallData;
        address midAsset;        // что окажется на руках после первой ноги (0 = одна нога)
        address swapTarget2;
        bytes swapCallData2;
    }

    struct FlashCtx {
        address collateralAsset;
        address user;
        Legs legs;
    }

    error NotOwner();
    error NotPool();
    error BadInitiator();
    error Reentrant();
    error NothingSeized();
    error SwapFailed();
    error CannotRepay(uint256 have, uint256 need);
    error ProfitTooLow(uint256 got, uint256 min);
    error ERC20OpFailed();

    event Liquidated(
        address indexed user, address indexed collateralAsset, address indexed debtAsset, uint256 profit
    );
    event OwnerChanged(address indexed from, address indexed to);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier nonReentrant() {
        if (_locked == 2) revert Reentrant();
        _locked = 2;
        _;
        _locked = 1;
    }

    constructor(address pool) {
        POOL = pool;
        owner = msg.sender;
    }

    function setOwner(address newOwner) external onlyOwner {
        emit OwnerChanged(owner, newOwner);
        owner = newOwner;
    }

    // --------------------------------------------------------------------- liquidation entry

    /// @notice Liquidate `user` on the (collateralAsset, debtAsset) pair, repaying `debtToCover`
    /// of debt, swapping the seized collateral to the debt asset via `swapTarget`/`swapCallData`.
    /// Reverts unless realized profit (swept to owner) >= `minProfit` (denominated in the debt
    /// asset). onlyOwner so the swap calldata is always our own.
    /// @param useFlashloan true = flash-loan the debt from the Pool (zero-capital, default);
    ///        false = spend this contract's own debt-asset balance (operator pre-funded).
    function liquidate(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool useFlashloan,
        address swapTarget,
        bytes calldata swapCallData,
        uint256 minProfit
    ) external onlyOwner nonReentrant returns (uint256 profit) {
        profit = _liquidate(
            collateralAsset, debtAsset, user, debtToCover, useFlashloan,
            Legs(swapTarget, swapCallData, address(0), address(0), ""), minProfit
        );
    }

    /// @notice liquidate() с ДВУМЯ последовательными свопами: залог -> midAsset -> долг.
    /// Нужна там, где прямого маршрута залог->долг нет или он не влезает в газ малого блока
    /// (случай PT-kHYPE, см. комментарий к FlashCtx). Вторая нога включается только при
    /// swapTarget2 != 0; экономику по-прежнему стерегут CannotRepay и ProfitTooLow, то есть
    /// лишний хоп не может превратить убыток в «успех».
    function liquidateTwoLeg(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool useFlashloan,
        address swapTarget,
        bytes calldata swapCallData,
        address midAsset,
        address swapTarget2,
        bytes calldata swapCallData2,
        uint256 minProfit
    ) external onlyOwner nonReentrant returns (uint256 profit) {
        profit = _liquidate(
            collateralAsset, debtAsset, user, debtToCover, useFlashloan,
            Legs(swapTarget, swapCallData, midAsset, swapTarget2, swapCallData2), minProfit
        );
    }

    /// @notice Same as liquidate(), preceded by an atomic oracle push: `oracleTarget` is called
    /// with `oracleCalldata` (a fresh signed RedStone payload -> the price feed adapter) BEFORE the
    /// flash loan, so the liquidation runs against the price that makes the position liquidatable
    /// in the SAME transaction — the technique the 04.08 race analysis proved the field leader
    /// uses (push is permissionless; waiting for the relayer's confirmed push loses by one block
    /// structurally).
    /// @dev The push TOLERATES failure on purpose: if someone else's push landed earlier in this
    /// block, the adapter rejects ours as not-newer — but the price is already fresh, so the
    /// liquidation below must still run. A broken payload therefore degrades to the reactive path
    /// instead of blocking it; the off-chain bot detects a rejected push by the absence of our
    /// ValueUpdate in the receipt logs. The real safety gates are unchanged: liquidationCall's own
    /// HF check, CannotRepay and ProfitTooLow.
    function liquidateWithPush(
        address oracleTarget,
        bytes calldata oracleCalldata,
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool useFlashloan,
        address swapTarget,
        bytes calldata swapCallData,
        uint256 minProfit
    ) external onlyOwner nonReentrant returns (uint256 profit) {
        if (oracleTarget != address(0)) {
            (bool pushed,) = oracleTarget.call(oracleCalldata);
            pushed; // deliberate: see @dev — a rejected push must not block the liquidation
        }
        profit = _liquidate(
            collateralAsset, debtAsset, user, debtToCover, useFlashloan,
            Legs(swapTarget, swapCallData, address(0), address(0), ""), minProfit
        );
    }

    function _liquidate(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool useFlashloan,
        Legs memory legs,
        uint256 minProfit
    ) internal returns (uint256 profit) {
        uint256 balBefore = IERC20(debtAsset).balanceOf(address(this));

        if (useFlashloan) {
            bytes memory params =
                abi.encode(FlashCtx({collateralAsset: collateralAsset, user: user, legs: legs}));
            // Pool sends debtToCover here, calls executeOperation, then pulls debtToCover+premium.
            IPool(POOL).flashLoanSimple(address(this), debtAsset, debtToCover, params, 0);
        } else {
            _liquidateAndSwap(collateralAsset, debtAsset, user, debtToCover, legs);
        }

        uint256 balAfter = IERC20(debtAsset).balanceOf(address(this));
        profit = balAfter - balBefore;
        if (profit < minProfit) revert ProfitTooLow(profit, minProfit);
        _safeTransfer(debtAsset, owner, balAfter); // sweep everything (incl. any prior dust)
        emit Liquidated(user, collateralAsset, debtAsset, profit);
    }

    /// @notice Aave flash-loan callback. The Pool has already sent `amount` of `asset` (the debt
    /// asset) here. We run the liquidation + swap, then approve the Pool to pull `amount + premium`.
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        if (msg.sender != POOL) revert NotPool();
        if (initiator != address(this)) revert BadInitiator();

        FlashCtx memory c = abi.decode(params, (FlashCtx));
        _liquidateAndSwap(c.collateralAsset, asset, c.user, amount, c.legs);

        uint256 owed = amount + premium;
        uint256 have = IERC20(asset).balanceOf(address(this));
        if (have < owed) revert CannotRepay(have, owed); // insufficient exit -> revert whole tx
        _forceApprove(asset, POOL, owed);                // Pool pulls exactly this next
        return true;
    }

    // --------------------------------------------------------------------- core

    /// @dev Repay `debtToCover` of `debtAsset` against `user`, receive the seized collateral, and
    /// swap ALL of it to the debt asset via the LiquidSwap Router calldata built off-chain.
    function _liquidateAndSwap(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        Legs memory legs
    ) internal {
        // approve the Pool to pull the debt repayment (it pulls the ACTUAL amount, <= debtToCover)
        _forceApprove(debtAsset, POOL, debtToCover);
        IPool(POOL).liquidationCall(collateralAsset, debtAsset, user, debtToCover, false);
        _forceApprove(debtAsset, POOL, 0); // drop any dangling allowance

        // coll == debt (04.08: 55 events / ~$44k bonus structurally unreachable before this): the
        // seized collateral IS the repayment asset — there is no route to swap into itself, and
        // the unconditional swap made every such position revert as "no route". Skip the swap; the
        // CannotRepay / ProfitTooLow gates still guarantee the economics. NothingSeized is not
        // checkable here for the flash path (the balance already holds the flash-loaned debt), but
        // a liquidation that seized nothing fails those same gates anyway.
        if (collateralAsset == debtAsset) return;

        uint256 collBal = IERC20(collateralAsset).balanceOf(address(this));
        if (collBal == 0) revert NothingSeized();

        // swap seized collateral -> debt asset. The Router pulls collateral via transferFrom; the
        // bot bakes an amountIn <= collBal (drift haircut) so this never over-pulls.
        _forceApprove(collateralAsset, legs.swapTarget, collBal);
        (bool ok,) = legs.swapTarget.call(legs.swapCallData);
        if (!ok) revert SwapFailed();
        _forceApprove(collateralAsset, legs.swapTarget, 0); // drop dangling allowance

        // ВТОРАЯ НОГА: midAsset -> debtAsset. Включается только явным swapTarget2, поэтому
        // путь с одним свопом остаётся прежним. Баланс читается ПОСЛЕ первой ноги — сколько
        // реально пришло, столько и продаём; нулевой остаток означает, что первая нога не
        // отдала актив, и это отказ, а не повод молча пропустить хоп.
        if (legs.swapTarget2 != address(0)) {
            uint256 midBal = IERC20(legs.midAsset).balanceOf(address(this));
            if (midBal == 0) revert NothingSeized();
            _forceApprove(legs.midAsset, legs.swapTarget2, midBal);
            (bool ok2,) = legs.swapTarget2.call(legs.swapCallData2);
            if (!ok2) revert SwapFailed();
            _forceApprove(legs.midAsset, legs.swapTarget2, 0);
        }
    }

    // --------------------------------------------------------------------- recovery

    /// @notice Recover stuck tokens (dust collateral from a partial swap, airdrops) to owner.
    function sweep(address token) external onlyOwner {
        _safeTransfer(token, owner, IERC20(token).balanceOf(address(this)));
    }

    /// @notice Recover native HYPE (should never accumulate, but safe to have).
    function sweepNative() external onlyOwner {
        (bool ok,) = owner.call{value: address(this).balance}("");
        if (!ok) revert ERC20OpFailed();
    }

    receive() external payable {}

    // --- return-data-checked ERC20 helpers (handle non-standard tokens like USDT) ---

    function _forceApprove(address token, address spender, uint256 amount) internal {
        _call(token, abi.encodeWithSelector(IERC20.approve.selector, spender, 0));
        if (amount != 0) {
            _call(token, abi.encodeWithSelector(IERC20.approve.selector, spender, amount));
        }
    }

    function _safeTransfer(address token, address to, uint256 amount) internal {
        if (amount == 0) return;
        _call(token, abi.encodeWithSelector(IERC20.transfer.selector, to, amount));
    }

    function _call(address token, bytes memory payload) private {
        (bool ok, bytes memory ret) = token.call(payload);
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert ERC20OpFailed();
    }
}
