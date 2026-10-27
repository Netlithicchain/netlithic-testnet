> **Superseded by [POINTS_V4.md](POINTS_V4.md).** Same idea (ship inside 0.5.1), but the shipped rules are the v4 ones and the first payout moves to block 7880. Kept for the reasoning on timing and the min-peer gate.

# Points v3: how it ships with 0.5.1, and the min-peer gate

**Status: PROPOSAL for the founder. Nothing here has been run, pushed, tagged or deployed.** Heights and dates are estimates;
re-run the numbers from the live `/status` on the day (section 3).

## 1. Why v3 must ship inside 0.5.1 (or before block 5000 at the latest)

* 0.5.1 is not tagged or deployed yet. It already forces every node to upgrade before block 5000 (pause key, `MIN_PEER_VERSION = 0.5.1`).
* v3 is a consensus change for the first payout at **6440**. A node without v3 computes different payouts there and forks.
* Block 5000 to 6440 is only **about 24 hours** at the 60 s LWMA target (5000 to 6000 about 16.7 h). So there is no calm
  window after 5000 for a separate 0.5.2 with proper notice: a second release would have to be installed by every node in
  roughly one day, or before 5000 anyway.

**Decision proposed: ship v3 inside 0.5.1, frozen by a freeze date, one release, one notice, one min-peer gate.**
Then 0.5.1 is "0.5.0 + pause key + points v3". Never reuse the string "0.5.1" for a build with different consensus than the one
announced: the rules hash in `/status` and `/hello` (`points_v3_rules`) catches a mismatch between builds with the same version, but
nodes still look identical to users.

**Fallback if 0.5.1 must go out without v3:** (a) call the next release 0.5.2, set its `MIN_PEER_VERSION` gate with its own 14-day
notice, and finish it before 5000 (same schedule problem as 0.5.1); or (b) rely on the pause switch: pause payouts before 6440
(the pause is valid from 5000, auto-expires after 10,080 blocks, about 7 days at the 60 s pace) so 0.5.2 gets a real notice. (b) works
only if the pause key exists and every node already runs 0.5.1, and a pause is a loud, visible event. It is a safety net, not a plan.

**How v3 and 0.5.1 combine (tested locally).** `points-fix-v3` merged into `release-0.5.1` in a throwaway worktree:
one trivial conflict (the `from core import ...` line in `p2p_http.py`: keep `import pause` and add `points_v3_rules_hash`). Full suite on the
merge: **668 passed, 2 skipped**. The merge was not committed anywhere (no new branch, no push). Combination steps for the real release: merge
`points-fix-v3` into `release-0.5.1`, resolve that one line, set the pause key with `ops/set_pause_pubkey.py`, run `ops/check_family_issuers.py`
against both seeds, run the drill, then tag.

## 2. The min-peer gate (0.5.1)

What exists: `MIN_PEER_VERSION = "0.5.1"` and `UPGRADE_DEADLINE_HEIGHT = 4900` in `netlithic_version.py`; `handle_hello` warns an outdated peer
below the deadline height and refuses **new** handshakes from it at or above. Already-connected peers are not dropped, and blocks pushed to
`/receive_block` are not checked, so it is an exposure limit and a warning, not a hard guard. v3 adds a second, separate check in the same place:
a peer that reports a different `points_v3_rules` is refused at hello (any height).

Requested: at least **14 days** between the 0.5.1 release and the gate, and the gate well before 4900. The two limits conflict whenever blocks
arrive fast, because the gate is a *height* and the notice is *days*:

| Blocks per hour before 5000 | 4900 reached | 5000 | 6000 | 6440 (first payout) | Latest release date for "14 days, then the gate, then 2 more days before 4900" |
|---|---|---|---|---|---|
| 6 (slow) | Sun Nov 22 | Mon Nov 23 | Tue Nov 24 | Tue Nov 24 14:00 | Fri Nov 6 |
| **9 (the last 300 blocks: 8.8)** | **Thu Nov 12** | **Fri Nov 13** | **Sat Nov 14** | **Sat Nov 14 09:00** | **Tue Oct 27** |
| 13 (the best recent 100 blocks) | Fri Nov 6 | Sat Nov 7 | Sat Nov 7 | Sun Nov 8 | already passed |
| 19 (fast) | Mon Nov 2 | Mon Nov 2 | Tue Nov 3 | Tue Nov 3 | already passed |

(All times America/Denver, computed from height 583 at 22:00 MDT Fri Oct 23, 2026; after 5000 at the 60 s target, 60 blocks an hour.
Treat every date before 5000 as an estimate; the pace swung between 7 and 13 blocks an hour over the last 600 blocks.)

**Options for the gate:**
1. **Height gate, set at release time (simplest, no new code).** Pick `UPGRADE_DEADLINE_HEIGHT` at the release from the pace of the previous
   7 days: `gate = height_at_release + 14 days * pace`, then lower it by 10% as margin. Re-check weekly; if blocks speed up, announce the new date.
   At pace 9 and a release on Tue Oct 27, that is about height 1,420 + 3,024 = about 4,440, not 4,900.
2. **Date gate plus height backstop (recommended if the pace cannot be trusted).** Refuse old peers when `now >= RELEASE + 14 days`
   **or** height >= 4900, whichever first. It is a non-consensus handshake check, so a wall-clock rule is safe; it needs a small change in
   `handle_hello` and a test on the 0.5.1 branch, which I did not make here to keep the release branch untouched.
3. Keep 4900 and publish the release at once. At pace 9 that gives about 16 days of notice, at pace 13 about 10 days, at pace 19 about 6 days. It does **not** meet "14 days and well before 4900" unless blocks stay slow.

**Recommendation: option 2.** In every case the release date is the lever: the earlier 0.5.1 is out, the more the 14 days fit. The blocking item is the founder's
pause public key (ceremony), so the real schedule starts when that key is handed over.

## 3. Re-computing the schedule on release day (read only)
```bash
curl -s http://137.184.199.238:8000/status | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['height'],d['latest_timestamp'])"
```
Do it twice a day apart, `pace = delta_height / delta_hours`, and `date_of(h) = now + (h - height) / pace` before 5000, 60 blocks an hour after.

## 4. Release checklist for v3 (adds to RELEASE_PLAN.md)
- [ ] `pytest -q`, bandit, semgrep on the merged release commit; `tests/test_points_v3_hardening.py::test_release_gate_issuer_list_is_non_empty_and_valid` passes.
- [ ] `python ops/check_family_issuers.py http://137.184.199.238:8000` and `.../157.230.63.97:8000` exit 0 (or 2 with every unlisted funder understood).
- [ ] Faucet address confirmed from the public faucet `/api/info` against `contributions.FAMILY_ISSUERS`, whether or not the family rule is on.
- [ ] the founder decided: family rule on/off (proposed off), per-point ceiling (proposed value in POINTS_FIX.md), and B5.
- [ ] Both seeds show the same `points_v3_rules` in `/status` after the upgrade.
- [ ] Notice text sent (the founder approves wording): the release, the gate date, what changes for payouts.
