// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

import {Test} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";

/// Fork test: is the RedStone push adapter PERMISSIONLESS for us?
///
/// 04.08 we concluded "нас не пускают" from a fork run that produced a silent no-op. The review
/// challenged it: the canary died on `UpdateSkipDueToBlockTimestamp`, which fires AFTER signature
/// verification — a STALE packet, not a rejected sender. This test settles it by state delta.
///
/// Acceptance is the stored feed value/timestamp CHANGING, never the call's return code: the
/// adapter swallows a rejected feed into an event and returns success (that is exactly how the
/// first conclusion went wrong).
///
/// The sender is a fresh EOA with no history — if the adapter gated on the caller, this is the
/// call that must fail.
///
/// Run (calldata comes from a live gateway fetch, see analysis/selfpush_probe.py):
///   HYPE_RPC=... PUSH_ADAPTER=0x.. PUSH_CALLDATA=0x.. PUSH_FEED=HYPE \
///   forge test --match-path test/ForkSelfPush.t.sol -vv
interface IMultiFeedAdapter {
    /// (lastDataTimestamp, lastBlockTimestamp, lastValue) for one feed
    function getLastUpdateDetails(bytes32 dataFeedId)
        external view returns (uint256, uint256, uint256);
    function getValueForDataFeed(bytes32 dataFeedId) external view returns (uint256);
    function getUniqueSignersThreshold() external view returns (uint8);
    function getAuthorisedSignerIndex(address signer) external view returns (uint8);
}

contract ForkSelfPushTest is Test {
    function test_pushFromUnknownEoaLandsOnChain() public {
        string memory rpcUrl = vm.envOr("HYPE_RPC", string(""));
        bytes memory cd = vm.envOr("PUSH_CALLDATA", bytes(""));
        // Manual harness: needs a FRESH signed packet (packets expire in ~3 min). Without env it
        // SKIPS — a suite that is red by default teaches everyone to ignore red.
        vm.skip(bytes(rpcUrl).length == 0 || cd.length == 0);
        vm.createSelectFork(rpcUrl);

        address adapter = vm.envAddress("PUSH_ADAPTER");
        bytes32 feed = bytes32(bytes(vm.envOr("PUSH_FEED", string("HYPE"))));
        IMultiFeedAdapter a = IMultiFeedAdapter(adapter);

        // --- positive control on the test's OWN inputs -------------------------------------
        // A fabricated address answers eth_call with zeros and would read as "nothing changed".
        // Before trusting any delta, prove we are talking to a live adapter holding this feed.
        (uint256 dtBefore, uint256 btBefore, uint256 vBefore) = a.getLastUpdateDetails(feed);
        assertGt(vBefore, 0, "control: adapter holds no value for this feed - wrong address?");
        assertGt(dtBefore, 0, "control: no stored data timestamp - wrong feed id?");
        emit log_named_uint("stored value BEFORE", vBefore);
        emit log_named_uint("stored dataTimestamp BEFORE (ms)", dtBefore);
        emit log_named_uint("threshold (unique signers)", a.getUniqueSignersThreshold());

        // The packet must be NEWER than what is stored, else the adapter legitimately skips it
        // and the run says nothing about permissions. Assert the premise instead of guessing.
        uint256 pktTs = vm.envUint("PUSH_PKG_TS_MS");
        assertGt(pktTs, dtBefore, "packet older than stored data - refetch, this proves nothing");
        // ...and inside the adapter's freshness window relative to the block we execute in.
        assertLe(pktTs / 1000, block.timestamp + 15, "packet too far ahead of fork block");
        assertGe(pktTs / 1000 + 180, block.timestamp, "packet older than 3 min at fork block");

        // --- the actual question: an EOA nobody has ever seen pushes the price ---------------
        address nobody = makeAddr("nobody-eoa");
        vm.deal(nobody, 1 ether);
        vm.recordLogs();
        vm.prank(nobody, nobody); // tx.origin == msg.sender: a plain EOA send, no contract
        (bool ok,) = adapter.call(cd);
        emit log_named_string("raw call returned", ok ? "success" : "REVERT");

        (uint256 dtAfter, uint256 btAfter, uint256 vAfter) = a.getLastUpdateDetails(feed);
        emit log_named_uint("stored value AFTER", vAfter);
        emit log_named_uint("stored dataTimestamp AFTER (ms)", dtAfter);
        // Any emitted event is the adapter telling us WHY, if it refused (UpdateSkipDueTo*).
        Vm.Log[] memory evs = vm.getRecordedLogs();
        emit log_named_uint("events emitted", evs.length);
        for (uint256 i = 0; i < evs.length; i++) {
            emit log_named_address("  event from", evs[i].emitter);
            emit log_named_bytes32("  topic0", evs[i].topics[0]);
            emit log_named_bytes("  data", evs[i].data);
        }

        assertTrue(ok, "adapter reverted for an unknown EOA - permissioned after all");
        // STATE DELTA is the verdict. A success code with an unchanged feed is the silent no-op
        // that produced the wrong 04.08 conclusion.
        assertEq(dtAfter, pktTs, "data timestamp did not advance to our packet: push was skipped");
        assertGt(btAfter, btBefore, "block timestamp of last update did not advance");
        assertTrue(vAfter > 0, "value cleared");
    }

    /// The decisive experiment, and the one that needs no fresh packet: take a push the adapter
    /// PROVABLY accepted on mainnet, replay it one block earlier — once from the relayer that
    /// sent it (control: the harness can land a push at all) and once from a stranger EOA
    /// (experiment: the only changed variable is msg.sender/tx.origin).
    ///
    /// control lands + stranger lands  -> permissionless, the 04.08 verdict is wrong
    /// control lands + stranger skips  -> genuinely gated on the sender
    /// control skips                   -> the HARNESS is lying; nothing may be concluded
    function test_replayAcceptedPushFromStranger() public {
        string memory rpcUrl = vm.envOr("HYPE_RPC", string(""));
        bytes memory cd = vm.envOr("REPLAY_CALLDATA", bytes(""));
        vm.skip(bytes(rpcUrl).length == 0 || cd.length == 0);
        vm.createSelectFork(rpcUrl, vm.envUint("REPLAY_BLOCK") - 1);

        address adapter = vm.envAddress("PUSH_ADAPTER");
        address relayer = vm.envAddress("REPLAY_SENDER");
        bytes32 feed = bytes32(bytes(vm.envOr("PUSH_FEED", string("HYPE"))));
        uint256 pktTs = vm.envUint("PUSH_PKG_TS_MS");
        IMultiFeedAdapter a = IMultiFeedAdapter(adapter);

        (uint256 dt0,, uint256 v0) = a.getLastUpdateDetails(feed);
        assertGt(v0, 0, "control: adapter holds no value - wrong address/feed");
        assertLt(dt0, pktTs, "control: replayed packet is not newer than the pre-state");
        emit log_named_uint("pre-state dataTimestamp (ms)", dt0);

        // --- CONTROL: the original sender, at the block where this very payload was accepted ---
        uint256 snap = vm.snapshotState();
        vm.prank(relayer, relayer);
        (bool okR,) = adapter.call(cd);
        (uint256 dtR,,) = a.getLastUpdateDetails(feed);
        emit log_named_string("control (original relayer) landed", dtR == pktTs ? "YES" : "NO");
        assertTrue(okR && dtR == pktTs,
            "CONTROL FAILED: even the original relayer's own push does not land on this fork - "
            "the harness is lying, no conclusion about permissions may be drawn");

        // --- EXPERIMENT: identical bytes, identical block, a sender nobody has ever seen -------
        vm.revertToState(snap);
        address stranger = makeAddr("stranger-eoa");
        vm.deal(stranger, 1 ether);
        vm.prank(stranger, stranger);
        (bool okS,) = adapter.call(cd);
        (uint256 dtS,, uint256 vS) = a.getLastUpdateDetails(feed);
        emit log_named_string("experiment (stranger EOA) landed", dtS == pktTs ? "YES" : "NO");
        emit log_named_uint("value written by stranger", vS);

        assertTrue(okS, "adapter reverted for the stranger");
        assertEq(dtS, pktTs, "stranger's identical push was SKIPPED -> the adapter gates on sender");
    }

    /// Same replay, swept over a list of senders (SENDERS=0x..,0x..). Answers WHO the adapter
    /// lets in: the relayer, the field leader's EOA, his executor contract, a stranger. Reported
    /// as a table rather than an assertion — this one is a measurement, not a pass/fail gate.
    function test_whoCanPush() public {
        string memory rpcUrl = vm.envOr("HYPE_RPC", string(""));
        bytes memory cd = vm.envOr("REPLAY_CALLDATA", bytes(""));
        vm.skip(bytes(rpcUrl).length == 0 || cd.length == 0);
        vm.createSelectFork(rpcUrl, vm.envUint("REPLAY_BLOCK") - 1);

        address adapter = vm.envAddress("PUSH_ADAPTER");
        bytes32 feed = bytes32(bytes(vm.envOr("PUSH_FEED", string("HYPE"))));
        uint256 pktTs = vm.envUint("PUSH_PKG_TS_MS");
        address[] memory senders = vm.envAddress("SENDERS", ",");

        for (uint256 i = 0; i < senders.length; i++) {
            uint256 snap = vm.snapshotState();
            vm.deal(senders[i], 1 ether);
            vm.prank(senders[i], senders[i]);
            (bool ok,) = adapter.call(cd);
            (uint256 dt,,) = IMultiFeedAdapter(adapter).getLastUpdateDetails(feed);
            emit log_named_address("sender", senders[i]);
            emit log_named_string("  landed", (ok && dt == pktTs) ? "YES" : "no (skipped)");
            vm.revertToState(snap);
        }
    }
}
