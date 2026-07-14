// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

import {Script, console2} from "forge-std/Script.sol";
import {HyperLendLiquidator} from "../src/HyperLendLiquidator.sol";

/// @notice Deploys HyperLendLiquidator to HyperEVM (chainId 999). This is the ONE live on-chain
/// step the operator runs (the bot itself never deploys). Pool = HyperLend core Pool (verified).
///   HL_KEYFILE=~/.hyperlend-bot/key forge script script/Deploy.s.sol --rpc-url $HL_RPC --broadcast
contract Deploy is Script {
    address constant POOL = 0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b; // HyperLend Pool (verified)

    function run() external {
        uint256 pk = vm.envUint("HL_DEPLOYER_PK");
        vm.startBroadcast(pk);
        HyperLendLiquidator liq = new HyperLendLiquidator(POOL);
        vm.stopBroadcast();
        console2.log("HyperLendLiquidator deployed at:", address(liq));
        console2.log("owner:", liq.owner());
        console2.log("POOL:", liq.POOL());
    }
}
