# RandomX proof-of-work (PROPOSAL, pending Aric)

Goal: replace SHA-256 PoW with RandomX (CPU-friendly, ASIC/GPU-resistant, used by Monero)
at a planned testnet fork height, through the official C library tevador/RandomX.
**Never a pure-Python hash** (it would be thousands of times too slow to verify and unsafe).

## Python bindings that exist (checked 2026-10-23)

| Package | Last release | Upkeep | Notes |
|---|---|---|---|
| `RandomX` (PyPI), github xloem/RandomX-Python | 1.1.10.post3, **Jun 5 2022** | Last push Feb 2024, 1 contributor, 4 stars | Cython over RandomX v1.1.10. **Source only, no wheels**; compiles with `-march=native` (binary not portable to other CPUs). Effectively unmaintained. |
| `openssl-encrypt-randomx` (module `randomx_native`) | 1.0.0, Aug 22 2026 | One release, owned by another project (a KDF) | Same RandomX v1.1.10 pin, W^X by default, portable AES, pinned/sha256-checked sources. Source only; needs C++ and Rust toolchains. Not built for mining. |
| `pyrx2` (github Vizonex) | not on PyPI | 1 star, "testing" on its TODO | Not usable. |
| `pyrx` (PyPI) | 2013 | unrelated package | Not RandomX. |

No maintained binding ships prebuilt wheels. Both real ones built on the box only after
installing g++, cmake and python3-dev.

## Benchmark (box, 8 vCPU, 1 thread, secure JIT, no large pages)

| Mode | Init | Per hash | Hashes/s | Peak memory |
|---|---|---|---|---|
| Light (verify) - RandomX | 0.40 s | 25.5 ms | 39 | 277 MB |
| Full (mine) - RandomX | 5.0 s | 2.78 ms | 360 | 2,356 MB |
| Light - randomx_native | 0.39 s | 25.7 ms | 39 | 275 MB |
| Full - randomx_native | 5.2 s | 2.69 ms | 371 | 2,353 MB |
| SHA-256 today (hashlib) | - | ~0.001-0.003 ms | ~500,000+ | - |

Both bindings give identical hashes. Block verification = one RandomX hash per block:
a light node re-checking 5,000 blocks takes ~2 min; one year of 60 s blocks (525,600)
takes ~3.7 h on one core in light mode. A seed change (every 2,048 blocks) costs ~0.4 s
(light) or ~5 s (full).

## Fork plan

1. Not in the Nov 9 / block-5000 release. That release already carries LWMA, the reorg
   limit, anti-farming v2 and the pause; adding a PoW change makes it riskier and needs a
   wheel/installer pipeline that does not exist yet.
2. Own the binding: fork the RandomX-Python Cython wrapper (or vendor tevador/RandomX
   directly) into a `netlithic-randomx` package, pinned and sha256-checked, built with
   cibuildwheel for Windows/macOS/Linux x64+arm64, **without -march=native**.
3. Separate release ~4 weeks after 5000, fork height e.g. **block 15,000** (~1 week at 60 s
   after release), with the same "update before block N" notice.
4. Rule: from the fork, `pow_hash = RandomX(key = hash of block seed_height(h), input =
   canonical header without hash)`, valid when `int(pow_hash) * work < 2**256` (same LWMA
   target rule). Block id stays SHA-256. LWMA work resets to a minimum at the fork.
5. Prototype in this branch: `randomx_pow.py` (`RANDOMX_FORK_HEIGHT = None`, off), tests in
   `tests/test_proposals.py` (skip if no binding). Not wired into consensus.

## Risks

- **Memory:** verifiers need ~256 MB (light); efficient miners need ~2.1 GB+ (full). Fine on
  desktops, tight on 1 GB servers/Raspberry Pi; relays should verify in light mode.
- **Verification cost:** ~10,000x slower than SHA-256 per block; initial sync gets slower
  (hours per year of chain). Checkpoints help.
- **Windows desktop packaging:** a C++ extension must be built with MSVC and bundled by
  PyInstaller. RandomX code is a known **antivirus "coin miner" signature**; an unsigned app
  with RandomX inside is likely to be flagged or quarantined. Code signing should come first.
- **Binding upkeep:** the only general binding is abandoned since 2022; we would own it.
- **JIT/W^X:** some hardened systems block the JIT; the interpreted fallback is ~10x slower.
- **Timeline:** realistic only after Nov 9; trying to fit it would put the block-5000 date at risk.

## Alerts (kept and added)

Before this branch, healthcheck had no reorg alert and no miner-share alert (only down,
stalled, out-of-sync, faucet, certificate, disk). Added as proposals in `ops/healthcheck.py`:
`reorg_problem()` (alert when 3+ recent blocks were replaced) and `miner_share_problems()`
(alert when one address made >40% of the last 50+ blocks). Tested; not yet wired into the
running check, which only uses `/status` today; wiring needs a read of recent block hashes.

## Recommendation

Do not ship RandomX in the block-5000 release. Approve it as a follow-up fork after code
signing, with our own pinned binding and prebuilt wheels.
