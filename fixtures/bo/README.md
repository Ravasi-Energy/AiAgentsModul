# Fixtures BO (Valul 1)

Artefacte sintetice pentru contractele `bo.*.v1` — niciun secret, nicio dată
reală, niciun efect extern.

## `telemetry/`

`bo.telemetry.v1.schema.json` este documentul canonic al contractului de
telemetrie (CONTRACTE-V1 + decizia VAL1-02). Validarea runtime trăiește în
`openexecutive/bo/telemetry/schema.py`; testele (`tests/unit/test_bo_telemetry.py`)
verifică că fiecare fixture din `valid/` trece și fiecare fixture din
`invalid/` este respins — schema acceptă numai forma convenită.

Fixture-urile din `valid/` sunt **produse de adaptorul real**, nu scrise de
mână: `packages/core/scripts/generate_bo_telemetry_fixtures.py` injectează un
`BufferedTransport` într-un `TelemetryAdapter` real și scrie plicurile emise.
Regenerare: `cd packages/core && uv run python scripts/generate_bo_telemetry_fixtures.py`.

Contractul VAL1-02, pe scurt: `configVersion` este string opac (serializat
explicit din versiunea internă numerică); `correlationId`+`configVersion`
obligatorii; `runRef` locuiește numai în anvelopă și e obligatoriu pentru
evenimentele de rulare; `RunStarted.data ⊆ {trigger, definitionRef,
versionNo, runKind}`; `VerificationFinding` cere `ownerRef`, `severity` din
INFO/LOW/MEDIUM/HIGH/CRITICAL și `evidenceRefs` listă ≤32×128; `actorRef` e
referință opacă derivată server (`actor_<sha256>`, nu email); timpii au fus
UTC explicit; numerele resping bool.

Identitatea (`tenantRef`, `producerId`, `installationId`) este stampilată de
adaptor la emisie din identitatea autenticată + configurația instalației —
niciodată din payload-ul apelantului.

## `bots/`

`heartbeat_stale.bobot.json` — definiția BoBot sintetică cerută de mandat:
un heartbeat mai vechi de 30 de minute produce un finding de simulare.
Oglindă exactă a `openexecutive.bo.bots.examples.HEARTBEAT_STALE`; testul de
paritate le ține în pas.
