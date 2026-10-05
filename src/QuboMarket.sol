// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title QuboMarket
/// @notice Reference client-side implementation of a proof-of-useful-work job market for QMS.
///         A client posts a QUBO instance and locks a fee. Solvers commit to a solution, then reveal it.
///         The contract evaluates every revealed solution on-chain (x^T Q x) — no oracle, no trust —
///         and pays the lowest-energy solution once the reveal window closes.
///
/// QUBO wire format (`q`): a packed list of upper-triangular entries, 6 bytes each:
///     [ i : uint8 ][ j : uint8 ][ w : int32 big-endian ]      with i <= j < n
///     E(x) = sum_k  w_k * x_{i_k} * x_{j_k}                    (diagonal entries are linear terms)
/// Solutions are bitmasks: bit i of `x` is variable x_i. Lower energy is better (minimization).
///
/// Only keccak256(q) is stored. The full instance is emitted in `JobPosted`, so the chain itself is the
/// data-availability layer: solvers read the problem from logs, nothing lives off-chain.
///
/// QMS testnet notes baked into the design:
///   - windows are measured in blocks, not seconds (PoUW block times are probabilistic, 10 s target)
///   - no use of PREVRANDAO (returns 0 on QMS v0.1). Equal energies go to the EARLIEST COMMIT (first to
///     solve), then to the earlier reveal — never to whoever has the lowest-latency RPC at reveal time
///   - `finalized` returns genesis on v0.1, so off-chain actors should wait on confirmation depth instead
contract QuboMarket {
    // ─────────────────────────────────────────────────────────────── constants
    uint256 public constant MAX_N = 256;
    uint256 public constant MAX_ENTRIES = 8192;
    uint64 public constant MIN_WINDOW = 3; // blocks
    uint64 public constant MAX_WINDOW = 50_000; // blocks (~5.8 days at 10 s)
    uint256 private constant ENTRY_BYTES = 6;

    // ─────────────────────────────────────────────────────────────── storage
    struct Job {
        address client; //       slot 0
        uint64 commitEnd; //      block number: commits accepted while block.number <= commitEnd
        uint16 n; //
        bool settled; //
        address bestSolver; //   slot 1
        uint64 revealEnd; //      block number: reveals accepted while commitEnd < block.number <= revealEnd
        uint96 fee; //           slot 2
        uint64 bestCommitBlock; //  commit block of the current best (tie-break)
        bytes32 qHash; //        slot 3
        int256 acceptEnergy; //  slot 4 — winner is paid only if bestEnergy <= acceptEnergy
        int256 bestEnergy; //    slot 5
        uint256 bestX; //        slot 6
    }

    struct Commitment {
        bytes32 hash;
        uint64 blockNumber; // re-committing resets it, so an early junk commit can't reserve priority
    }

    Job[] internal _jobs;
    mapping(uint256 => mapping(address => Commitment)) public commitments;
    mapping(address => uint256) public balances;
    uint256 private _lock = 1;

    // ─────────────────────────────────────────────────────────────── events
    event JobPosted(
        uint256 indexed jobId,
        address indexed client,
        uint256 fee,
        uint16 n,
        uint64 commitEnd,
        uint64 revealEnd,
        int256 acceptEnergy,
        bytes32 meta,
        bytes q
    );
    event Committed(uint256 indexed jobId, address indexed solver, uint64 blockNumber);
    event Revealed(
        uint256 indexed jobId, address indexed solver, int256 energy, uint256 x, uint64 commitBlock, bool isBest
    );
    event Settled(uint256 indexed jobId, address indexed winner, int256 energy, uint256 x, uint256 payout);
    event Withdrawn(address indexed account, uint256 amount);

    // ─────────────────────────────────────────────────────────────── errors
    error BadInstance();
    error BadWindow();
    error BadFee();
    error UnknownJob();
    error CommitClosed();
    error RevealNotOpen();
    error RevealClosed();
    error NoCommitment();
    error CommitmentMismatch();
    error InstanceMismatch();
    error SolutionOutOfRange();
    error NotSettleable();
    error NothingToWithdraw();
    error TransferFailed();
    error Reentrancy();

    modifier nonReentrant() {
        if (_lock != 1) revert Reentrancy();
        _lock = 2;
        _;
        _lock = 1;
    }

    // ─────────────────────────────────────────────────────────────── client side

    /// @param q             packed QUBO instance (see header)
    /// @param n             number of binary variables (1..256)
    /// @param commitBlocks  length of the commit window, in blocks
    /// @param revealBlocks  length of the reveal window, in blocks
    /// @param acceptEnergy  quality bar: pay only if the best energy is <= this (use type(int256).max for "any")
    /// @param meta          free 32-byte tag, e.g. keccak of the off-chain universe the instance encodes
    function postJob(
        bytes calldata q,
        uint16 n,
        uint64 commitBlocks,
        uint64 revealBlocks,
        int256 acceptEnergy,
        bytes32 meta
    ) external payable returns (uint256 jobId) {
        if (msg.value == 0 || msg.value > type(uint96).max) revert BadFee();
        if (
            commitBlocks < MIN_WINDOW || commitBlocks > MAX_WINDOW || revealBlocks < MIN_WINDOW
                || revealBlocks > MAX_WINDOW
        ) revert BadWindow();
        _validate(q, n);

        uint64 commitEnd = uint64(block.number) + commitBlocks;
        uint64 revealEnd = commitEnd + revealBlocks;

        jobId = _jobs.length;
        _jobs.push(
            Job({
                client: msg.sender,
                commitEnd: commitEnd,
                n: n,
                settled: false,
                bestSolver: address(0),
                revealEnd: revealEnd,
                fee: uint96(msg.value),
                bestCommitBlock: type(uint64).max,
                qHash: keccak256(q),
                acceptEnergy: acceptEnergy,
                bestEnergy: type(int256).max,
                bestX: 0
            })
        );

        emit JobPosted(jobId, msg.sender, msg.value, n, commitEnd, revealEnd, acceptEnergy, meta, q);
    }

    // ─────────────────────────────────────────────────────────────── solver side

    /// @notice commitment = keccak256(abi.encode(jobId, solver, x, salt)). Binding the solver address
    ///         means a copied commitment is useless to anyone else. Re-committing overwrites AND resets the
    ///         commit block, so tie priority always belongs to the solution actually revealed.
    function commit(uint256 jobId, bytes32 commitment) external {
        Job storage job = _job(jobId);
        if (block.number > job.commitEnd) revert CommitClosed();
        if (commitment == bytes32(0)) revert NoCommitment();
        commitments[jobId][msg.sender] = Commitment(commitment, uint64(block.number));
        emit Committed(jobId, msg.sender, uint64(block.number));
    }

    /// @notice Reveal a committed solution. The instance must be passed again as calldata and is checked
    ///         against the stored hash, then the energy is computed on-chain.
    function reveal(uint256 jobId, uint256 x, bytes32 salt, bytes calldata q) external returns (int256 e) {
        Job storage job = _job(jobId);
        if (block.number <= job.commitEnd) revert RevealNotOpen();
        if (block.number > job.revealEnd) revert RevealClosed();

        Commitment memory c = commitments[jobId][msg.sender];
        if (c.hash == bytes32(0)) revert NoCommitment();
        if (c.hash != commitmentFor(jobId, msg.sender, x, salt)) revert CommitmentMismatch();
        delete commitments[jobId][msg.sender]; // one reveal per commitment

        if (keccak256(q) != job.qHash) revert InstanceMismatch();
        uint256 n = job.n;
        if (n < 256 && (x >> n) != 0) revert SolutionOutOfRange();

        e = energy(q, x);
        // lower energy wins; equal energy → earlier commit wins; same commit block → earlier reveal keeps it
        bool isBest = e < job.bestEnergy || (e == job.bestEnergy && c.blockNumber < job.bestCommitBlock);
        if (isBest) {
            job.bestEnergy = e;
            job.bestX = x;
            job.bestSolver = msg.sender;
            job.bestCommitBlock = c.blockNumber;
        }
        emit Revealed(jobId, msg.sender, e, x, c.blockNumber, isBest);
    }

    // ─────────────────────────────────────────────────────────────── settlement

    /// @notice Callable by anyone once the reveal window has closed. Credits the winner if the best
    ///         solution meets the client's quality bar, otherwise refunds the client.
    function settle(uint256 jobId) external {
        Job storage job = _job(jobId);
        if (job.settled || block.number <= job.revealEnd) revert NotSettleable();
        job.settled = true;

        uint256 fee = job.fee;
        bool paid = job.bestSolver != address(0) && job.bestEnergy <= job.acceptEnergy;
        address to = paid ? job.bestSolver : job.client;
        balances[to] += fee;

        emit Settled(jobId, paid ? job.bestSolver : address(0), job.bestEnergy, job.bestX, paid ? fee : 0);
    }

    function withdraw() external nonReentrant {
        uint256 amount = balances[msg.sender];
        if (amount == 0) revert NothingToWithdraw();
        balances[msg.sender] = 0;
        (bool ok,) = msg.sender.call{value: amount}("");
        if (!ok) revert TransferFailed();
        emit Withdrawn(msg.sender, amount);
    }

    // ─────────────────────────────────────────────────────────────── views

    function jobCount() external view returns (uint256) {
        return _jobs.length;
    }

    function getJob(uint256 jobId) external view returns (Job memory) {
        return _job(jobId);
    }

    function commitmentFor(uint256 jobId, address solver, uint256 x, bytes32 salt) public pure returns (bytes32) {
        return keccak256(abi.encode(jobId, solver, x, salt));
    }

    /// @notice E(x) = sum w_k x_i x_j over the packed entries. Pure, so clients and solvers can
    ///         eth_call it to check their own numbers against the exact on-chain arithmetic.
    function energy(bytes calldata q, uint256 x) public pure returns (int256 e) {
        uint256 entries = q.length / ENTRY_BYTES;
        assembly ("memory-safe") {
            let p := q.offset
            let end := add(p, mul(entries, 6))
            for {} lt(p, end) { p := add(p, 6) } {
                let word := calldataload(p)
                let i := byte(0, word)
                let j := byte(1, word)
                if and(and(shr(i, x), shr(j, x)), 1) {
                    // bytes 2..5 → int32, sign-extended to 256 bits
                    e := add(e, signextend(3, shr(224, shl(16, word))))
                }
            }
        }
    }

    // ─────────────────────────────────────────────────────────────── internal

    function _job(uint256 jobId) internal view returns (Job storage) {
        if (jobId >= _jobs.length) revert UnknownJob();
        return _jobs[jobId];
    }

    function _validate(bytes calldata q, uint16 n) internal pure {
        uint256 len = q.length;
        if (n == 0 || n > MAX_N || len == 0 || len % ENTRY_BYTES != 0 || len / ENTRY_BYTES > MAX_ENTRIES) {
            revert BadInstance();
        }
        bool ok = true;
        assembly ("memory-safe") {
            let p := q.offset
            let end := add(p, len)
            for {} lt(p, end) { p := add(p, 6) } {
                let word := calldataload(p)
                let i := byte(0, word)
                let j := byte(1, word)
                // require i <= j < n
                if or(gt(i, j), iszero(lt(j, n))) {
                    ok := 0
                    break
                }
            }
        }
        if (!ok) revert BadInstance();
    }
}
