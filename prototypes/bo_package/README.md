# Prototip VAL2-00 — pachete semnate `bo.package.v1`

**Statut: prototip izolat, ISTORIC (VAL2-00). NU este funcționalitate de
produs, NU este activat în runtime și NU are efecte externe.**

> **Sursa canonică VAL2-01 este artefactul înghețat din
> `coordonare/contracte/bo.package.v1/`** (în afara acestui repo —
> fingerprint `02571944…92d99`, schemă `f070e838…95e13`). Fișierele de aici
> sunt istoricul propunerii VAL2-00: schema din acest director
> (`4c826aeb…f4c`) diferă de cea înghețată și **nu** este citită de
> runtime (`openexecutive/bo/packages/contract.py` validează prin cod).
> Implementarea de produs remediată este în `openexecutive/bo/packages/`.

## Conținut

- `CONTRACT-bo.package.v1.md` — contractul propus (manifest, canonicalizare,
  registru de încredere, ordinea verificării).
- `bo.package.v1.schema.json` — schema **istorică** JSON Schema 2020-12
  (VAL2-00; cea canonică e în artefactul înghețat).
- `bo_pkg/` — implementarea de referință: `contract.py` (validare + semver),
  `canon.py` (canonicalizare subset-JCS), `signing.py` (Ed25519),
  `registry.py` (trust store `bo.package.registry.v1`), `build.py`
  (producere), `verify.py` (verificare, fără instalare).
- `router_interface.py` — interfața propusă a routerului de modele (BO-R03),
  doar contract, fără runtime.
- `make_fixture_packages.py` — generator determinist de fixture-uri.
- `fixtures/` — chei SINTETICE, registru, 16 pachete (2 pozitive, 14 negative),
  `expected.json` cu verdictul așteptat per caz.
- `tests/` — suită pytest a prototipului.

## Rulare

```bash
# regenerare fixture-uri (deterministă la nivel de manifest)
PYTHONPATH=prototypes/bo_package python prototypes/bo_package/make_fixture_packages.py

# teste
python -m pytest prototypes/bo_package/tests/ -q
```

Dependență: `cryptography` (deja în venv-ul `packages/core`). Nicio dependență
nouă, niciun import din `openexecutive`, nicio scriere în afara `fixtures/`.
