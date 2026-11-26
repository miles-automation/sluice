# Python test coverage

Every test function in `python/tests/` is listed here, mapped either to the
conformance fixture ids that express it (`*` matches any run of characters) or
to the reason it stays Python-only and whether the TypeScript version needs an
equivalent. `python/tests/test_conformance.py::test_coverage_map_lists_every_python_test`
fails when a test function is added, renamed or removed without updating this
file, or when an id here matches no fixture.

The Python tests that became fixtures are kept for now, so the two can be
compared while the TypeScript port lands. Whether to delete them is an open
decision; until then a behavior change updates both.

Summary: 307 test functions. 200 are expressed as fixtures, 93 stay
Python-only, 14 are the conformance runner itself. 515 fixture cases in
total; 47 of them cover behavior no existing Python test asserted (listed
at the end).

Reasons a test stays Python-only:

- It drives the MCP Python SDK end to end (proxying, tool listing, round trips,
  result metadata). The TypeScript version has its own tests against the
  TypeScript SDK and a fake downstream (`typescript/test/proxy.test.ts`,
  `pagination.test.ts`, `cli.test.ts`; task 1078).
- It injects faults or reads internals by monkeypatching Python code
  (atomicity under a failed insert, the startup canary refusing to run, internal
  counters). The TypeScript version needs equivalent unit tests of its own
  internals.
- It is timing-sensitive (timeouts, interrupts, cancellation, admission
  concurrency). Node's libuv pool changes the shape of these tests (review
  constraint 4), so each version keeps its own.
- It is a Hypothesis property. Fixed samples of each property are fixtures; the
  TypeScript version needs its own property tests.
- It pins a Python binding or SDK contract that differs in Node by design.

## Python bugs and spec gaps found while building the suite

- `python/src/sluice/infer.py` and `tests/test_infer.py` say a mixed column typed
  VARCHAR makes `median()` fail. It does not: DuckDB 1.5.5 and 2.0.1 return a
  lexicographic median over VARCHAR too (`engine.median-on-varchar-is-lexicographic-too`).
  `sum()` and `avg()` do fail. The real safeguard is `exact: false` on the
  column. Spec 001 §5.5 rule 2 itself only claims JSON is worse, which holds.
- Spec 001 §3.2 defines renaming for reserved names only. When a renamed column
  takes a name a later source key holds verbatim, Python renames that key too
  (`project.repeated-reserved-collisions-keep-allocating`). Pinned as observed.
- Cell truncation can split an escape sequence, leaving a lone backslash before
  the ellipsis (`cell.escaping-counts-toward-the-cap`). Spec 001 §6.3 does not
  say whether that is allowed. Pinned as observed.
- Python's text channel accepts the non-standard JSON literals `Infinity`,
  `-Infinity` and `NaN` (`infer.non-finite-*`, `session.handle.edge-numbers-are-flagged-inexact`).
  Spec 001 §5.1 does not decide whether that is JSON.

## tests/test_cli.py

| Test | Covered by |
|---|---|
| `test_sluice_runs_as_a_real_stdio_server` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/cli.test.ts` "Sluice runs as a real stdio server and writes only MCP to stdout". |
| `test_missing_config_exits_with_a_diagnostic` | Python-only: CLI process exit code and stderr. Config error itself is `config.missing-file-is-an-error`. TS: `typescript/test/cli.test.ts` "a missing config exits with a diagnostic". |
| `test_invalid_limits_exit_with_a_diagnostic` | Python-only: CLI process exit code and stderr. The validation is `config.*-must-be-positive`. TS: `typescript/test/cli.test.ts` "invalid limits exit with a diagnostic". |
| `test_unreachable_downstream_fails_to_start` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/cli.test.ts` "an unreachable downstream fails to start". |
| `test_startup_errors_are_unwrapped_from_exception_groups` | Python-only: anyio ExceptionGroup unwrapping is a Python runtime detail. TS: not needed. |

## tests/test_concurrency.py

| Test | Covered by |
|---|---|
| `test_a_timing_out_query_does_not_disturb_a_concurrent_query` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: needs an equivalent; review/duckdb2-node-check.md constraint 4 (libuv pool) makes it a different test. |
| `test_a_timing_out_query_does_not_disturb_a_concurrent_write` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: needs an equivalent; review/duckdb2-node-check.md constraint 4 (libuv pool) makes it a different test. |
| `test_two_concurrent_queries_both_return` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: needs an equivalent; review/duckdb2-node-check.md constraint 4 (libuv pool) makes it a different test. |

## tests/test_config.py

| Test | Covered by |
|---|---|
| `test_minimal_config_parses` | `config.minimal-config-parses` |
| `test_env_expansion` | `config.env-vars-expand` |
| `test_missing_env_var_is_an_error` | `config.missing-env-var-is-an-error` |
| `test_rejected_configs` | `config.no-servers`, `config.two-servers-are-refused`, `config.url-is-refused`, `config.command-and-url-are-exclusive`, `config.command-must-be-a-string`, `config.args-must-be-strings`, `config.unknown-limit-key` |
| `test_rejected_limits` | `config.*-must-be-positive`, `config.*-must-not-be-negative` |
| `test_rejected_limit_types` | `config.limit-as-*`, `config.timeout-*-is-refused` |
| `test_a_fractional_timeout_is_still_allowed` | `config.fractional-timeout-is-allowed` |
| `test_limits_are_validated_when_built_in_code_too` | Python-only: constructs the `Limits` dataclass directly, an API boundary not a file format. TS: needs an equivalent if its limits object is constructible outside the parser. |
| `test_empty_memory_limit_is_rejected` | `config.empty-memory-limit-is-refused` |
| `test_session_retention_limit_parses_and_is_positive` | `config.session-retention-limit-parses`, `config.max-session-bytes-must-be-positive` |
| `test_session_retention_limit_rejects_zero` | `config.max-session-bytes-must-be-positive` |
| `test_session_retention_must_cover_payload_limit` | `config.session-retention-must-cover-the-payload-limit` |
| `test_session_call_limit_is_positive` | `config.max-session-calls-must-be-positive` |
| `test_rendering_limits_leave_room_for_explicit_truncation_notices` | `config.query-output-must-fit-the-notices`, `config.cell-must-fit-an-ellipsis` |
| `test_load_config_reads_a_file` | `config.minimal-config-parses` |
| `test_load_config_reports_a_missing_file` | `config.missing-file-is-an-error` |
| `test_load_config_reports_bad_toml` | `config.bad-toml-is-an-error` |
| `test_find_config_precedence` | Python-only: `--config`, then $SLUICE_CONFIG, then ./sluice.toml is CLI discovery, not parsing. TS: `typescript/test/cli.test.ts` "config discovery: --config, then SLUICE_CONFIG, then ./sluice.toml". |
| `test_the_example_config_is_valid` | `config.example-config-is-valid` |

## tests/test_conformance.py

| Test | Covered by |
|---|---|
| `test_every_fixture_kind_has_a_python_runner` | The conformance runner itself. |
| `test_extract` | The conformance runner itself. |
| `test_project` | The conformance runner itself. |
| `test_infer` | The conformance runner itself. |
| `test_naming` | The conformance runner itself. |
| `test_scope` | The conformance runner itself. |
| `test_cell` | The conformance runner itself. |
| `test_gate` | The conformance runner itself. |
| `test_gate_ast` | The conformance runner itself. |
| `test_engine` | The conformance runner itself. |
| `test_session` | The conformance runner itself. |
| `test_paginate` | The conformance runner itself. |
| `test_config` | The conformance runner itself. |
| `test_coverage_map_lists_every_python_test` | The conformance runner itself. |

## tests/test_demo_median.py

| Test | Covered by |
|---|---|
| `test_prompt_and_grading_row_count_stay_aligned` | Python-only: the model-eval demo harness, not product behavior. TS: not needed. |
| `test_extract_result_from_stream_json_transcript` | Python-only: the model-eval demo harness, not product behavior. TS: not needed. |

## tests/test_engine_contract.py

| Test | Covered by |
|---|---|
| `test_the_fixture_files_really_are_readable_without_the_lockdown` | Python-only: needs real files on disk. The fixtures distinguish a permission error from a read error instead (`engine.unlocked-glob-is-not-a-permission-error`). TS: covered by those fixtures. |
| `test_lockdown_blocks_reads_that_would_otherwise_succeed` | `engine.lockdown-blocks-read-json`, `engine.lockdown-blocks-read-parquet`, `engine.lockdown-blocks-glob` |
| `test_lockdown_blocks_our_own_read_json` | `engine.lockdown-blocks-read-json` |
| `test_lockdown_blocks_writing_data_out` | `engine.lockdown-blocks-copy-to` |
| `test_lockdown_blocks_attach_and_extensions` | `engine.lockdown-blocks-attach`, `engine.lockdown-blocks-install`, `engine.lockdown-blocks-load` |
| `test_configuration_is_frozen_by_the_lock` | `engine.lock-freezes-configuration`, `engine.threads-is-settable-without-the-lock` |
| `test_engine_lockdown_does_not_block_pragma` | `engine.lockdown-does-not-block-pragma` |
| `test_locked_engine_supports_atomic_retention_ddl_and_envelope_update` | `engine.retention-ddl-commits-as-one-unit` |
| `test_retention_ddl_rolls_back_as_one_unit` | `engine.retention-ddl-rolls-back-as-one-unit` |
| `test_extract_statements_classifies` | Python-only: pins the Python binding's `extract_statements` typing, which differs in Node (review/duckdb2-node-check.md constraint 1). The gate outcomes are fixtures (`gate.statement.*`). TS: needs its own binding contract test. |
| `test_median_is_exactly_equal_inside_the_domain` | `engine.median-bigint-*`, `engine.median-double-*` |
| `test_avg_is_not_exactly_equal_and_needs_a_tolerance` | `engine.avg-double-needs-a-tolerance` |
| `test_relative_tolerance_alone_does_not_survive_cancellation` | `engine.relative-tolerance-alone-does-not-survive-cancellation` |
| `test_median_on_a_json_column_returns_a_lexicographic_answer` | `engine.median-on-json-is-lexicographic`, `engine.sum-on-json-fails`, `engine.avg-on-json-fails` |
| `test_interrupt_reaches_the_executing_connection` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: needs an equivalent; review/duckdb2-node-check.md constraint 4 (libuv pool) makes it a different test. |
| `test_interrupt_on_the_parent_does_not_reach_a_cursor` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: needs an equivalent; review/duckdb2-node-check.md constraint 4 (libuv pool) makes it a different test. |
| `test_round_trips_require_the_modern_protocol_version` | Python-only: pins the MCP Python SDK's result unions. TS: `typescript/test/proxy.test.ts` "the initialize handshake tops out below the round-trip version" and "an input request is refused with the versions named when the upstream client is too old". |
| `test_client_discover_negotiates_the_modern_version` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "discover negotiates the modern version with a modern downstream". |
| `test_output_schema_violation_raises_with_a_classifiable_message` | Python-only: pins the Python SDK's error text that proxy.py classifies. TS: `typescript/test/proxy.test.ts` "the SDK's output-schema error carries a classifiable message". |

## tests/test_gate_fail_closed.py

| Test | Covered by |
|---|---|
| `test_nothing_hides_inside_a_cte_or_nested_query` | `gate.cte.*` |
| `test_legitimate_queries_are_still_admitted` | `gate.legal.*`, `gate.cte.legal-*` |
| `test_an_unknown_ast_shape_is_rejected` | `gate_ast.renamed-cte-body`, `gate_ast.cte-map-without-a-list`, `gate_ast.cte-map-not-an-object`, `gate_ast.malformed-cte-entry`, `gate_ast.unknown-cte-body-type`, `gate_ast.empty-statements`, `gate_ast.no-statements-key`, `gate_ast.unknown-statement-node` |
| `test_a_missing_or_unknown_subtree_is_rejected_not_skipped` | `gate_ast.unknown-from-table-type`, `gate_ast.from-table-not-an-object`, `gate_ast.join-without-left`, `gate_ast.base-table-without-name`, `gate_ast.function-without-name`, `gate_ast.set-operation-without-members`, `gate_ast.recursive-cte-without-right`, `gate_ast.subquery-without-node` |
| `test_expressions_beside_a_cte_body_are_walked` | `gate_ast.expressions-beside-a-cte-body-are-walked` |
| `test_a_qualified_path_cannot_hide_behind_an_empty_schema` | `gate_ast.qualified-path-cannot-hide-behind-an-empty-schema` |
| `test_a_catalog_alone_is_still_qualified` | `gate_ast.catalog-alone-is-still-qualified` |
| `test_the_canary_passes_on_the_installed_duckdb` | Python-only: runs Python's startup canary. Its probes are gate fixtures (`gate.cte.*`, `gate.function.sleep-ms`, `gate.function.pg-get-viewdef-*`, `gate.function.json-serialize-plan`). TS: `verifyParserContract` in `typescript/src/gate.ts` runs the same probes, plus a check of the CTE body path, at `Store.open` (review constraint 3). |
| `test_a_session_opens_with_the_canary_after_lockdown` | Python-only: Store.open plus canary. TS: needs an equivalent. |
| `test_the_canary_refuses_when_a_table_goes_unseen` | Python-only: monkeypatches the Python gate to prove the startup canary refuses to run. TS: needs its own canary and an equivalent test (review/duckdb2-node-check.md constraint 3). |
| `test_the_canary_refuses_a_rejection_for_the_wrong_reason` | Python-only: monkeypatches the Python gate to prove the startup canary refuses to run. TS: needs its own canary and an equivalent test (review/duckdb2-node-check.md constraint 3). |
| `test_the_canary_refuses_when_a_legal_cte_is_refused` | Python-only: monkeypatches the Python gate to prove the startup canary refuses to run. TS: needs its own canary and an equivalent test (review/duckdb2-node-check.md constraint 3). |
| `test_a_store_that_fails_the_canary_does_not_open` | Python-only: monkeypatches the Python gate to prove the startup canary refuses to run. TS: needs its own canary and an equivalent test (review/duckdb2-node-check.md constraint 3). |
| `test_builtin_macros_cannot_reach_what_the_gate_denies` | `gate.function.pg-*`, `gate.function.format-type`, `gate.function.get-block-size`, `gate.function.generate-subscripts`, `gate.function.regexp-split-to-table`, `gate.function.json-serialize-plan*` |
| `test_ordinary_macros_and_aggregates_are_still_admitted` | `gate.function.ordinary-*` |
| `test_the_canary_refuses_when_macros_are_not_denied` | Python-only: monkeypatches the Python gate to prove the startup canary refuses to run. TS: needs its own canary and an equivalent test (review/duckdb2-node-check.md constraint 3). |
| `test_the_canary_refuses_when_scalar_denials_go_unseen` | Python-only: monkeypatches the Python gate to prove the startup canary refuses to run. TS: needs its own canary and an equivalent test (review/duckdb2-node-check.md constraint 3). |
| `test_a_quoted_call_in_a_macro_definition_is_followed` | `gate.function.user-macro-reaching-a-denied-function` |
| `test_the_canary_refuses_when_plan_serialization_is_allowed` | Python-only: monkeypatches the Python gate to prove the startup canary refuses to run. TS: needs its own canary and an equivalent test (review/duckdb2-node-check.md constraint 3). |

## tests/test_infer.py

| Test | Covered by |
|---|---|
| `test_inference_table` | `infer.*` |
| `test_mixed_scalars_become_varchar_never_json` | `infer.many-ints-and-one-string-is-varchar-never-json` |
| `test_timestamp_shaped_strings_stay_varchar` | `infer.iso-timestamp-with-offset-stays-varchar`, `infer.iso-date-stays-varchar`, `infer.version-string-stays-varchar`, `infer.protocol-date-stays-varchar` |
| `test_integers_past_the_exact_float_range_are_marked_inexact` | `infer.mixed-int-past-2-53-and-float`, `infer.integers-past-2-53-are-inexact` |
| `test_large_integer_columns_are_inexact_for_median` | `infer.large-integers-inexact-for-median` |
| `test_large_finite_float_columns_are_inexact_for_tolerance_aggregates` | `infer.large-finite-floats-are-inexact` |
| `test_mixed_numeric_columns_are_never_claimed_exact` | `infer.mixed-2-53-and-float` |
| `test_non_finite_floats_are_marked_inexact` | `infer.non-finite-*` (one-sided, Python: non-standard JSON literals) |
| `test_booleans_are_not_treated_as_integers` | `infer.booleans-are-not-integers`, `infer.boolean-and-integer-is-varchar` |
| `test_coerce` | `session.handle.mixed-scalars-are-stored-as-text`, `session.handle.json-columns-store-the-value` |

## tests/test_intercept.py

| Test | Covered by |
|---|---|
| `test_payload_is_replaced_by_a_handle` | `session.handle.rows-are-replaced-by-a-handle` |
| `test_structured_content_mirrors_the_handle` | `session.handle.*` |
| `test_structured_channel_wins_over_prose` | `session.handle.structured-channel-wins-over-prose` |
| `test_channel_disagreement_is_surfaced` | `session.handle.channel-disagreement-is-surfaced` |
| `test_non_json_text_is_recorded_without_a_table` | `session.handle.non-json-text-is-envelope-only` |
| `test_small_payloads_are_previewed_in_full` | `session.handle.small-payload-is-previewed-in-full` |
| `test_large_payloads_are_truncated_and_say_so` | `session.handle.rows-are-replaced-by-a-handle` |
| `test_errors_pass_through_and_are_still_recorded` | `session.passthrough.errors-are-returned-verbatim-and-recorded` |
| `test_image_results_pass_through_and_are_still_recorded` | `session.passthrough.images-are-returned-verbatim-and-bytes-not-stored` |
| `test_image_bytes_are_not_stored` | `session.passthrough.images-are-returned-verbatim-and-bytes-not-stored` |
| `test_oversize_payloads_pass_through_unparsed` | `session.passthrough.oversize-payloads-pass-through-with-a-note` |
| `test_dual_channels_share_one_payload_ceiling` | `session.passthrough.dual-channels-share-one-ceiling` |
| `test_non_text_metadata_path_does_not_serialize_the_result` | Python-only: fault injection by monkeypatching Python internals. TS: needs an equivalent unit test of its own internals. (It proves a 1 MB image is never serialized, an allocation property.) |
| `test_no_query_hint_until_the_query_tool_exists` | Python-only: the `query_available` flag was a staging step from plan M2-M4; every session in the suite mounts `query`. TS: not needed unless it stages the same way. |
| `test_envelope_write_failure_does_not_fail_the_call` | Python-only: fault injection by monkeypatching Python internals. TS: needs an equivalent unit test of its own internals. |
| `test_full_loop_returns_a_handle_not_a_payload` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "the full loop returns a handle, not a payload". |
| `test_rows_are_materialized_with_real_types` | `session.handle.rows-are-replaced-by-a-handle` |
| `test_aggregates_match_the_source_data` | `session.aggregates.median-a-model-would-get-wrong` |
| `test_every_candidate_array_becomes_its_own_table` | `session.handle.every-candidate-array-becomes-a-table` |
| `test_mixed_elements_produce_no_table` | `session.handle.mixed-elements-produce-no-table` |
| `test_empty_result_says_so` | `session.handle.empty-result-says-so` |
| `test_nested_values_become_json_columns` | `session.handle.nested-values-become-json-columns` |
| `test_json_columns_are_queryable` | `session.handle.nested-values-become-json-columns` |
| `test_wide_payloads_are_capped_with_an_extra_column` | `session.handle.wide-payloads-spill-to-extra` |
| `test_edge_numbers_are_flagged_inexact` | `session.handle.edge-numbers-are-flagged-inexact`, `session.handle.non-finite-float-literals` (one-sided, Python) |
| `test_scalar_arrays_get_a_value_column` | `session.handle.scalar-arrays-get-a-value-column` |
| `test_colliding_tool_names_get_separate_tables` | `session.handle.colliding-tool-names-get-separate-tables` |
| `test_flat_table_rows_are_traceable_to_the_call` | `session.handle.rows-are-traceable-to-the-call` |
| `test_downstream_structured_content_is_preserved_on_the_envelope` | `session.handle.structured-channel-wins-over-prose` |
| `test_no_orphan_tables_when_materialization_fails` | Python-only: fault injection by monkeypatching Python internals. TS: needs an equivalent unit test of its own internals. (Atomicity under an injected insert failure.) |

## tests/test_memory_bounds.py

| Test | Covered by |
|---|---|
| `test_admission_covers_selection_and_commit` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: needs an equivalent; review/duckdb2-node-check.md constraint 4 (libuv pool) makes it a different test. |
| `test_selector_exception_passes_through_and_releases_fifo_ticket` | Python-only: fault injection by monkeypatching Python internals. TS: needs an equivalent unit test of its own internals. |
| `test_cancelled_planning_releases_fifo_ticket` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: needs an equivalent; review/duckdb2-node-check.md constraint 4 (libuv pool) makes it a different test. |
| `test_degraded_commit_retry_resolves_ticket_and_reason` | Python-only: fault injection by monkeypatching Python internals. TS: needs an equivalent unit test of its own internals. |
| `test_overbudget_passthrough_metadata_is_bounded` | `session.passthrough.metadata-is-bounded` |
| `test_row_preview_never_switches_to_a_later_table` | `session.handle.preview-never-switches-to-a-later-table` |
| `test_dual_channel_retention_accounts_for_both_channels` | Python-only: asserts the internal retained-bytes counter, whose size estimate is implementation-defined (spec 001 §7). TS: needs an equivalent over its own counter. |
| `test_retention_charge_covers_each_stored_envelope_representation` | Python-only: as above, an internal counter. TS: needs an equivalent. |
| `test_sequential_retention_evicts_oldest_call_coherently` | `session.retention.oldest-call-is-evicted-coherently`, `session.retention.evicted-names-stay-rejected-after-new-calls` |
| `test_out_of_order_planning_still_commits_in_arrival_order` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: needs an equivalent; review/duckdb2-node-check.md constraint 4 (libuv pool) makes it a different test. |
| `test_preview_uses_bounded_raw_rows_not_storage_columns` | `session.handle.preview-uses-raw-rows-not-storage-columns` |
| `test_call_larger_than_retention_budget_degrades_safely` | `session.retention.call-larger-than-the-budget-degrades-safely` |
| `test_over_budget_call_does_not_evict_healthy_retained_calls` | `session.retention.over-budget-call-does-not-evict-healthy-calls` |
| `test_session_call_cap_deletes_old_unique_scopes` | `session.retention.call-cap-deletes-old-scopes` |
| `test_session_call_cap_keeps_shared_scope_view_until_last_row` | `session.retention.call-cap-keeps-a-shared-scope-view` |
| `test_evicted_table_diagnostics_are_bounded` | Python-only: the size of the internal evicted-name cache (64 per call slot) is an implementation bound. TS: needs an equivalent bound. |

## tests/test_naming.py

| Test | Covered by |
|---|---|
| `test_hyphen_and_underscore_tools_do_not_collide` | `naming.hyphen-and-underscore-do-not-collide` |
| `test_case_differing_tools_do_not_collide` | `naming.case-differing-tools-do-not-collide` |
| `test_server_and_tool_boundary_is_unambiguous` | `naming.server-tool-boundary-is-unambiguous` |
| `test_mounted_name_is_stable` | `naming.mounted-name-vector` |
| `test_mounted_name_shape` | `naming.mounted-name-hashes-the-unsanitized-name` |
| `test_long_names_fail_loudly_rather_than_truncating` | `naming.mounted-name-stays-under-the-tool-name-limit` |
| `test_name_too_long_raises` | Python-only: lowers MAX_MOUNTED_NAME by monkeypatch; with SLUG_MAX 40 and a 12-char tag no real name reaches 128, so no fixture can trigger it. TS: same guard, unit-tested with a lowered limit in typescript/test/naming.test.ts. |
| `test_table_name_carries_scope_and_sequence` | `naming.table-name-carries-scope-and-sequence` |
| `test_table_names_from_different_scopes_differ` | `naming.table-names-from-different-scopes-differ` |
| `test_sequence_widens_past_9999` | `naming.table-sequence-widens-past-9999` |
| `test_quote_ident` | `naming.quote-*` |
| `test_the_discovered_collision_pair_no_longer_collides` | `naming.the-discovered-24-bit-collision-no-longer-collides` |
| `test_injectivity_check_accepts_distinct_names` | `naming.injectivity-check-accepts-distinct-names` |
| `test_injectivity_check_fails_loudly_on_a_forced_collision` | Python-only: forces a hash collision by monkeypatching `tag`; no real colliding pair is known at 48 bits. TS: covered by a unit test with an injected mount function in typescript/test/naming.test.ts. |
| `test_repeated_identical_pairs_are_not_a_collision` | `naming.repeated-identical-pairs-are-not-a-collision` |

## tests/test_pagination.py

| Test | Covered by |
|---|---|
| `test_pagination_config_defaults` | `config.pagination-defaults` |
| `test_pagination_config_rejects` | `config.pagination-rejects-*` |
| `test_pagination_must_be_a_table` | `config.pagination-must-be-tables` |
| `test_startup_refuses_unknown_tool` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/pagination.test.ts` "startup refuses a tool the downstream did not list". |
| `test_startup_refuses_tool_without_read_only_annotation` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/pagination.test.ts` "startup refuses a tool without a read-only annotation". |
| `test_collection_is_listed_next_to_the_original` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/pagination.test.ts` "the collection is listed next to the original". |
| `test_complete_fetch_lands_every_row_in_one_table` | `paginate.forwards-filters-and-managed-arguments` (loop and `collection` result) |
| `test_a_single_page_is_still_a_collection` | `paginate.a-single-page-is-complete` (loop and `collection` result) |
| `test_resume_from_an_offset` | `paginate.resume-from-an-offset` |
| `test_envelope_records_the_collection_call` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/pagination.test.ts` "the envelope records the collection call". |
| `test_max_pages_yields_a_partial_result_with_a_resume_offset` | `paginate.max-pages-is-partial-with-a-resume-offset` (loop and `collection` result) |
| `test_max_bytes_never_keeps_the_page_that_would_cross_it` | `paginate.max-bytes-never-keeps-the-crossing-page`, `paginate.max-bytes-exact-fit-is-kept` |
| `test_a_first_page_over_max_bytes_is_an_error_with_status` | `paginate.first-page-over-max-bytes-yields-nothing` (loop and `collection` result) |
| `test_a_page_without_has_more_is_malformed_not_final` | `paginate.page-without-has-more-is-malformed-not-final` (loop and `collection` result) |
| `test_max_seconds_stops_the_fetch_and_leaves_the_proxy_usable` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: `typescript/test/pagination.test.ts` "max_seconds stops the fetch and leaves the proxy usable". |
| `test_non_advancing_offset_is_a_partial_result` | `paginate.next-offset-behind-is-not-advancing` |
| `test_missing_next_offset_is_a_partial_result` | `paginate.missing-next-offset` |
| `test_a_failed_later_page_keeps_the_pages_before_it` | `paginate.error-on-a-later-page-keeps-earlier-pages` (loop and `collection` result) |
| `test_a_failed_first_page_is_an_error_not_an_empty_table` | `paginate.error-on-the-first-page-is-kept-for-the-caller` (loop and `collection` result) |
| `test_malformed_page_is_a_partial_result` | `paginate.items-not-an-array-is-malformed` (loop and `collection` result) |
| `test_interactive_page_stops_the_fetch` | `paginate.interactive-page-stops-the-fetch` |
| `test_agent_supplied_limit_is_rejected` | `paginate.agent-supplied-limit-is-rejected` |
| `test_original_tool_still_returns_one_page` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/pagination.test.ts` "the original tool still returns one page". |
| `test_oversize_passthrough_still_carries_the_pagination_status` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/pagination.test.ts` "an oversize passthrough still carries the pagination status". |
| `test_collection_result_turns_a_raw_passthrough_into_an_error_with_status` | Python-only: unit test of `server.collection_result` for a collection whose combined payload could not be materialized, which no scripted page sequence reaches without fault injection. The zero-page error path is fixtured through the collection tool (`paginate.*` with `collection`). TS: `typescript/test/pagination.test.ts` "collectionResult turns a raw passthrough into an error with status". |
| `test_collection_result_keeps_a_recorded_handle` | `paginate.forwards-filters-and-managed-arguments`, `paginate.max-pages-is-partial-with-a-resume-offset` (collection handle carries the pagination status) |
| `test_concurrent_collections_respect_admission` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: `typescript/test/pagination.test.ts` "concurrent collections respect admission". |
| `test_loop_forwards_filters_and_managed_arguments` | `paginate.forwards-filters-and-managed-arguments` |
| `test_loop_reports_a_transport_failure_as_partial` | `paginate.transport-failure-is-partial` |
| `test_loop_treats_a_repeated_offset_as_not_advancing` | `paginate.repeated-offset-is-not-advancing` |
| `test_loop_treats_an_empty_page_with_has_more_as_not_advancing` | `paginate.empty-page-with-has-more-is-not-advancing` |
| `test_loop_requires_an_explicit_has_more` | `paginate.page-without-has-more-is-malformed-not-final` |
| `test_loop_rejects_bad_resume_offsets` | `paginate.negative-offset-is-rejected`, `paginate.boolean-offset-is-rejected`, `paginate.string-offset-is-rejected` |
| `test_collection_names_are_distinct_from_mounted_names` | `naming.collection-differs-from-mounted`, `naming.collection-differs-from-dotted-tool` |

## tests/test_passthrough.py

| Test | Covered by |
|---|---|
| `test_result_matches_a_direct_call` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "a result through the proxy matches a direct call". |
| `test_result_metadata_and_annotations_survive_the_hop` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "result metadata and content annotations survive the hop". |
| `test_errors_are_forwarded_verbatim` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "errors are forwarded verbatim". |
| `test_image_content_is_not_materialized` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "image content is not materialized". |
| `test_full_loop_through_the_sluice_server` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "the full loop through the Sluice server". |
| `test_error_metadata_survives_the_whole_product_path` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "error metadata survives the whole product path". |
| `test_image_metadata_survives_the_whole_product_path` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "image metadata survives the whole product path". |
| `test_rich_text_metadata_survives_the_whole_product_path` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "non-JSON rich text is recorded as an envelope-only handle". |
| `test_nonforwardable_failures_are_recorded_with_their_class` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "non-forwardable failures are recorded with their class". |

## tests/test_property_aggregates.py

| Test | Covered by |
|---|---|
| `test_aggregates_match_the_normalized_source` | Python-only: Hypothesis property over generated rows. Fixed samples are `session.aggregates.missing-and-null-are-both-sql-null` and `session.aggregates.row-boundary-*`. TS: needs an equivalent property test (fast-check or similar). |
| `test_float_tolerances_on_bounded_dyadic_values` | Python-only: Hypothesis property. Fixed sample: `session.aggregates.bounded-dyadic-floats-within-tolerance`. TS: needs an equivalent property test. |
| `test_out_of_domain_cases_are_explicitly_inexact` | `session.aggregates.large-bigint-median-loses-units`, `session.aggregates.double-median-near-overflow`, `session.aggregates.double-avg-cancellation`, `session.aggregates.mixed-int-and-float-max` |
| `test_float_cancellation_is_a_bounded_regression_not_a_guarantee` | `session.aggregates.float-cancellation-exceeds-the-tolerances` |
| `test_advertised_row_boundaries_are_materialized` | `session.aggregates.row-boundary-1`, `session.aggregates.row-boundary-500` |

## tests/test_proxy.py

| Test | Covered by |
|---|---|
| `test_page_two_tool_is_mounted` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "a tool reachable only on page two is mounted". |
| `test_sdk_list_tools_really_does_return_one_page` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "the SDK's per-page tools/list returns one page". |
| `test_colliding_tool_names_are_both_mounted` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "colliding tool names are both mounted". |
| `test_annotations_survive_cloning` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "annotations, title and _meta survive cloning". |
| `test_output_schema_is_removed` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "the output schema is removed". |
| `test_description_keeps_upstream_text_and_appends_the_note` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "the description keeps the upstream text and appends the note". |
| `test_input_schema_is_passed_through` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "the input schema is passed through". |
| `test_call_forwards_and_returns_the_payload` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "a call is forwarded and returns the payload". |
| `test_unknown_tool_is_a_protocol_failure` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "an unknown tool is a protocol failure". |
| `test_output_schema_violation_is_classified` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "an output-schema violation is classified". |
| `test_transport_failure_marks_the_session_unhealthy` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "a transport failure marks the session unhealthy". |
| `test_round_trips_are_relayed_not_answered` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "round trips are relayed, not answered". |
| `test_sluice_advertises_no_sampling_or_elicitation` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "Sluice advertises no sampling or elicitation of its own". |
| `test_startup_fails_loudly_on_a_mounted_name_collision` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "startup fails loudly on a mounted-name collision". |

## tests/test_query_limits.py

| Test | Covered by |
|---|---|
| `test_cell_escaping` | `cell.*` |
| `test_null_and_the_string_null_are_deliberately_ambiguous` | `cell.null`, `cell.the-string-null`, `cell.empty-string` |
| `test_cell_truncation_cuts_on_a_character_boundary` | `cell.truncation-cuts-on-a-character-boundary` |
| `test_render_reports_every_kind_of_truncation` | `session.query.every-kind-of-truncation-is-reported`, `session.query.long-alias-omits-the-table-and-says-so` |
| `test_render_never_claims_a_row_count_it_cannot_know` | `session.query.row-cap-is-applied-and-reported`, `session.query.every-kind-of-truncation-is-reported` |
| `test_row_cap_is_applied_and_reported` | `session.query.row-cap-is-applied-and-reported` |
| `test_no_more_rows_notice_when_everything_fits` | `session.query.no-more-rows-notice-when-everything-fits`, `session.query.exactly-max-rows-is-not-more` |
| `test_max_rows_cannot_exceed_the_configured_cap` | `session.query.max-rows-cannot-exceed-the-configured-cap` |
| `test_max_rows_below_one_is_rejected` | `session.query.bad-arguments-are-errors-6` |
| `test_output_never_exceeds_the_byte_cap` | `session.query.output-never-exceeds-the-byte-cap-*` |
| `test_the_truncation_notice_always_survives` | `session.query.truncation-notice-survives-multibyte-data` |
| `test_byte_cap_counts_bytes_not_characters` | `session.query.byte-cap-counts-bytes-not-characters` |
| `test_a_long_column_alias_cannot_blow_the_cap` | `session.query.long-alias-omits-the-table-and-says-so` |
| `test_a_wide_header_cannot_evict_the_truncation_notices` | `session.query.wide-header-cannot-evict-the-notices` |
| `test_headers_are_escaped_like_cells` | `session.query.headers-are-escaped-like-cells` |
| `test_byte_cap_drops_rows_and_says_so` | `session.query.byte-cap-drops-rows-and-says-so` |
| `test_sql_runs_unmodified_with_a_trailing_semicolon` | `session.query.trailing-semicolon-runs-unmodified` |
| `test_sql_runs_unmodified_with_duplicate_column_names` | `session.query.duplicate-column-names-run-unmodified` |
| `test_a_failing_query_is_an_error_not_an_empty_result` | `session.query.failing-query-is-an-error-not-an-empty-result` |
| `test_aggregating_a_json_column_fails_loudly` | `session.handle.nested-values-become-json-columns` |
| `test_a_long_query_is_interrupted_at_the_timeout` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: needs an equivalent; review/duckdb2-node-check.md constraint 4 (libuv pool) makes it a different test. |
| `test_cancelling_the_caller_still_bounds_the_query` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: needs an equivalent; review/duckdb2-node-check.md constraint 4 (libuv pool) makes it a different test. |
| `test_a_timeout_leaves_the_store_usable` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: needs an equivalent; review/duckdb2-node-check.md constraint 4 (libuv pool) makes it a different test. |
| `test_timeout_expiring_in_worker_queue_prevents_late_execution` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: needs an equivalent; review/duckdb2-node-check.md constraint 4 (libuv pool) makes it a different test. |
| `test_a_vanished_allowed_table_does_not_name_its_nearest_neighbour` | Python-only: reads internals (seeds the allowlist with a table that is not in the catalog, standing in for an eviction race). TS: needs an equivalent unit test. |
| `test_engine_errors_never_carry_did_you_mean_suggestions` | Python-only: engine error text is binding-specific. TS: needs an equivalent unit test. |
| `test_the_lockdown_disables_spilling_to_disk` | Python-only: reads a connection setting through the Python binding. TS: needs an equivalent unit test. |

## tests/test_query_safety.py

| Test | Covered by |
|---|---|
| `test_non_select_statements_are_rejected` | `gate.statement.*-is-rejected`, `gate_ast.statement-layer-refuses-*` |
| `test_multiple_statements_are_rejected_by_count` | `gate.statement.two-statements-are-counted-not-split`, `gate_ast.statement-layer-counts-statements` |
| `test_a_semicolon_inside_a_string_is_not_two_statements` | `gate.statement.semicolon-inside-a-string-is-one-statement` |
| `test_enumeration_and_filesystem_vectors_are_rejected` | `gate.object.*`, `gate.function.unnest-range`, `gate.function.write-log`, `gate.function.lpad`, `gate.function.list-resize`, `gate.function.bitstring` |
| `test_show_and_describe_are_rejected` | `gate.statement.show-tables-is-rejected`, `gate.statement.show-all-tables-is-rejected`, `gate.statement.describe-mine-is-rejected`, `gate.statement.summarize-mine-is-rejected` |
| `test_pragmas_are_rejected` | `gate.statement.pragma-*` |
| `test_the_statement_gate_alone_would_pass_these` | Python-only: pins how the Python binding types SHOW, DESCRIBE and PRAGMA, which differs in Node (review constraint 1). The refusals are `gate.statement.*`. TS: needs its own binding contract test. |
| `test_an_unknown_table_names_no_others` | `gate.object.unknown-table-names-no-others` |
| `test_legal_queries_pass` | `gate.statement.legal-single-select-*`, `gate.legal.*`, `gate.cte.legal-1`, `gate.cte.legal-2`, `gate.function.ordinary-1` |
| `test_cte_binding_order_cannot_shadow_physical_tables` | `gate.cte.self-reference-is-not-a-shadow`, `gate.cte.earlier-cte-cannot-see-a-later-one`, `gate.cte.recursive-seed-cannot-use-its-own-name` |
| `test_ordered_ctes_and_recursive_terms_remain_legal` | `gate.cte.legal-*` |
| `test_unparseable_sql_is_rejected_not_executed` | `gate.statement.unparseable-sql-is-rejected` |
| `test_empty_sql_is_rejected` | `gate.statement.blank-sql-is-rejected`, `gate.statement.empty-sql-is-rejected`, `gate.statement.comment-only-sql-is-rejected` |

## tests/test_query_tool.py

| Test | Covered by |
|---|---|
| `test_query_is_mounted_and_marked_read_only` | Python-only: drives the MCP Python SDK end to end (stdio transport, `Client`, server wiring). TS: `typescript/test/proxy.test.ts` "the query tool is mounted and marked read-only". |
| `test_the_handle_points_at_a_table_the_query_can_reach` | `session.handle.rows-are-replaced-by-a-handle` |
| `test_the_median_a_model_would_get_wrong` | `session.aggregates.median-a-model-would-get-wrong` |
| `test_group_by_matches_the_source` | `session.aggregates.median-a-model-would-get-wrong` |
| `test_the_envelope_view_reaches_the_untouched_payload` | `session.query.envelope-view-reaches-the-payload`, `session.handle.structured-channel-wins-over-prose` |
| `test_the_physical_envelope_is_not_reachable` | `session.query.physical-envelope-is-not-reachable` |
| `test_another_scopes_tables_are_not_reachable_by_name` | `session.query.another-scopes-view-shows-only-its-own-calls` |
| `test_rejections_are_errors_not_empty_successes` | `session.query.rejection-is-an-error-*` |
| `test_bad_arguments_are_errors` | `session.query.bad-arguments-are-errors-1`, `session.query.bad-arguments-are-errors-2`, `session.query.bad-arguments-are-errors-3`, `session.query.bad-arguments-are-errors-4` |
| `test_max_rows_must_be_an_integer` | `session.query.bad-arguments-are-errors-5`, `session.query.bad-arguments-are-errors-7` |
| `test_json_columns_are_usable_from_the_handle` | `session.handle.nested-values-become-json-columns` |
| `test_a_large_result_never_returns_the_whole_payload` | `session.query.large-result-never-returns-the-whole-payload` |
| `test_a_nested_cte_cannot_shadow_its_way_to_the_envelope` | `session.query.nested-cte-cannot-shadow-its-way-to-the-envelope`, `gate.cte.cte-in-exists-does-not-whitelist-outer` |
| `test_shadowing_variants_are_all_rejected` | `gate.cte.cte-in-exists-does-not-whitelist-sqlite-master`, `gate.cte.cte-in-exists-does-not-whitelist-a-table-function`, `gate.cte.self-reference-is-not-a-shadow`, `gate.cte.earlier-cte-cannot-see-a-later-one`, `gate.cte.sibling-subquery-cte-does-not-leak` |
| `test_legitimate_ctes_still_work_through_the_product_path` | `session.query.legitimate-cte-through-the-product-path` |
| `test_recursive_cte_still_works_through_the_product_path` | `session.query.recursive-cte-through-the-product-path` |

## tests/test_scope.py

| Test | Covered by |
|---|---|
| `test_minted_scopes_are_distinct_and_full_width` | `scope.minted-scopes-are-full-width-and-distinct` |
| `test_minted_scopes_carry_capability_strength_entropy` | `scope.minted-scopes-are-full-width-and-distinct` |
| `test_minting_draws_from_secrets_not_random` | Python-only: proves the generator is `secrets` by monkeypatching it. TS: typescript/test/scope.test.ts proves minting calls `crypto.getRandomValues`. |
| `test_conversation_id_gives_a_stable_scope` | `scope.conversation-id-vector` |
| `test_conversation_scope_matches_a_fixed_digest_vector` | `scope.conversation-id-vector`, `scope.conversation-id-non-ascii` |
| `test_conversation_scope_is_stable_across_processes` | `scope.conversation-id-vector` |
| `test_derive_prefers_a_client_supplied_conversation_id` | `scope.derive-prefers-conversation-id`, `scope.derive-key-order` |
| `test_derive_mints_when_the_client_supplies_nothing` | `scope.derive-mints-without-meta`, `scope.derive-mints-for-empty-meta` |
| `test_derive_ignores_non_string_and_empty_values` | `scope.derive-ignores-empty-string`, `scope.derive-ignores-non-string` |
| `test_a_stale_handle_cannot_name_a_live_table` | `naming.table-names-from-different-scopes-differ` |

## tests/test_shape.py

| Test | Covered by |
|---|---|
| `test_bare_list_of_objects` | `extract.bare-list-of-objects` |
| `test_bare_list_of_scalars_gets_a_value_column` | `extract.bare-list-of-scalars-gets-a-value-column` |
| `test_every_candidate_array_is_materialized` | `extract.every-candidate-array-is-materialized` |
| `test_mixed_elements_yield_no_table` | `extract.mixed-top-level-list-yields-no-table`, `extract.mixed-nested-list-yields-no-table` |
| `test_empty_list_is_reported_not_silently_dropped` | `extract.empty-nested-list-is-reported` |
| `test_object_with_no_candidate_arrays_becomes_one_row` | `extract.object-without-candidates-is-one-row` |
| `test_scalars_yield_no_table` | `extract.scalar-*` |
| `test_projection_adds_a_row_ordinal` | `project.adds-a-row-ordinal` |
| `test_missing_keys_become_null` | `project.missing-keys-become-null` |
| `test_missing_key_and_json_null_are_indistinguishable` | `project.missing-key-and-json-null-are-indistinguishable` |
| `test_reserved_column_collisions_are_renamed` | `project.reserved-column-collision-is-renamed` |
| `test_repeated_reserved_collisions_keep_allocating` | `project.repeated-reserved-collisions-keep-allocating`, `project.reserved-rename-skips-a-taken-name` |
| `test_column_cap_keeps_the_most_present_and_spills_the_rest` | `project.column-cap-keeps-the-most-present` |
| `test_column_cap_ties_break_deterministically` | `project.column-cap-ties-break-by-first-appearance`, `session.handle.wide-payloads-spill-to-extra` |

## tests/test_store.py

| Test | Covered by |
|---|---|
| `test_lockdown_closes_external_access` | `engine.lockdown-blocks-read-csv` |
| `test_lockdown_freezes_configuration` | `engine.lock-freezes-configuration` |
| `test_sluice_can_still_create_tables_after_the_lockdown` | `engine.create-and-insert-still-work-after-the-lockdown` |
| `test_every_envelope_column_round_trips` | Python-only: reads every column of the physical envelope through the store's own connection, including timestamps and wire_bytes whose values are run-dependent. The agent-visible columns are covered through scope views in `session.handle.*` and `session.passthrough.*`. TS: needs an equivalent over its own store. |
| `test_flat_table_lineage_is_stored` | `session.handle.rows-are-traceable-to-the-call` |
| `test_row_ordinals_follow_source_order` | `session.handle.rows-are-traceable-to-the-call`, `session.query.values-are-escaped` |
| `test_a_failing_table_leaves_nothing_behind` | Python-only: fault injection by monkeypatching Python internals. TS: needs an equivalent unit test of its own internals. (Commits a plan that cannot be coerced, which no valid tool result produces.) |
| `test_a_failing_second_table_rolls_back_the_first` | Python-only: fault injection by monkeypatching Python internals. TS: needs an equivalent unit test of its own internals. |
| `test_a_failing_envelope_write_rolls_back_the_tables` | Python-only: fault injection by monkeypatching Python internals. TS: needs an equivalent unit test of its own internals. |
| `test_a_successful_commit_leaves_both` | `session.handle.rows-are-traceable-to-the-call` |
| `test_the_connection_stays_usable_after_a_rollback` | Python-only: fault injection by monkeypatching Python internals. TS: needs an equivalent unit test of its own internals. |
| `test_cancelled_direct_waiter_releases_its_fifo_ticket` | Python-only: timing-sensitive (threads, deadlines, cancellation). TS: needs an equivalent; review/duckdb2-node-check.md constraint 4 (libuv pool) makes it a different test. |
| `test_sequences_are_monotonic_per_tool_and_independent` | `session.handle.call-sequence-counts-per-tool`, `session.handle.table-sequence-counts-tables-per-tool` |

## Fixtures with no prior Python test

- `config.byte-order-mark-is-refused`
- `config.env-values-are-stringified-like-python`
- `config.limit-cross-check-above-2-53-is-exact`
- `config.limits-above-2-53-are-accepted`
- `config.pagination-max-bytes-above-2-53-is-compared-exactly`
- `config.preview-bytes-may-be-zero`
- `config.preview-rows-may-be-zero`
- `config.query-output-floor-is-allowed`
- `engine.integer-sum-is-exact-hugeint`
- `engine.lockdown-blocks-read-text`
- `engine.max-of-mixed-double-rounds`
- `engine.median-of-large-bigints-loses-units`
- `engine.median-on-varchar-is-lexicographic-too`
- `engine.sum-on-varchar-fails`
- `extract.empty-bare-list-is-reported`
- `extract.envelope-keeps-only-object-arrays`
- `extract.nine-objects-and-a-scalar`
- `extract.object-with-scalar-list-is-one-row`
- `gate.function.array-resize`
- `gate.function.format`
- `gate.function.harmless-user-macro-is-allowed`
- `gate.function.nextval`
- `gate.function.printf`
- `gate.function.rpad`
- `gate.function.setseed`
- `gate.function.user-macro-running-a-query`
- `gate.function.user-table-macro`
- `gate_ast.statement-layer-admits-a-select`
- `gate_ast.unknown-scalar-field-is-tolerated`
- `naming.collection-name-vector`
- `naming.slug-is-bounded-to-40`
- `naming.slug-lowercases-and-collapses`
- `naming.slug-of-non-ascii-is-x`
- `naming.slug-of-symbols-is-x`
- `naming.slug-strips-edges`
- `naming.tag-of-empty`
- `naming.tag-vector`
- `project.column-cap-restores-payload-order`
- `project.column-names-are-source-keys-verbatim`
- `project.every-reserved-name-is-renamed`
- `project.later-rows-introduce-later-columns`
- `project.nested-values-stay-whole`
- `scope.derive-reads-session-id`
- `scope.derive-snake-case-thread-id`
- `session.aggregates.median-odd-row-count`
- `session.query.cells-are-truncated-and-reported`
- `session.query.default-max-rows-is-the-configured-cap`
