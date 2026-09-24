"use client";

// Execuții delegate (VAL4-01): mandate, rulări, checkpointuri și ledgerul
// de efecte. Distincțiile obligatorii din contractul comun: execuție ≠
// observație, succes ≠ rezultat necunoscut, reluare ≠ duplicarea efectului,
// anularea NU inversează un efect extern deja executat.
import { useCallback, useEffect, useState } from "react";

import IconBO from "@/components/bo/IconBO";
import { InlineAlert, Pill, StateBlock } from "@/components/bo/ui";
import {
  BoApiError,
  cancelBoRun,
  getBoExecStatus,
  getBoRunDetail,
  listBoExecRuns,
  pauseBoRun,
  reconcileBoRun,
  resumeBoRun,
  type BoExecRun,
  type BoExecStatus,
  type BoRunDetail,
} from "@/lib/bo";

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "forbidden" }
  | {
      kind: "data";
      status: BoExecStatus;
      runs: BoExecRun[];
      detail: BoRunDetail | null;
    };

const STATE_LABEL: Record<string, { label: string; kind: "ok" | "warn" | "danger" | "info" | "neutral" }> = {
  PENDING: { label: "în așteptare", kind: "neutral" },
  CLAIMED: { label: "revendicată", kind: "info" },
  RUNNING: { label: "în rulare", kind: "info" },
  PAUSED: { label: "pausată", kind: "warn" },
  SUCCEEDED: { label: "reușită", kind: "ok" },
  FAILED: { label: "eșuată", kind: "danger" },
  UNKNOWN: { label: "necunoscută", kind: "warn" },
  RECONCILIATION_REQUIRED: { label: "reconciliere necesară", kind: "danger" },
  CANCELLED: { label: "anulată", kind: "neutral" },
};

const LEDGER_LABEL: Record<string, { label: string; kind: "ok" | "warn" | "danger" | "info" | "neutral" }> = {
  INTENT: { label: "intenție", kind: "neutral" },
  SUBMITTED: { label: "trimisă", kind: "info" },
  SUCCEEDED: { label: "efect confirmat", kind: "ok" },
  FAILED: { label: "efect eșuat", kind: "danger" },
  UNKNOWN: { label: "efect necunoscut", kind: "warn" },
  RECONCILIATION_REQUIRED: { label: "reconciliere", kind: "danger" },
};

function statePill(state: string) {
  const s = STATE_LABEL[state] ?? { label: state, kind: "neutral" as const };
  return <Pill kind={s.kind}>{s.label}</Pill>;
}

function RunDetail({
  detail,
  canOperate,
  onChanged,
}: {
  detail: BoRunDetail;
  canOperate: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "warn" | "danger"; text: string } | null>(null);

  async function act(fn: () => Promise<unknown>, okText: string) {
    setBusy(true);
    setNotice(null);
    try {
      await fn();
      setNotice({ kind: "ok", text: okText });
      onChanged();
    } catch (err) {
      const text =
        err instanceof BoApiError
          ? `${err.status} — ${typeof err.detail === "string" ? err.detail : err.code}`
          : "Acțiunea a eșuat.";
      setNotice({ kind: "danger", text });
    } finally {
      setBusy(false);
    }
  }

  const run = detail.run;
  const ambiguous = detail.ledger.filter(
    (e) => e.status === "UNKNOWN" || e.status === "RECONCILIATION_REQUIRED",
  );
  const active = !["SUCCEEDED", "FAILED", "CANCELLED"].includes(run.state);
  const canResume = ["PAUSED", "UNKNOWN", "RECONCILIATION_REQUIRED"].includes(
    run.state,
  );

  return (
    <div className="bo-card" style={{ marginTop: 12 }}>
      <div className="bo-spread">
        <h3 className="bo-card-title">Rulare {run.run_id}</h3>
        {statePill(run.state)}
      </div>
      <p className="bo-hint" style={{ marginTop: 4 }}>
        Pas {run.current_step}/{run.steps.length} · politică v
        {run.policy_version} · corelare {run.correlation_id}
      </p>
      {run.block_reason ? (
        <div style={{ marginTop: 8 }}>
          <InlineAlert kind="warn">Blocată: {run.block_reason}</InlineAlert>
        </div>
      ) : null}

      {/* Lanțul de delegare */}
      <h4 className="bo-card-title" style={{ marginTop: 16 }}>Lanț de delegare</h4>
      {detail.chain.map((m) => (
        <div key={m.mandate_id} className="bo-row" style={{ marginTop: 6 }}>
          <Pill kind={m.state === "active" ? "ok" : "danger"} icon="shield-check">
            adâncime {m.depth} · {m.state}
          </Pill>
          <span className="bo-hint">
            {m.mandate_id} · acțiuni [{m.allowed_actions.join(", ")}] ·
            resurse [{m.allowed_resources.join(", ")}] · buget{" "}
            {m.budget_limit} · expiră {m.expires_at.slice(0, 19)}Z
          </span>
        </div>
      ))}
      {detail.mandate.revoked_reason ? (
        <p className="bo-hint" style={{ marginTop: 4 }}>
          Revocat: {detail.mandate.revoked_reason}
        </p>
      ) : null}

      {/* Rezervare */}
      {detail.reservation ? (
        <p className="bo-hint" style={{ marginTop: 12 }}>
          Rezervare: {detail.reservation.amount} ·{" "}
          {detail.reservation.slots} sloturi ·{" "}
          {detail.reservation.state}
        </p>
      ) : null}

      {/* Ledger */}
      <h4 className="bo-card-title" style={{ marginTop: 16 }}>Ledger de efecte</h4>
      {detail.ledger.length === 0 ? (
        <p className="bo-hint">Nicio intrare — niciun efect nu a fost încercat.</p>
      ) : (
        detail.ledger.map((e) => {
          const l = LEDGER_LABEL[e.status] ?? {
            label: e.status,
            kind: "neutral" as const,
          };
          return (
            <div key={e.entry_id} className="bo-row" style={{ marginTop: 6 }}>
              <Pill kind={l.kind}>{l.label}</Pill>
              <span className="bo-hint">
                pas {e.step} · {e.provider} · cheie {e.idempotency_key} ·
                digest {e.payload_digest.slice(0, 12)}… · încercări{" "}
                {e.attempts}
                {e.receipt_ref ? ` · chitanță ${e.receipt_ref}` : " · fără chitanță"}
              </span>
            </div>
          );
        })
      )}
      {ambiguous.length > 0 ? (
        <div style={{ marginTop: 8 }}>
          <InlineAlert kind="warn" icon="shield-alert">
            {ambiguous.length} efect(e) cu stare externă necunoscută — un
            timeout după trimitere NU dovedește eșecul. Reconcilierea
            verifică chitanța providerului înainte de orice reexecutare;
            exactly-once nu e garantat pentru provideri fără idempotență.
          </InlineAlert>
        </div>
      ) : null}

      {/* Checkpointuri */}
      <h4 className="bo-card-title" style={{ marginTop: 16 }}>Checkpointuri</h4>
      {detail.checkpoints.length === 0 ? (
        <p className="bo-hint">Niciun checkpoint — rularea nu a pornit.</p>
      ) : (
        detail.checkpoints.map((c) => (
          <div key={`${c.step}-${c.checkpoint_version}`} className="bo-row" style={{ marginTop: 4 }}>
            <Pill kind="neutral" icon="history">
              pas {c.step} · v{c.checkpoint_version}
            </Pill>
            <span className="bo-hint">
              {String(c.state.phase)} · {c.created_at.slice(11, 19)}Z ·
              stare de execuție (nu dovadă de efect)
            </span>
          </div>
        ))
      )}

      {/* Controale */}
      {canOperate && active ? (
        <div className="bo-row" style={{ marginTop: 16, flexWrap: "wrap" }}>
          {!run.pause_requested && (
            <button
              type="button"
              className="bo-btn"
              disabled={busy}
              onClick={() =>
                act(() => pauseBoRun(run.run_id), "Pauză cerută — se aplică la următorul pas.")
              }
            >
              Pauzează
            </button>
          )}
          {canResume && (
            <button
              type="button"
              className="bo-btn bo-btn--primary"
              disabled={busy}
              onClick={() =>
                act(() => resumeBoRun(run.run_id), "Reluare programată — aceeași identitate și chei.")
              }
            >
              Reia
            </button>
          )}
          {ambiguous.length > 0 && (
            <>
              <button
                type="button"
                className="bo-btn"
                disabled={busy}
                onClick={() =>
                  act(
                    () => reconcileBoRun(run.run_id, "receipt"),
                    "Reconciliere prin chitanțe efectuată.",
                  )
                }
              >
                Reconciliază (chitanțe)
              </button>
              <button
                type="button"
                className="bo-btn"
                disabled={busy}
                onClick={() =>
                  act(
                    () => reconcileBoRun(run.run_id, "mark_failed"),
                    "Marcat ca neexecutat — decizia operatorului e auditată.",
                  )
                }
              >
                Marchează neexecutat
              </button>
            </>
          )}
          {!run.cancel_requested && (
            <button
              type="button"
              className="bo-btn"
              disabled={busy}
              onClick={() =>
                act(() => cancelBoRun(run.run_id), "Anulare cerută.")
              }
            >
              Anulează
            </button>
          )}
        </div>
      ) : null}
      <p className="bo-hint" style={{ marginTop: 10 }}>
        Anularea oprește pașii viitori la următoarea frontieră — NU
        inversează un efect extern deja executat.
      </p>
      {notice ? (
        <div style={{ marginTop: 8 }}>
          <InlineAlert kind={notice.kind === "ok" ? "info" : notice.kind}>
            {notice.text}
          </InlineAlert>
        </div>
      ) : null}
    </div>
  );
}

export default function BoExecutionsPage() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("");

  const load = useCallback(async () => {
    try {
      const [status, runsRes] = await Promise.all([
        getBoExecStatus(),
        listBoExecRuns(filter || undefined),
      ]);
      let detail: BoRunDetail | null = null;
      if (selected) {
        try {
          detail = await getBoRunDetail(selected);
        } catch {
          detail = null;
        }
      }
      setState({ kind: "data", status, runs: runsRes.runs, detail });
    } catch (err) {
      if (err instanceof BoApiError && err.status === 403) {
        setState({ kind: "forbidden" });
      } else {
        setState({
          kind: "error",
          message: "Nu am putut încărca execuțiile. Verifică backend-ul.",
        });
      }
    }
  }, [filter, selected]);

  useEffect(() => {
    void load();
  }, [load]);

  async function open(runId: string) {
    setSelected(runId === selected ? null : runId);
  }

  if (state.kind === "loading") {
    return <StateBlock state="loading" title="Se încarcă execuțiile" />;
  }
  if (state.kind === "forbidden") {
    return (
      <StateBlock
        state="forbidden"
        title="Acces interzis"
        detail="Contul tău nu are drept de citire pe execuțiile BOAgents."
      />
    );
  }
  if (state.kind === "error") {
    return (
      <StateBlock
        state="error"
        title="Eroare la încărcare"
        detail={state.message}
        action={
          <button type="button" className="bo-btn" onClick={load}>
            <IconBO name="refresh" size={15} /> Reîncearcă
          </button>
        }
      />
    );
  }

  const { status, runs, detail } = state;
  const canOperate = true; // server enforcește RBAC; controalele cer 403 explicit

  return (
    <div className="bo-scope" style={{ marginTop: 20 }}>
      <div className="bo-row" style={{ marginBottom: 16, flexWrap: "wrap" }}>
        <Pill kind={status.enabled ? "ok" : "warn"} icon="shield-check">
          execuție {status.enabled ? "pornită" : "oprită"}
        </Pill>
        <Pill kind="neutral" icon="list">
          {status.runs_total} rulări
        </Pill>
        <Pill kind="neutral" icon="activity">
          efecte sintetice: {status.synthetic_effect_total}
        </Pill>
      </div>

      <InlineAlert kind="info" icon="info">
        Aceasta este o suprafață de <strong>execuție</strong>: mandatele
        autorizează, checkpointurile salvează stare de execuție, iar
        ledgerul dovedește efectele. Este diferită de observațiile
        routerului (care nu produc efecte). Efectele sunt sintetice și
        locale în această etapă.
      </InlineAlert>
      <p className="bo-hint" style={{ marginTop: 8 }}>{status.note}</p>

      {/* Filtru stare */}
      <div className="bo-row" style={{ marginTop: 16 }}>
        <select
          className="bo-select"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          aria-label="Filtru stare"
        >
          <option value="">Toate stările</option>
          {Object.keys(STATE_LABEL).map((s) => (
            <option key={s} value={s}>
              {STATE_LABEL[s].label}
            </option>
          ))}
        </select>
        <button type="button" className="bo-btn" onClick={load}>
          <IconBO name="refresh" size={15} /> Reîmprospătează
        </button>
      </div>

      {/* Lista rulărilor */}
      {runs.length === 0 ? (
        <StateBlock
          state="empty"
          title="Nicio rulare"
          detail="Execuțiile delegate apar aici după trimitere."
        />
      ) : (
        <div style={{ marginTop: 12 }}>
          {runs.map((r) => (
            <button
              key={r.run_id}
              type="button"
              className="bo-card"
              style={{
                width: "100%",
                textAlign: "left",
                marginTop: 8,
                cursor: "pointer",
              }}
              onClick={() => void open(r.run_id)}
            >
              <div className="bo-spread">
                <div>
                  <strong>{r.run_id}</strong>
                  <span className="bo-hint" style={{ marginLeft: 8 }}>
                    mandat {r.mandate_id.slice(0, 14)}… · pas{" "}
                    {r.current_step}/{r.steps.length}
                    {r.parent_run_id ? " · copil" : ""}
                  </span>
                </div>
                {statePill(r.state)}
              </div>
              {r.block_reason ? (
                <p className="bo-hint" style={{ marginTop: 4 }}>
                  {r.block_reason}
                </p>
              ) : null}
            </button>
          ))}
        </div>
      )}

      {detail ? (
        <RunDetail
          detail={detail}
          canOperate={canOperate}
          onChanged={() => void load()}
        />
      ) : null}
    </div>
  );
}
