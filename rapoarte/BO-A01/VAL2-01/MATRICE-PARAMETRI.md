# Matricea parametrilor — paginile Pachete și Setări (VAL2-01)

Sursă de adevăr: `openexecutive/bo/settings/registry.py` (`REGISTRY`, 9 chei).
Toate cele 9 chei sunt administrabile astăzi — niciun parametru blocat.

**Coloane comune (identice pentru toate cheile):**

- **Scope:** `tenant` — o suprascriere per `(tenant, key)`.
- **RBAC:** citire `settings:read` → minim `viewer`; scriere
  `settings:write` → minim `admin` (`openexecutive/bo/identity.py`,
  `_CAP_MIN_ROLE`). Rolul vine din `x-caller-email` + `BO_ADMIN_EMAILS` /
  roster principal.
- **Persistență:** tabel `bo_settings` în `bo_agents.db`, PK `(tenant,key)`,
  CAS per cheie prin `expected_version`; `config_version` agregat în răspuns.
- **Audit:** fiecare salvare scrie `bo_setting_change` în auditul global
  (`old`/`new` redactate dacă `sensitivity != "normal"` — toate cele 9 sunt
  `normal`).
- **Aplicare:** `IMMEDIATE` = efect instant; `NEW_RUN` = doar la simulări
  pornite după salvare.

## Setări (pagina `/settings/bo`, tab „general")

| Cheie | Tip / default / limite | Aplicare | Proba efectului |
|---|---|---|---|
| `bo.ui.display_name` | text / `"BOAgents"` / 1–80 | IMMEDIATE | UI randează denumirea; `test_invalid_values_rejected` respinge >80/gol; probe-ui `settings-bo-pachete.png` |
| `bo.ui.language` | enum / `"ro"` / `ro`+`en` | IMMEDIATE | `test_invalid_values_rejected` respinge alte limbi; pagina /settings/bo afișează select RO/EN |
| `bo.ui.timezone` | timezone / `"Europe/Bucharest"` / IANA valid | IMMEDIATE | `test_invalid_values_rejected` respinge fus invalid |
| `bo.bobot.simulation.max_steps` | int / `50` / 1–500 | NEW_RUN | simulările noi respectă limita; validare respinsă în afara intervalului (teste setări) |
| `bo.bobot.simulation.retention_days` | int / `30` / 1–3650 | IMMEDIATE | sweep-ul șterge doar simulări expirate; audit neatins |
| `bo.packages.enabled` | bool / **`false`** | IMMEDIATE | oprit → `POST /bo/packages/import` răspunde `PACKAGES_DISABLED` (`test_packages_disabled`); pornit + trust store gol → `INVALID_REGISTRY` (`test_empty_trust_store_rejects`); probă live: import acceptat după activare |
| `bo.packages.max_package_bytes` | int / `8388608` / 65536–268435456 | IMMEDIATE | pachet peste minimul (setare, trust store) → `PACKAGE_TOO_LARGE` (`test_manifest_too_large`, fixture `manifest-too-large`) |
| `bo.packages.trust_store_json` | text (JSON `bo.package.registry.v1`) / `""` | IMMEDIATE | salvarea validează structural (registru malformat respins la salvare — `test_trust_store_accepts_pretty_json` + respingere malformată); gol = niciun pachet nu verifică; revocarea unei chei intră în vigoare la următorul import |
| `bo.packages.rollback_requires_approval` | bool / **`true`** | IMMEDIATE | activ → downgrade fără aprobare legată = `ROLLBACK_UNAUTHORIZED` (`test_downgrade_needs_bound_approval`); aprobare legată + neconsumată → ACCEPT și consum (`test_downgrade_with_bound_approval_consumes_it`) |

## Pagina `/bo/packages` — parametri de intrare (nu sunt setări)

| Parametru | Tip / limite | RBAC | Persistență / audit | Probă |
|---|---|---|---|---|
| `source_dir` (import) | cale director pe server, validată; fără upload | `packages:write` → admin | rând `bo_package_imports` (QUARANTINED/REJECTED/DRAFT) + `bo_package_imported`/`_rejected` în audit | probe-ui `/bo/packages` (import real ACCEPT+REJECT); `test_bo_packages` 66 |
| `package_id`, `from_version`, `to_version`, `artifact_set_digest`, `expires_at` (aprobare) | stringuri, max 64–80 caractere, semver pe versiuni, digest `sha256:` | `packages:write` → admin | rând `bo_package_approvals` + `bo_package_approval`/`_revoked` în audit | fixture-uri `approval-*` + `test_approval_*` |
| `import_id` (promovare) | id intern | `packages:write` → admin | tranziție QUARANTINED→DRAFT cu re-verificare + `bo_package_promoted` | `test_promote_*`; probă live (import promovat la DRAFT) |

**Aplicare imediată demonstrată live (proba FIN-01):** seed prin API real —
`bo.packages.enabled` false→true a comutat importul din `PACKAGES_DISABLED` în
ACCEPT; trust store gol → `INVALID_REGISTRY`; reimport identic →
`idempotent: true` în anvelopă.
