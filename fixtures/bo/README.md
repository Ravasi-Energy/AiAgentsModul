# Fixtures BO (Valul 1)

Artefacte sintetice pentru contractele `bo.*.v1` — niciun secret, nicio dată
reală, niciun efect extern.

## `telemetry/`

`bo.telemetry.v1.schema.json` este documentul canonic al contractului de
telemetrie (CONTRACTE-V1). Validarea runtime trăiește în
`openexecutive/bo/telemetry/schema.py`; testele (`tests/unit/test_bo_telemetry.py`)
verifică că fiecare fixture din `valid/` trece și fiecare fixture din
`invalid/` este respins — schema acceptă numai forma convenită.

Identitatea (`tenantRef`, `producerId`, `installationId`) este stampilată de
adaptor la emisie din identitatea autenticată + configurația instalației —
niciodată din payload-ul apelantului.

## `bots/`

`heartbeat_stale.bobot.json` — definiția BoBot sintetică cerută de mandat:
un heartbeat mai vechi de 30 de minute produce un finding de simulare.
Oglindă exactă a `openexecutive.bo.bots.examples.HEARTBEAT_STALE`; testul de
paritate le ține în pas.
