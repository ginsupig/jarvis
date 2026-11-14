# Fleet control validation

Date: November 13, 2026 (America/Chicago).
Base: `9b36442c8eec6c03c114153537afa05aff17196f`.
Companion: ginsupig/MegaEmpire `codex/autonomous-fleet-control`.

## Changes

- `fleet.py`: schema-v2 delayed-research export; data-through timestamp; allocation
  regime/status readout; immutable entry regime/volatility for outcome learning.
  Legacy outcomes use only an earlier completed session (maximum four days old).
  Existing fleet lessons are retired once under fleet_label_version=2; raw history
  remains. Empty native context stays unknown rather than borrowing same-day close.
- `autopilot.py`: idempotent command keys; resume compare-and-swap against the pause
  ID; fresh MegaEmpire regime for learned controls; unknown-regime no-resume;
  request/acknowledgement wording and stored publication status.
- Regression coverage for ownership, delayed exports, entry-time labels and unknown
  regimes. Updated existing label expectations and static header-field audit entries
  with the specific justification for the new command/status accesses.

## Tests and limits

Focused command: `python -m pytest -q tests/test_autopilot.py tests/test_market.py
 tests/test_fleet_control_regressions.py tests/test_header_lines.py` (one shell line).
Result: 162 passed. The broad suite was also run against the changed branch and an
unchanged worktree at the base above, using the same Python 3.12 environment and
clearing inherited HTTP/HTTPS/ALL_PROXY variables for both runs. Exact counts below.
No paid model, broker order or external runtime was invoked by these tests.

Windows sockets, OS services, credentials and the external runtime directories were
not available for integration verification. Existing socket permission failures and
platform/time-dependent test failures are recorded rather than concealed. Full-suite
success is not claimed. Browser tests remain deselected by the project's pytest config.

Deploy the companion MegaEmpire change before running this controller: it adds
request IDs, command ownership checks and receipts. Back up the Jarvis data directory
first because entry-label migration retires old fleet lessons. Verify a paper pause
progresses from recorded to allocation_published; a runtime receipt is separate and
is currently emitted at an accepted entry guard, not on every paused heartbeat.

This PR does not turn LLM prose into arbitrary execution authority, deploy source
rewrites, prove profitability, or connect missing market-internal feeds. Refer to the
companion's implementation document for the remaining allocation-replay and host work.

## Final recorded results

| Run | Result |
|---|---|
| Focused changed components | 162 passed |
| Full changed branch | 2671 passed, 13 failed, 11 errors, 45 skipped, 10 deselected |
| Full unchanged baseline | 2667 passed, 13 failed, 11 errors, 45 skipped, 10 deselected |

The failure/error test IDs match exactly between full runs. Options contains a
date-sensitive fixture; private-data cases assume platform path behavior; socket
setup raises Operation not permitted; voice-backpressure sees environment startup
notifications. These remain unresolved baseline findings, not a green full suite.

```text
ERROR tests/test_session_steer.py::test_a_dead_transport_during_the_readback_is_readback_failed_not_cancelled
ERROR tests/test_session_steer.py::test_a_prompt_is_delivered_as_one_json_line
ERROR tests/test_session_steer.py::test_an_auth_token_is_sent_first_when_the_environment_has_one
ERROR tests/test_session_steer.py::test_an_empty_prompt_is_refused_before_it_reaches_the_socket
ERROR tests/test_session_steer.py::test_empty_prompt_is_recorded_exactly_once
ERROR tests/test_session_steer.py::test_every_steer_is_recorded_whatever_the_outcome
ERROR tests/test_session_steer.py::test_needs_a_human_hand_and_not_steerable_and_sent_are_each_recorded_once
ERROR tests/test_session_steer.py::test_no_voice_refuses_to_steer_rather_than_sending_unheard
ERROR tests/test_session_steer.py::test_performing_a_staged_steer_reads_it_back_before_sending
ERROR tests/test_session_steer.py::test_saying_stop_during_the_window_cancels_the_send
ERROR tests/test_session_steer.py::test_the_tool_stages_and_speaks_nothing_at_all
FAILED tests/test_options.py::test_describe_says_it_is_new
FAILED tests/test_private_data.py::test_search_refuses_every_spelling_of_the_root
FAILED tests/test_private_data.py::test_the_walk_never_descends_a_miscased_private_root
FAILED tests/test_session_steer.py::test_a_barge_in_during_the_readback_blocks_the_send
FAILED tests/test_session_steer.py::test_a_cancel_word_in_the_window_after_the_readback_blocks_the_send
FAILED tests/test_session_steer.py::test_a_socket_that_refuses_the_connection_is_not_live
FAILED tests/test_session_steer.py::test_a_staged_steer_is_never_performed_twice
FAILED tests/test_session_steer.py::test_a_staged_steer_still_happens_when_the_turn_ends_badly
FAILED tests/test_session_steer.py::test_a_wedged_readback_sends_nothing_and_records_readback_failed
FAILED tests/test_session_steer.py::test_the_readback_plays_after_the_turn_and_before_delivery
FAILED tests/test_session_steer.py::test_two_steers_staged_in_one_turn_are_both_performed_in_order
FAILED tests/test_voice_backpressure.py::test_a_client_whose_socket_fails_is_dropped
FAILED tests/test_voice_backpressure.py::test_a_wedged_client_does_not_delay_the_frame
```
