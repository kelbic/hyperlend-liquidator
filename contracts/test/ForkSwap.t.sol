// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

import {Test} from "forge-std/Test.sol";

/// Fork test: proves the REAL LiquidSwap Router sends swap output to msg.sender (the caller),
/// which is the one assumption the mock unit-tests bake in and the build flagged as unverified.
/// If the router sent output elsewhere, our HyperLendLiquidator.executeOperation would hit
/// CannotRepay and revert (no fund loss, but every liquidation silently misses). This closes it.
///
/// Run with a freshly-fetched WHYPE->USDC route:
///   HYPE_RPC=... SWAP_TARGET=0x.. SWAP_CALLDATA=0x.. AMOUNT_IN_WEI=.. \
///   forge test --match-path test/ForkSwap.t.sol -vv
interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
}

interface IWHYPE {
    function deposit() external payable;
}

contract ForkSwapTest is Test {
    address constant WHYPE = 0x5555555555555555555555555555555555555555;
    address constant USDC = 0xb88339CB7199b77E23DB6E890353E22632Ba630f;

    function test_realRouterSendsOutputToMsgSender() public {
        // Manual harness: needs a freshly-fetched route (see header). Without env it SKIPS —
        // a suite that is red by default teaches everyone to ignore red.
        string memory rpcUrl = vm.envOr("HYPE_RPC", string(""));
        vm.skip(bytes(rpcUrl).length == 0);
        vm.createSelectFork(rpcUrl);
        address router = vm.envAddress("SWAP_TARGET");
        bytes memory cd = vm.envBytes("SWAP_CALLDATA");
        uint256 amountIn = vm.envUint("AMOUNT_IN_WEI");
        // LiquidSwap bakes a near-zero-buffer deadline; the forked "latest" block timestamp is
        // real-now which can already be past it. Warp to the route's fetch time so the deadline
        // is in the future (this is a pure recipient test, not a timing test).
        vm.warp(vm.envUint("WARP_TS"));

        // Acquire WHYPE the honest way: wrap native HYPE (0x5555.. is the canonical WHYPE wrapper).
        vm.deal(address(this), amountIn + 1 ether);
        IWHYPE(WHYPE).deposit{value: amountIn}();
        assertGe(IERC20(WHYPE).balanceOf(address(this)), amountIn, "did not obtain WHYPE");

        uint256 usdcBefore = IERC20(USDC).balanceOf(address(this));
        // Exactly what the liquidator contract does: approve the router, then call the baked calldata.
        IERC20(WHYPE).approve(router, type(uint256).max);
        (bool ok,) = router.call(cd);
        assertTrue(ok, "router call reverted on the fork");

        uint256 got = IERC20(USDC).balanceOf(address(this)) - usdcBefore;
        emit log_named_uint("USDC delivered to msg.sender (caller)", got);
        assertGt(got, 0, "router did NOT send output to msg.sender -> executeOperation would revert");
    }
}
