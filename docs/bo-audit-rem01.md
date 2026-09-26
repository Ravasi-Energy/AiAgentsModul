# BOAgents AUDIT-REM-01

The service credential authenticates an operator. Delegated UI users additionally
require `BACKEND_PROXY_SECRET`, configured identically on API and Next.js and
separately from `BACKEND_SHARED_SECRET`. Incoming caller headers are stripped.
Existing deployments must provision the new server-only secret before enabling
user traffic. No credentials belong in BO settings or the database.

Every effect checks the local mandate chain and each Guardian binding. A child
inherits an omitted binding; an explicit different binding adds a constraint,
never replaces its ancestors. Guardian status must match tenant, mandate,
`BOAgents`, `BO_INSTALLATION_ID`, and the exact step action/resource. Deploy
Guardian commit `7401eaa63fc74a1728f2758768d67545f8306e35` or its successor first;
old responses without effective scope/identity fail closed. Standalone remains
available only for unbound chains when Guardian authorization is optional.
The status endpoint already applies current-policy revocation and narrowing.
The optional policy-read credential adds a direct read; its absence does not
skip the effective status verdict.

`bo.exec.enabled=false` rejects submissions and prevents claims. Active workers
pause at the next effect boundary; already dispatched effects cannot be undone.
`bo.exec.max_steps` limits new runs in addition to the mandate limit.
`GET /bo/execution/runs/{id}/authority` uses the same local/Guardian evaluator and
reports pause/cancel requests. The UI displays all inherited Guardian references.

Budget is cumulative for the mandate subtree: RESERVED, EXPOSED and COMMITTED
amounts consume budget. Only RESERVED consumes execution slots. UNKNOWN retains
EXPOSED budget; successful or partially executed runs conservatively commit the
whole declared run budget because the provider contract has no per-step charge.
A run with no effects can release its reservation. Resume checks every ancestor
and reacquires capacity in the same SQLite transaction as PENDING. A paused run
is resumed explicitly; workers no longer auto-claim it.

Reconciliation performs a state/version CAS, increments the effect fence and
preserves confirmed receipts. An active SUBMITTED effect may be confirmed from
a receipt, but cannot be declared unexecuted while its lease is active. Late
worker finalization cannot replace terminal evidence. No exactly-once guarantee
is added for providers without idempotency or receipts.

Dead-letter retry starts a fresh attempt series without resetting total attempts
or replacing envelope bytes. `bo_outbox_retries` records the prior count/error,
operator, reason and timestamp in the requeue transaction. The API and expanded
outbox UI expose this history and current-series/total counters.

Startup performs additive, idempotent migration: outbox `retry_base` and retry
history table, plus conservative recovery of legacy RELEASED reservations with
ambiguous or confirmed ledger evidence. Back up using the existing deployment
procedure before upgrade. Keep workers stopped if rolling code back: old code
does not understand EXPOSED accounting or separate retry series. Do not delete
receipts/history or downgrade a live database; restoring an older database must
follow reconciliation procedures to avoid replaying external effects.

The vendored event schema is from Guardian `7401eaa`, SHA256
`e04fafeba4a673eed51a01ce6c36269129e4c6c62981ce105f7f641735e75010`.
Checkpoint `fencingToken` is the persistent run epoch, including terminal states;
it agrees with the lease token whenever a lease is present.
