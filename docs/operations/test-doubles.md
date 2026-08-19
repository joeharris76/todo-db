# Test doubles: what each one cannot catch

A double written to satisfy the code under test agrees with that code by
construction. This page records, for each double in `tests/`, what would still
pass if the implementation were subtly wrong, so the next reader does not have
to re-derive it.

It exists because two defects in one helper let twelve provider tests pass
against a resolver that could not drive a real secret store: `_provider_script`
wrote the two-character sequence `\n` instead of a line break, collapsing every
stub to one malformed shell line, and every stub accepted whatever arguments it
was handed. The suite was green throughout and an external reviewer found it.

## The question to ask

Not "does this test pass?" but **"what would still pass if the implementation
were wrong?"** Where the answer is "something that matters", make the double
strict. Where it is "nothing that matters", record why below.

## `FakeLibsql` / `FakeRawConnection` — `tests/test_hosted_backend.py`

Stands in for the `libsql` module.

- **Now strict about**: keyword arguments to `connect`. It rejects anything
  outside `_LIBSQL_CONNECT_KEYWORDS`, so a misspelled or invented keyword fails
  the test instead of being silently accepted the way `**kwargs` used to accept
  it. Real libsql would have raised; the double did not.
- **Still cannot catch**: SQL dialect differences, server-side constraint or
  concurrency behaviour, network and TLS failure shapes, or whether the real
  client applies `isolation_level=None` the way SQLite does. It executes against
  local SQLite, so anything that is true of SQLite but not of libSQL passes.
- **Covered instead by**: `scripts/turso_acceptance.sh` against a real database.

## `FakeHranaModule` — `tests/fake_hrana.py`

Stands in for the embedded-replica client.

- **Now strict about**: the same keyword set, for the same reason.
- **Still cannot catch**: real replication lag, `sync()` semantics, or the
  divergence between replica reads and primary writes that the latency harness
  measures. It models the calls, not the distributed behaviour.
- **Covered instead by**: `tests/test_hosted_latency.py` measurement arms and
  the opt-in acceptance harness.

## `_provider_script` — `tests/test_hosted_backend.py`

Writes an executable stub standing in for an operator's secret store.

- **Now strict about**: line breaks, which are real newlines rather than escaped
  sequences, and argument arity in
  `test_provider_argv_is_passed_through_untouched`, whose stub exits 44 on an
  unexpected positional exactly as `security` does.
- **Still cannot catch**: interactive unlock prompts, biometric gates, keychain
  ACL behaviour, and the real timeout headroom a cold store needs. Every stub
  returns immediately; no stub ever asks the operator for anything.
- **Covered instead by**: `scripts/hosted_auth_acceptance.sh` against a real
  store and database. This is the gap that matters most, because the provider
  feature exists precisely to interact with tools none of these stubs are.

## Accepted looseness

- **`types.ModuleType("libsql")` with no attributes**, used in
  `tests/test_doctor.py` to prove the missing-credential path never reaches the
  client. Deliberate: the test asserts resolution fails before any connect, so a
  fuller double would add nothing.
- **`FakeRawConnection` executing against local SQLite.** Deliberate: the tracker
  logic under test is dialect-independent, and pinning it to a real backend would
  make the fast suite depend on the network.

## Rule for new doubles

1. Reject unexpected arguments, keywords, and call shapes rather than tolerating
   them.
2. Build multi-line fixtures from real newlines and assert the fixture behaves as
   intended before relying on it.
3. When a double stands in for an external tool, write down which real tool it
   imitates and where that tool is actually exercised.
