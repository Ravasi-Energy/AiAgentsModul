# PREDARE — BO-A01 / VAL2-00: pachete semnate și interfața routerului

**Statut: TESTAT_LOCAL. Prototip izolat + propuneri de contract — nimic nu
este activat în runtime și nimic nu este funcționalitate acceptată. Codex
verifică independent și decide.**

- Repository: `Ravasi-Energy/BOAgents` (worktree separat `boagents-val2`)
- Branch: `bo/val2-00-a01-packages` — separat de `bo/val1-a01-agents` (VAL1-02 rămâne neatins)
- baseSHA: `622747c5221e70c2db629027bb0390c630ab7bc8` (main)
- headSHA: vezi MANIFEST-VAL2-00.json

## Cerințe acoperite

| Cerință mandat | Livrat | Unde |
|---|---|---|
| `bo.package.v1` — câmpuri, canonicalizare, algoritm, limite, hash artefacte | Contract + schemă JSON + implementare referință | `prototypes/bo_package/CONTRACT-bo.package.v1.md`, `bo.package.v1.schema.json`, `bo_pkg/` |
| Prototip producere/verificare pachet sintetic semnat | Ed25519 + JCS-subset; build + verify fără efecte | `bo_pkg/{build,verify,signing,canon}.py` |
| Pachet modificat → refuz | `tampered-artifact`, `missing-artifact`, `unsigned-artifact` | `fixtures/packages/` |
| Emitent necunoscut/revocat → refuz | `unknown-signer`, `suspended-publisher`, `revoked-key`, `expired-key` | idem |
| Versiune incompatibilă → refuz | `incompatible-host` (host < minHost) | idem |
| Rollback neautorizat → refuz | `rollback-unauthorized` (respins) vs `rollback-approved` (acceptat cu approvalRef din registru) | idem |
| Capabilități excesive → refuz | `excessive-capabilities`, `unknown-capability` | idem |
| Propunere Setări | 10 chei `bo.packages.*`/`bo.router.*`, default oprit, admin+audit | `rapoarte/BO-A01/VAL2-00/SETARI-PROPUSE-bo.packages.json` |
| Evaluare router separat, fără runtime | Contract I/O + 6 refuzuri + reguli de comportament | `prototypes/bo_package/router_interface.py`, `ROUTER-EVAL.md` |
| Fără MetaHarness integral | Doar idei din studiu, SHA fixat; zero cod preluat, zero dependențe noi | proveniență mai jos |
| Telemetrie VAL1-02 neatinsă | diff = doar `prototypes/` + `rapoarte/` | `git diff --name-only` |

## Decizii de design (de ratificat de Codex/A02)

1. **Cheia publică vine doar din registrul de încredere** (`bo.package.registry.v1`,
   administrat separat) — nu din manifest. Corectează defectul upstream
   documentat în studiu (manifest care își poartă propria cheie nu stabilește
   autoritatea emitentului).
2. **Ordinea verificării este contract** (§4): schemă → publisher → cheie →
   semnătură → artefacte → compatibilitate → capabilități → rollback →
   dimensiune. Scurtcircuit la prima respingere.
3. **Reinstalarea aceleiași versiuni = rollback** — respinsă fără `approvedRollbacks`
   (împiedică înlocuirea tăcută a conținutului).
4. **`manifest.json` nu poate fi artefact semnat** (auto-referință).
5. **Routerul e doar interfață**: `RouteDecision.REFUSE` e ieșire de primă
   clasă; `best_effort` la `QUALITY_BAR_UNMET` este informativ + escaladare,
   niciodată rută acceptată.

## Verificări executate

```bash
PYTHONPATH=prototypes/bo_package python prototypes/bo_package/make_fixture_packages.py
# → 17 cazuri: 2 ACCEPT, 15 REJECT cu coduri distincte

pytest prototypes/bo_package/tests/ -q          # 26 passed
ruff check prototypes/bo_package/...            # All checks passed
mypy prototypes/bo_package/bo_pkg/ ...          # no issues, 10 files
```

## Proveniență / licențe

- Inspirație conceptuală: MetaHarness `witness.rs` (manifest semnat) și
  `router/src/index.ts` @ `d5833dc6512ac1adeeef91a331c29055cd8a4dbb`, licență
  MIT declarată la rădăcină. **Nicio linie de cod upstream nu a fost preluată.**
- Dependență: `cryptography` (Ed25519) — deja în `uv.lock` al repo-ului;
  altfel stdlib. Cheile din `fixtures/keys.json` sunt SINTETICE, generate
  local, fără autoritate reală.

## Limite declarate

- Verificarea dovedește integritate + proveniență conform trust-store; NU
  dovedește siguranța comportamentului pachetului (cerință explicită din studiu).
- Ciclul de viață complet (import → carantină → aprobare → activare) și
  consumul real de către BOGuardian rămân la VAL2-01/A02.
- Prototipul nu a fost verificat de al doilea agent (subagenții sunt interziși
  de mandat) — auto-revizie documentată în ledger Anvil.
- Rollback `<=` strict: reinstalarea aceleiași versiuni cere aprobare;
  dacă se dorește reinstalare idempotentă, contractul se ajustează la ratificare.

## Revenire

`git checkout 622747c` — prototipul e pur aditiv (`prototypes/`, `rapoarte/`);
ștergerea celor două directoare revine complet.

## Artefacte

- `~/bo-a01-work/bo-a01-val2-00.bundle` + SHA256 în `MANIFEST-VAL2-00.json`
