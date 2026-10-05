// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {QuboMarket} from "../src/QuboMarket.sol";

contract QuboMarketTest is Test {
    QuboMarket m;
    address client = makeAddr("client");
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");
    bytes32 constant SALT_A = keccak256("a");
    bytes32 constant SALT_B = keccak256("b");

    uint256 internal cur; // explicit block cursor: under via_ir, repeated `block.number` reads get cached

    function _advance(uint256 d) internal {
        cur += d;
        vm.roll(cur);
    }

    function setUp() public {
        cur = block.number;
        m = new QuboMarket();
        vm.deal(client, 100 ether);
        vm.deal(alice, 1 ether);
        vm.deal(bob, 1 ether);
    }

    // ───────────────────────────────────────────── helpers
    function _entry(uint8 i, uint8 j, int32 w) internal pure returns (bytes memory) {
        return abi.encodePacked(i, j, w);
    }

    /// 3-variable toy: E = -5x0 -3x1 -4x2 + 4x0x1 + 6x0x2 + 2x1x2
    /// Optimum: x = {x1,x2} = 6 → -3 -4 +2 = -5 ; x = {x0,x1} → -5-3+4 = -4 ; x0 alone → -5 (tie, worse index)
    function _toy() internal pure returns (bytes memory q) {
        q = bytes.concat(
            _entry(0, 0, -5), _entry(1, 1, -3), _entry(2, 2, -4), _entry(0, 1, 4), _entry(0, 2, 6), _entry(1, 2, 2)
        );
    }

    function _refEnergy(bytes memory q, uint256 x) internal pure returns (int256 e) {
        for (uint256 k = 0; k < q.length / 6; k++) {
            uint8 i = uint8(q[6 * k]);
            uint8 j = uint8(q[6 * k + 1]);
            uint32 raw = (uint32(uint8(q[6 * k + 2])) << 24) | (uint32(uint8(q[6 * k + 3])) << 16)
                | (uint32(uint8(q[6 * k + 4])) << 8) | uint32(uint8(q[6 * k + 5]));
            int32 w = int32(raw);
            if ((x >> i) & 1 == 1 && (x >> j) & 1 == 1) e += w;
        }
    }

    function _post(bytes memory q, uint16 n, int256 accept) internal returns (uint256 id) {
        vm.prank(client);
        id = m.postJob{value: 1 ether}(q, n, 5, 5, accept, bytes32("toy"));
    }

    function _commit(uint256 id, address who, uint256 x, bytes32 salt) internal {
        bytes32 c = m.commitmentFor(id, who, x, salt);
        vm.prank(who);
        m.commit(id, c);
    }

    // ───────────────────────────────────────────── energy
    function test_energyToy() public view {
        bytes memory q = _toy();
        assertEq(m.energy(q, 0), 0);
        assertEq(m.energy(q, 1), -5);
        assertEq(m.energy(q, 3), -4);
        assertEq(m.energy(q, 6), -5);
        assertEq(m.energy(q, 7), -5 - 3 - 4 + 4 + 6 + 2);
    }

    function testFuzz_energyMatchesReference(uint256 seed, uint256 x) public view {
        uint16 n = uint16(bound(seed, 1, 256));
        uint256 entries = bound(seed >> 16, 1, 300);
        bytes memory q;
        for (uint256 k = 0; k < entries; k++) {
            uint256 r = uint256(keccak256(abi.encode(seed, k)));
            uint8 a = uint8(r % n);
            uint8 b = uint8((r >> 8) % n);
            (uint8 i, uint8 j) = a <= b ? (a, b) : (b, a);
            int32 w = int32(uint32(r >> 32));
            q = bytes.concat(q, _entry(i, j, w));
        }
        if (n < 256) x &= (uint256(1) << n) - 1;
        assertEq(m.energy(q, x), _refEnergy(q, x));
    }

    // ───────────────────────────────────────────── happy path
    function test_fullFlow_bestSolverPaid() public {
        bytes memory q = _toy();
        uint256 id = _post(q, 3, type(int256).max);

        _commit(id, alice, 3, SALT_A); // -4
        _commit(id, bob, 6, SALT_B); // -5

        _advance(6); // commit window over
        vm.prank(alice);
        assertEq(m.reveal(id, 3, SALT_A, q), -4);
        vm.prank(bob);
        assertEq(m.reveal(id, 6, SALT_B, q), -5);

        _advance(5);
        m.settle(id);

        QuboMarket.Job memory j = m.getJob(id);
        assertEq(j.bestSolver, bob);
        assertEq(j.bestEnergy, -5);
        assertEq(j.bestX, 6);
        assertEq(m.balances(bob), 1 ether);

        uint256 before = bob.balance;
        vm.prank(bob);
        m.withdraw();
        assertEq(bob.balance - before, 1 ether);
        assertEq(address(m).balance, 0);
    }

    /// The race that showed up on the Home VPS run: both solvers hit the optimum, the LATER committer
    /// reveals first. Priority must stay with whoever committed first.
    function test_tieGoesToEarlierCommit_evenIfRevealedLater() public {
        bytes memory q = _toy();
        uint256 id = _post(q, 3, type(int256).max);
        _commit(id, alice, 1, SALT_A); // -5, block b
        _advance(1);
        _commit(id, bob, 6, SALT_B); // -5, block b+1
        _advance(6);
        vm.prank(bob);
        m.reveal(id, 6, SALT_B, q); // bob wins the reveal race...
        assertEq(m.getJob(id).bestSolver, bob);
        vm.prank(alice);
        m.reveal(id, 1, SALT_A, q); // ...but alice committed first
        QuboMarket.Job memory j = m.getJob(id);
        assertEq(j.bestSolver, alice);
        assertEq(j.bestX, 1);
    }

    function test_sameCommitBlockTieGoesToEarlierReveal() public {
        bytes memory q = _toy();
        uint256 id = _post(q, 3, type(int256).max);
        _commit(id, alice, 1, SALT_A);
        _commit(id, bob, 6, SALT_B); // same block
        _advance(6);
        vm.prank(bob);
        m.reveal(id, 6, SALT_B, q);
        vm.prank(alice);
        m.reveal(id, 1, SALT_A, q);
        assertEq(m.getJob(id).bestSolver, bob);
    }

    function test_strictlyBetterBeatsEarlierCommit() public {
        bytes memory q = _toy();
        uint256 id = _post(q, 3, type(int256).max);
        _commit(id, alice, 3, SALT_A); // -4, early
        _advance(2);
        _commit(id, bob, 6, SALT_B); // -5, late
        _advance(6);
        vm.prank(alice);
        m.reveal(id, 3, SALT_A, q);
        vm.prank(bob);
        m.reveal(id, 6, SALT_B, q);
        assertEq(m.getJob(id).bestSolver, bob);
    }

    /// Reserving an early slot with a placeholder and swapping in the real answer later must not keep the slot.
    function test_recommitResetsPriority() public {
        bytes memory q = _toy();
        uint256 id = _post(q, 3, type(int256).max);
        _commit(id, alice, 0, SALT_A); // placeholder, block b
        _advance(1);
        _commit(id, bob, 6, SALT_B); // real optimum, block b+1
        _advance(1);
        _commit(id, alice, 1, SALT_A); // alice swaps to an optimum, block b+2
        (, uint64 aliceBlock) = m.commitments(id, alice);
        (, uint64 bobBlock) = m.commitments(id, bob);
        assertGt(aliceBlock, bobBlock);
        _advance(6);
        vm.prank(alice);
        m.reveal(id, 1, SALT_A, q);
        vm.prank(bob);
        m.reveal(id, 6, SALT_B, q);
        assertEq(m.getJob(id).bestSolver, bob);
    }

    function test_zeroCommitmentRejected() public {
        uint256 id = _post(_toy(), 3, type(int256).max);
        vm.prank(alice);
        vm.expectRevert(QuboMarket.NoCommitment.selector);
        m.commit(id, bytes32(0));
    }

    // ───────────────────────────────────────────── quality bar / refunds
    function test_acceptEnergyRefundsClient() public {
        bytes memory q = _toy();
        uint256 id = _post(q, 3, -5); // must reach -5
        _commit(id, alice, 3, SALT_A); // -4, not good enough
        _advance(6);
        vm.prank(alice);
        m.reveal(id, 3, SALT_A, q);
        _advance(5);
        m.settle(id);
        assertEq(m.balances(alice), 0);
        assertEq(m.balances(client), 1 ether);
    }

    function test_noRevealsRefundsClient() public {
        uint256 id = _post(_toy(), 3, type(int256).max);
        _advance(11);
        m.settle(id);
        assertEq(m.balances(client), 1 ether);
    }

    // ───────────────────────────────────────────── adversarial
    function test_copiedCommitmentIsUseless() public {
        bytes memory q = _toy();
        uint256 id = _post(q, 3, type(int256).max);
        bytes32 c = m.commitmentFor(id, alice, 6, SALT_A);
        vm.prank(alice);
        m.commit(id, c);
        vm.prank(bob);
        m.commit(id, c); // bob copies alice's commitment from the mempool
        _advance(6);
        vm.prank(bob);
        vm.expectRevert(QuboMarket.CommitmentMismatch.selector);
        m.reveal(id, 6, SALT_A, q);
    }

    function test_revealTimingEnforced() public {
        bytes memory q = _toy();
        uint256 id = _post(q, 3, type(int256).max);
        _commit(id, alice, 6, SALT_A);
        vm.prank(alice);
        vm.expectRevert(QuboMarket.RevealNotOpen.selector);
        m.reveal(id, 6, SALT_A, q);

        _advance(6);
        vm.prank(bob);
        vm.expectRevert(QuboMarket.CommitClosed.selector);
        m.commit(id, bytes32(uint256(1)));

        _advance(5);
        vm.prank(alice);
        vm.expectRevert(QuboMarket.RevealClosed.selector);
        m.reveal(id, 6, SALT_A, q);
    }

    function test_wrongInstanceRejected() public {
        bytes memory q = _toy();
        uint256 id = _post(q, 3, type(int256).max);
        _commit(id, alice, 6, SALT_A);
        _advance(6);
        bytes memory forged = bytes.concat(_entry(1, 1, -1000));
        vm.prank(alice);
        vm.expectRevert(QuboMarket.InstanceMismatch.selector);
        m.reveal(id, 6, SALT_A, forged);
    }

    function test_outOfRangeBitsRejected() public {
        bytes memory q = _toy();
        uint256 id = _post(q, 3, type(int256).max);
        _commit(id, alice, 14, SALT_A);
        _advance(6);
        vm.prank(alice);
        vm.expectRevert(QuboMarket.SolutionOutOfRange.selector);
        m.reveal(id, 14, SALT_A, q);
    }

    function test_doubleRevealAndDoubleSettleBlocked() public {
        bytes memory q = _toy();
        uint256 id = _post(q, 3, type(int256).max);
        _commit(id, alice, 6, SALT_A);
        _advance(6);
        vm.startPrank(alice);
        m.reveal(id, 6, SALT_A, q);
        vm.expectRevert(QuboMarket.NoCommitment.selector);
        m.reveal(id, 6, SALT_A, q);
        vm.stopPrank();
        _advance(5);
        m.settle(id);
        vm.expectRevert(QuboMarket.NotSettleable.selector);
        m.settle(id);
    }

    function test_settleBeforeRevealEndBlocked() public {
        uint256 id = _post(_toy(), 3, type(int256).max);
        _advance(10); // == revealEnd
        vm.expectRevert(QuboMarket.NotSettleable.selector);
        m.settle(id);
    }

    // ───────────────────────────────────────────── input validation
    function test_postValidation() public {
        vm.startPrank(client);
        vm.expectRevert(QuboMarket.BadFee.selector);
        m.postJob(_toy(), 3, 5, 5, 0, 0);

        vm.expectRevert(QuboMarket.BadWindow.selector);
        m.postJob{value: 1}(_toy(), 3, 2, 5, 0, 0);

        vm.expectRevert(QuboMarket.BadInstance.selector);
        m.postJob{value: 1}(_toy(), 2, 5, 5, 0, 0); // index 2 >= n

        vm.expectRevert(QuboMarket.BadInstance.selector);
        m.postJob{value: 1}(_entry(2, 1, 1), 3, 5, 5, 0, 0); // i > j

        vm.expectRevert(QuboMarket.BadInstance.selector);
        m.postJob{value: 1}(hex"0000000001", 3, 5, 5, 0, 0); // not a multiple of 6

        vm.expectRevert(QuboMarket.BadInstance.selector);
        m.postJob{value: 1}(_toy(), 0, 5, 5, 0, 0);
        vm.stopPrank();
    }

    function test_unknownJob() public {
        vm.expectRevert(QuboMarket.UnknownJob.selector);
        m.commit(7, bytes32(uint256(1)));
    }

    // ───────────────────────────────────────────── gas: dense n=64 (2080 entries)
    function test_gas_dense64Reveal() public {
        bytes memory q;
        for (uint256 i = 0; i < 64; i++) {
            for (uint256 j = i; j < 64; j++) {
                q = bytes.concat(q, _entry(uint8(i), uint8(j), int32(int256(i * 7 + j * 3) - 200)));
            }
        }
        vm.prank(client);
        uint256 id = m.postJob{value: 1 ether}(q, 64, 5, 5, type(int256).max, 0);
        uint256 x = 0xA5A5A5A5A5A5A5A5;
        _commit(id, alice, x, SALT_A);
        _advance(6);
        vm.prank(alice);
        uint256 g = gasleft();
        int256 e = m.reveal(id, x, SALT_A, q);
        emit log_named_uint("reveal gas (n=64 dense, excl. calldata)", g - gasleft());
        assertEq(e, _refEnergy(q, x));
    }
}
