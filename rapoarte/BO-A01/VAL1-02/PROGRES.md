# PROGRES — BO-A01 · VAL1-02 (remediere după verificarea Codex)

Lot: remediere VAL1-02. Sarcina: „Compatibilitate telemetrie, probe UI,
clarificare teste". Continuare pe `bo/val1-a01-agents` peste headSHA
anterior `571eb46`.

## BO-I01 — remediat

Decizia de contract VAL1-02 implementată în `bo.telemetry.v1`:

| Regulă | Înainte | Acum |
|---|---|---|
| configVersion | int ≥0 în anvelopă + data | **string opac nevid**; serializare explicită `str(v)` la emitere |
| correlationId/configVersion | corr obligatoriu, cv opțional | **ambele obligatorii** |
| runRef | în `data` | **numai în anvelopă**; obligatoriu nevid pentru RunStarted/RunFinished |
| RunStarted.data | +runRef | ⊆ `{trigger, definitionRef, versionNo≥1, runKind}`, toate opționale |
| RunFinished.data | +runRef | `{executionStatus, verificationStatus}` cerute; `planHash`/`durationMs` opționale, `durationMs` finit ≥0 |
| Finding | severity text liber ≤32, ownerRef opțional, evidenceRefs ≤50×256 | `ownerRef` **cerut**, `severity ∈ INFO/LOW/MEDIUM/HIGH/CRITICAL`, `evidenceRefs` ≤32×128 |
| ConfigApplied | fără appliedVersion, actorRef=email | `appliedVersion` cerut (nullable), `actorRef` opac `actor_<sha256>` — `@` respins |
| Timestamp | orice offset RFC3339 | **fus UTC explicit** (Z sau +00:00) |
| Schemă închisă / bool≠număr | da | neschimbat, păstrat |

Consecință: `bo.bobot.v1` `emit_finding.severity` a fost trecut de la
`info|warning|critical` la enumul contractului (`INFO|LOW|MEDIUM|HIGH|
CRITICAL`); exemplul + fixture-ul `heartbeat_stale` actualizate la `MEDIUM`.

## Fixture-uri produse de adaptorul real

`packages/core/scripts/generate_bo_telemetry_fixtures.py` emite fiecare
tip prin `TelemetryAdapter` real + `BufferedTransport` și scrie
`fixtures/bo/telemetry/valid/*.json`. 5 valide + **15 invalide**, fiecare
invalidă pică doar din motivul ei (verificat mesaj cu mesaj).

## Probă e2e: BoBot → evenimente → receptor Guardian local

`tests/unit/test_bo_e2e_telemetry.py` (5 teste): simulare reală a
`heartbeat_stale` cu adaptor injectat → `RunStarted`+`RunFinished`+
`VerificationFinding` ajung la `LocalGuardianReceiver` (dublură de test:
înrolare producător+instalație→tenant, credențial, aceeași poartă de schemă,
dedup pe eventId). Refuzate: producător neînrolat, credențial greșit,
tenantRef ≠ înrolare, replay eventId, payload modificat. Zero efecte
externe — totul în memorie.

## Probe vizuale — `probe-ui/` (40 capturi + REZULTATE.md)

Stivă reală (`next start` produs + backend + sesiune JWT reală, fără
modificări de cod): 5 pagini × {390,768,1440} × {dark,light}, **fără
overflow**; edge-uri măsurate computed: 699→700 (grid 1→2 col, padding
16→24, titlu 20→22) și 1099→1100 (padding 24→40); tastatură 14 opriri cu
focus vizibil; erori reale randate: bot negăsit, rulare inexistentă,
conflict CAS (banner „Altă modificare s-a salvat între timp").

## Clarificare teste (BO-I05)

Comanda din predarea anterioară era `pytest tests/unit/` — **unit** nu
întreaga suită. Precizare completă:

| Suită | Comandă | Rezultat |
|---|---|---|
| unit | `pytest tests/unit/ -x -q` | **3574 passed, 1 skipped** (include cele 111 teste BO) |
| integration | `pytest tests/integration/ -q` | **46 passed, 3 eșecuri** — vezi mai jos |

Eșecuri integration pe HEAD: `test_chat_committee…revised_text`
(TypeError `list_sessions(caller_person_id)` — test învechit pe main) +
`test_scheduler_runner` ×2. Pe **baseSHA `622747c`** (worktree separat):
identic `test_chat_committee` + `test_execute_action_retries_on_exception`
— 2 eșecuri preexistente confirmate. Al treilea (`marks_done_on_success`)
era poluare: un `episodic_memory.db` rătăcit creat de backend-ul meu de
probă în cwd; după ștergere testul trece și pe HEAD. **Niciunul nu atinge
codul BO** — dar poluarea prin DB implicit e o fragilitate reală a
suitei, documentată.

## Rezultate verificare VAL1-02

- `pytest tests/unit/ -k bo_` → **111 passed** (106 + 5 e2e noi)
- `pytest tests/unit/` complet → **3574 passed, 1 skipped**
- `pytest tests/integration/` → 46/49 (3 preexistente, explicate sus)
- ruff + mypy pe `openexecutive/bo`, `routes/bo.py`, script → curat
- `next build` → verde (UI neschimbat în acest lot)

## Dependență A02

Schema comună JSON nu a ajuns încă în pachet (verificat
`BO-VAL1/` la 22.09). Implementarea urmează textual decizia de contract;
la sosirea artefactului A02 consum identic documentul și reconciliez
orice diferență de redactare (nume de câmpuri, limite).
