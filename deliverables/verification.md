# Verification

Configuration: CPython 3.12.3 / Linux, SQLite local filesystem, four workers, saved mock Responses envelopes from `tests/mock_responses/corpus-completed.json`. Model configuration recorded in each record is `gpt-4.1-mini-2025-04-14`, temperature 0, 2400 output tokens and 60-second timeout; mock observations do not call that model. No live provider observation was performed. Fake HTTP tests inject sentinel credentials directly and discard them at the adapter boundary; tests never read the host API_KEY.

Run from the repository root:

```sh
env -u API_KEY python3 -m unittest discover -s tests -v > planning/test-run.txt 2>&1
env -u API_KEY python3 -m access_intake intake materials/requests --provider mock --fixtures tests/mock_responses --state .state/intake.sqlite --output deliverables
env -u API_KEY python3 -m access_intake intake tests/inputs --provider mock --fixtures tests/mock_responses --state .state/intake.sqlite --output deliverables
env -u API_KEY python3 -m access_intake queue --state .state/intake.sqlite
env -u API_KEY python3 -m access_intake show IT-001.json --state .state/intake.sqlite
env -u API_KEY python3 -m access_intake resume --state .state/intake.sqlite --output deliverables
env -u API_KEY python3 -m access_intake export --state .state/intake.sqlite --output deliverables
env -u API_KEY python3 -m access_intake accuracy --expected tests/expected.json --actual deliverables/extractions.jsonl --output deliverables/accuracy.md
env -u API_KEY python3 -m tools.build_evidence
```

Intended comparisons:

| Check | Assertion and comparison |
|---|---|
| `BoundaryTests.test_t02_original_bytes_identity_metadata_full_message` | Independently hash original bytes, compare filenames/messages across all 21 sources; exercise full 200-word input with conflicting export metadata. |
| `BoundaryTests.test_t03_every_schema_constraint` | Every required key, closed properties, nullable scalar types, all enum alternatives and both string arrays accept/reject against the supplied schema. |
| `BoundaryTests.test_t04_real_adapter_fake_transport_request_config_key_mapping_redaction` | Fake HTTP observes exact message, configured model/settings and schema; injected key is used only in the HTTP header and absent from request evidence/errors. |
| `BoundaryTests.test_t05_completion_refusal_interruption_malformed_and_quote_gate` | Saved success envelope is accepted, saved refusal/incomplete/cut-off/invalid quotes and malformed structures rejected before routing. |
| `PolicyTests.test_t06_to_t20_all_source_facts_and_ordered_routes` | All 21 independently authored field expectations, missing/conflict issues, source quote evidence and exact policy route tuples match; managers never approve requests. |
| `PolicyTests.test_t20_precedence_and_every_missing_fact` | Override takes precedence over withdrawal; each policy-required fact independently triggers clarify when null; production/read, staging/write and staging/admin require Security Review. |
| `PersistenceTests.test_t21_unchanged_reuse_unique_and_batch_omission` | No extra extraction call on unchanged rerun; absent batch documents remain and current entries are unique. |
| `PersistenceTests.test_t22_changed_source_independent_documents_and_history` | Changed bytes publish a new hash/request ID; old attempt remains; same request ID in a different filename stays independent. |
| `PersistenceTests.test_t24_t25_settings_policy_prompt_provenance_refresh` | Model/policy fingerprint changes refresh results and retain old settings/hash in history. Policy whitespace refresh checks invalidation, not an unprovided future policy's meaning. |
| `PersistenceTests.test_t26_to_t30_failures_retry_failed_after_success_and_repair` | Every saved fault and provider failure produces error/null extraction/retry command; a failed retry replaces current success while retaining prior success in history; repair restores review. |
| `PersistenceTests.test_t31_incremental_visibility_while_slow_request_pending` | Event-controlled provider delay leaves another committed result inspectable before release. |
| `PersistenceTests.test_t33_overlapping_claims_and_stale_revision_fencing` | Independent Store connections claim once; later registration wins even when an older result finishes later. |
| `PersistenceTests.test_t34_expired_claim_resume_and_late_worker_cannot_publish` | Expired lease recovers without losing completed work; stale token cannot overwrite the reclaimed attempt. |
| `PersistenceTests.test_t36_consistent_generation_exports_and_separate_history` | Pinned generation's queue and extraction IDs, attempt IDs and source hashes agree; prior versions export separately. |
| `ResponseEdgeTests` | Fake HTTP 429, 503, 401 and timeouts receive safe messages; actual response model identifies producer; strict JSON rejects duplicates/NaN/Infinity; metadata is not business evidence. |
| `ProcessTests.test_actual_process_kill_resume_preserves_completed_attempt` | Kill an actual worker process after one persisted success and one claim; resume through another Engine preserves the success and completes the expired claim. Test uses an already expired synthetic lease. |
| `ProcessTests.test_two_operator_processes_one_current_record_and_consistent_exports` | Two actual processes share SQLite and output path, yielding 20 unique attempts/current records and a coherent pinned generation. |
| `ProcessTests.test_mock_burst_2000_documents_bounded_workers` | Process 2,000 distinct filenames with four workers using one saved source/envelope. These are identity aliases, so identifier mismatches correctly clarify; this is a scheduler/state exercise. |
| `AccuracyTests.test_t38_exact_counts_null_arrays_failures_missing_and_factual_errors` | Independently calculate 294 exact field slots / 63 route slots; one changed permission, a failed record and missing record score 265/294 and report differences; duplicate IDs reject. |
| `CLITests` | Replay intake/queue/show/retry/resume/export/accuracy commands offline; inspect all required deliverables, source hashes, response labels and independently expected facts. |

Observed repairs: initially no production modules existed and four baseline tests errored (`planning/baseline-test-run.txt`). New application tests failed first (`planning/tdd-red.txt`); `planning/tdd-progress.txt` records the intermediate remaining CLI/accuracy/bundle gaps. Implemented the provider boundary, validation, ordered routing, transactional attempts, leased workers, CLI, comparisons and bundle. `planning/tdd-operations.txt` records the first additional operational checks (existing functionality may already pass); final verification is `planning/test-run.txt`.

Observed mock comparison: 21 records, 294/294 literal field matches, 63/63 route matches, zero processing failures and no factual-error candidates. The oracle and mock envelopes were authored from the same source catalogue before production code. This verifies deterministic handling/comparison, not independent live language understanding. Deliberately corrupted measured values in the accuracy test prove the comparator reports mismatches. Request evidence is illustrative, built by the real request constructor without calling the provider.

Operating assumptions and limits: Linux/POSIX, CPython 3.12, one host and local filesystem; operators share one SQLite state and export directory. Each process has 1–32 bounded workers, four by default; aggregate provider rate across processes is not globally limited. Adjust workers and retry 429/timeouts explicitly. The 2,000-document test is a mock state/scheduling exercise, not a live throughput or completion-time benchmark. No daily SLA is established. No network-filesystem or multi-host coordination was tested. Earlier histories/generations are retained indefinitely, so operators manage disk capacity.

Interruption behavior: source bytes and effective configuration are registered before model calls. Successful committed attempts survive a worker termination. Running claims use random fencing tokens, 300-second leases and 60-second heartbeat; resume skips active claims and reclaims expired ones. Calls can repeat after a crash between provider response and DB commit. There is no exactly-once external-call guarantee. Actual kill verification uses a past synthetic lease rather than waiting five minutes. Heartbeat timing and power-loss behavior were not fault-injected. SQLite FULL synchronization and fsynced generation files support durability. Read `current` once to pin a generation when comparing files; separate root link opens can straddle a publication. Use queue/show to inspect pending state; exported completed records can still show the earlier committed attempt while its newer attempt is pending, with both attempts distinguishable in state.

The last registered attempt, rather than latest finishing response or export timestamp, determines current revision. Review semantics live in `access_intake/policy.py`; `--policy` also supplies trusted prompt text and a content fingerprint, and does not compile arbitrary prose into routing code. A semantic policy update requires changing that router and rerunning intake. Current implementation/schema/prompt/policy/fixture identity invalidates reuse conservatively. Retry/resume enforce the same application/fixture revision before provider calls; new code/configuration should be refreshed with intake. Input JSON rejects duplicate keys and non-finite numbers. Invalid exports produce per-document errors; unreadable files are command errors. Empty/whitespace facts are normalized to null with issues. Resolved dates must be real calendar dates in YYYY-MM-DD. General JSON Schema dialects beyond keywords present in the supplied schema are not implemented.

Source-grounding limits: exact quote validation and trusted instructions do not prove every scalar fact or detect all novel prompt attacks. The explicit override patterns and saved cases are verified. Human reviewers inspect facts/evidence and own all approval decisions. Model-account availability, live accuracy, live refusal/rate behavior and provider billing were not observed. Configuration/provenance is stored without credentials; arbitrary provider bodies and exception text are excluded from operational errors.

Submission requirements remain external: no commit/push or Classroom submission is performed by this implementation task. Entire was inspected and reports enabled with divergent tracking; that alone does not prove a captured submitted-commit association. The facilitator's capture instructions/submission destination and a verified submitted commit are still needed (R-87–R-89). No manual checkpoint or capture setup changes were made.

Additional observed repairs: test-first fixture-drift and report-label checks (`planning/tdd-drift-focused-red.txt`) exposed stale fixture provenance and a hardcoded mock label. Workers now reject changed code/fixtures before calls, and reports derive observation labels from recorded response sources. CLI recovery/error-command checks already passed when added (`planning/tdd-cli-recovery.txt`).

A final test-first provenance repair (`planning/tdd-refusal-provenance-red.txt`) moves actual response-model recording before extraction validation, so refusals/interrupted replies retain their actual producer even when the request used a model alias.
