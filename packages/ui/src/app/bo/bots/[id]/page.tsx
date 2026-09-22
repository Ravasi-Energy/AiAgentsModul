"use client";

// BoBot detail — provenance (versions, hashes), draft editor with CAS,
// deterministic simulation panel, run history. Romanian copy per mandate.
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import IconBO from "@/components/bo/IconBO";
import { BoPage, Field, InlineAlert, Pill, StateBlock } from "@/components/bo/ui";
import {
  BoApiError,
  getBoBot,
  getBoSettings,
  listBoRuns,
  patchBoBot,
  publishBoBot,
  simulateBoBot,
  type BoBotDetail,
  type BoRun,
} from "@/lib/bo";

type DetailState =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "forbidden" }
  | { kind: "notfound" }
  | {
      kind: "data";
      detail: BoBotDetail;
      runs: BoRun[];
      role: "admin" | "operator" | "viewer" | null;
    };

const SAMPLE_INPUT = `{
  "service": {
    "name": "billing",
    "last_heartbeat_age_minutes": 45
  }
}`;

function shortHash(h: string | null | undefined) {
  return h ? `${h.slice(0, 10)}…` : "—";
}

export default function BoBotDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;

  const [state, setState] = useState<DetailState>({ kind: "loading" });
  const [busy, setBusy] = useState<"publish" | "save" | "simulate" | null>(null);
  const [notice, setNotice] = useState<
    { kind: "ok" | "warn" | "danger"; text: string } | null
  >(null);

  // draft editor state
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [contentJson, setContentJson] = useState("");

  // simulate panel state
  const [inputJson, setInputJson] = useState(SAMPLE_INPUT);
  const [useDraft, setUseDraft] = useState(false);
  const [lastRun, setLastRun] = useState<BoRun | null>(null);

  const load = useCallback(async () => {
    try {
      const detail = await getBoBot(id);
      const runs = await listBoRuns(id).catch(() => ({ runs: [] as BoRun[] }));
      let role: "admin" | "operator" | "viewer" | null = null;
      try {
        role = (await getBoSettings()).role;
      } catch {
        role = null; // unknown role → UI stays read-only; server enforces anyway
      }
      setState({ kind: "data", detail, runs: runs.runs, role });
      setName(detail.bot.name);
      setDescription(detail.bot.description);
      setContentJson(JSON.stringify(detail.draft?.content ?? {}, null, 2));
    } catch (err) {
      if (err instanceof BoApiError && err.status === 404) {
        setState({ kind: "notfound" });
      } else if (err instanceof BoApiError && err.status === 403) {
        setState({ kind: "forbidden" });
      } else {
        setState({ kind: "error" });
      }
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function saveDraft() {
    if (state.kind !== "data") return;
    setBusy("save");
    setNotice(null);
    let parsed: Record<string, unknown> | undefined;
    try {
      parsed = JSON.parse(contentJson);
    } catch {
      setNotice({ kind: "danger", text: "Conținutul ciornei nu este JSON valid." });
      setBusy(null);
      return;
    }
    try {
      await patchBoBot(id, {
        expected_version: state.detail.bot.draft_version,
        name,
        description,
        content: parsed,
      });
      setNotice({ kind: "ok", text: "Ciorna a fost salvată." });
      await load();
    } catch (err) {
      if (err instanceof BoApiError && err.status === 409) {
        setNotice({
          kind: "warn",
          text: "Ciorna s-a schimbat între timp — reîncarc pentru versiunea curentă.",
        });
        await load();
      } else if (err instanceof BoApiError && err.status === 422) {
        const d = err.detail;
        setNotice({
          kind: "danger",
          text: Array.isArray(d) ? d.join(" · ") : "Definiție invalidă.",
        });
      } else {
        setNotice({ kind: "danger", text: "Salvarea a eșuat." });
      }
    } finally {
      setBusy(null);
    }
  }

  async function publish() {
    if (state.kind !== "data") return;
    setBusy("publish");
    setNotice(null);
    try {
      const res = await publishBoBot(id);
      setNotice({
        kind: "ok",
        text: `Publicat — versiunea v${res.active_version_no} este acum activă.`,
      });
      await load();
    } catch (err) {
      if (err instanceof BoApiError && err.status === 422) {
        const d = err.detail;
        setNotice({
          kind: "danger",
          text: Array.isArray(d) ? d.join(" · ") : "Definiția nu validează.",
        });
      } else {
        setNotice({ kind: "danger", text: "Publicarea a eșuat." });
      }
    } finally {
      setBusy(null);
    }
  }

  async function simulate() {
    if (state.kind !== "data") return;
    setBusy("simulate");
    setNotice(null);
    let input: Record<string, unknown> | undefined;
    try {
      input = inputJson.trim() ? JSON.parse(inputJson) : {};
    } catch {
      setNotice({ kind: "danger", text: "Contextul de simulare nu este JSON valid." });
      setBusy(null);
      return;
    }
    try {
      const res = await simulateBoBot(id, { input, draft: useDraft });
      setLastRun(res.run);
      setNotice({
        kind: "ok",
        text: `Simulare ${res.run.result?.execution_status ?? ""} — ${res.run.result?.findings.length ?? 0} constatări.`,
      });
      await load();
    } catch (err) {
      if (err instanceof BoApiError && err.status === 422) {
        setNotice({
          kind: "danger",
          text:
            typeof err.detail === "string"
              ? err.detail
              : "Simularea a fost refuzată.",
        });
      } else if (err instanceof BoApiError && err.status === 403) {
        setNotice({ kind: "danger", text: "Simularea cere rol de operator sau admin." });
      } else {
        setNotice({ kind: "danger", text: "Simularea a eșuat." });
      }
    } finally {
      setBusy(null);
    }
  }

  if (state.kind === "loading") {
    return (
      <BoPage title="BoBot">
        <StateBlock state="loading" title="Se încarcă definiția" />
      </BoPage>
    );
  }
  if (state.kind === "notfound") {
    return (
      <BoPage
        title="BoBot negăsit"
        actions={
          <Link href="/bo/bots" className="bo-btn">
            <IconBO name="arrow-left" size={15} /> Înapoi la listă
          </Link>
        }
      >
        <StateBlock state="empty" title="Definiția nu există" detail="Verifică identificatorul sau întoarce-te la listă." />
      </BoPage>
    );
  }
  if (state.kind === "forbidden" || state.kind === "error") {
    return (
      <BoPage
        title="BoBot"
        actions={
          <Link href="/bo/bots" className="bo-btn">
            <IconBO name="arrow-left" size={15} /> Înapoi la listă
          </Link>
        }
      >
        <StateBlock
          state={state.kind === "forbidden" ? "forbidden" : "error"}
          title={state.kind === "forbidden" ? "Acces interzis" : "Eroare la încărcare"}
          detail={state.kind === "forbidden" ? "Nu ai drept de citire." : "Reîncearcă mai târziu."}
        />
      </BoPage>
    );
  }

  const { detail, runs, role } = state;
  const canWrite = role === "admin";
  const canSimulate = role === "admin" || role === "operator";

  return (
    <BoPage
      title={detail.bot.name}
      sub={detail.bot.description || undefined}
      actions={
        <>
          <Link href="/bo/bots" className="bo-btn">
            <IconBO name="arrow-left" size={15} /> Lista
          </Link>
          <button
            type="button"
            className="bo-btn bo-btn--primary"
            onClick={publish}
            disabled={!canWrite || busy !== null}
            title={!canWrite ? "Publicarea cere rol de administrator" : "Transformă ciorna în versiune activă imuabilă"}
          >
            <IconBO name="check" size={15} />
            {busy === "publish" ? "Se publică…" : "Publică ciorna"}
          </button>
        </>
      }
    >
      <div className="bo-row" style={{ marginBottom: 16 }}>
        <Pill kind={detail.bot.status === "active" ? "ok" : "neutral"} icon={detail.bot.status === "active" ? "check" : "clock"}>
          {detail.bot.status === "active" ? `activ · v${detail.bot.active_version_no}` : "ciornă — fără versiune activă"}
        </Pill>
        <Pill kind="neutral" icon="bot">{detail.bot.kind}</Pill>
        <Pill kind="neutral" icon="list">ciornă d{detail.bot.draft_version}</Pill>
      </div>

      {notice ? (
        <div style={{ marginBottom: 12 }}>
          <InlineAlert kind={notice.kind === "ok" ? "info" : notice.kind}>
            {notice.text}
          </InlineAlert>
        </div>
      ) : null}

      <div className="bo-grid bo-grid-2">
        {/* Editor ciornă */}
        <div className="bo-card">
          <h2 className="bo-card-title">Ciornă de lucru</h2>
          <p className="bo-hint" style={{ marginTop: 4 }}>
            Editarea ciornei nu atinge versiunea activă. Salvarea folosește
            expected_version — un conflict cere reîncărcare, nu suprascriere.
          </p>
          <div style={{ marginTop: 12, display: "grid", gap: 12 }}>
            <Field label="Nume" htmlFor="bbd-name">
              <input id="bbd-name" className="bo-input" value={name}
                onChange={(e) => setName(e.target.value)} disabled={!canWrite} />
            </Field>
            <Field label="Descriere" htmlFor="bbd-desc">
              <input id="bbd-desc" className="bo-input" value={description}
                onChange={(e) => setDescription(e.target.value)} disabled={!canWrite} />
            </Field>
            <Field label="Conținut (JSON)" htmlFor="bbd-content">
              <textarea id="bbd-content" className="bo-textarea"
                style={{ width: "100%", minHeight: 220 }}
                value={contentJson} onChange={(e) => setContentJson(e.target.value)}
                disabled={!canWrite} spellCheck={false} />
            </Field>
            <div>
              <button type="button" className="bo-btn" onClick={saveDraft}
                disabled={!canWrite || busy !== null}
                title={!canWrite ? "Editarea cere rol de administrator" : undefined}>
                {busy === "save" ? "Se salvează…" : "Salvează ciorna"}
              </button>
            </div>
            {!canWrite ? (
              <p className="bo-hint">Rolul tău permite doar citirea.</p>
            ) : null}
          </div>
        </div>

        {/* Simulare */}
        <div className="bo-card">
          <h2 className="bo-card-title">Simulare (dry-run)</h2>
          <p className="bo-hint" style={{ marginTop: 4 }}>
            Execută planul pe un context sintetic. Zero efecte externe —
            chitanțele sunt marcate NOT_EXECUTED.
          </p>
          <div style={{ marginTop: 12, display: "grid", gap: 12 }}>
            <Field label="Context de intrare (JSON)" htmlFor="bbs-input">
              <textarea id="bbs-input" className="bo-textarea"
                style={{ width: "100%", minHeight: 140 }}
                value={inputJson} onChange={(e) => setInputJson(e.target.value)}
                spellCheck={false} />
            </Field>
            <label className="bo-row" style={{ fontSize: 13 }}>
              <input type="checkbox" checked={useDraft}
                onChange={(e) => setUseDraft(e.target.checked)}
                disabled={!canSimulate} />
              Simulează ciorna (nu versiunea activă)
            </label>
            <div>
              <button type="button" className="bo-btn bo-btn--primary"
                onClick={simulate} disabled={!canSimulate || busy !== null}
                title={!canSimulate ? "Simularea cere rol de operator sau admin" : undefined}>
                <IconBO name="play" size={15} />
                {busy === "simulate" ? "Se simulează…" : "Simulează"}
              </button>
            </div>
            {lastRun ? (
              <div className="bo-alert bo-alert--info">
                <IconBO name="activity" size={16} />
                <div>
                  Ultima simulare: <strong>{lastRun.result?.execution_status}</strong>
                  {" · "}plan <span className="bo-mono">{shortHash(lastRun.plan_hash)}</span>
                  {" · "}
                  <Link href={`/bo/runs/${lastRun.id}`} style={{ color: "inherit", textDecoration: "underline" }}>
                    vezi timeline
                  </Link>
                </div>
              </div>
            ) : null}
          </div>
        </div>
      </div>

      {/* Versiuni */}
      <div className="bo-card" style={{ marginTop: 16 }}>
        <h2 className="bo-card-title">Versiuni</h2>
        <div className="bo-table-wrap" style={{ marginTop: 10 }}>
          <table className="bo-table" style={{ minWidth: 480 }}>
            <thead>
              <tr><th>Versiune</th><th>Stare</th><th>Hash conținut</th><th>Creat de</th><th>La</th></tr>
            </thead>
            <tbody>
              {detail.versions.map((v) => (
                <tr key={v.id}>
                  <td className="bo-mono">v{v.version_no}</td>
                  <td>
                    {v.status === "published" ? (
                      <Pill kind="ok" icon="check">publicat</Pill>
                    ) : (
                      <Pill kind="neutral" icon="clock">ciornă</Pill>
                    )}
                  </td>
                  <td className="bo-mono">{shortHash(v.hash)}</td>
                  <td className="bo-muted">{v.created_by}</td>
                  <td className="bo-muted">{new Date(v.created_at).toLocaleString("ro-RO")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Rulări */}
      <div className="bo-card" style={{ marginTop: 16 }}>
        <h2 className="bo-card-title">Rulări de simulare</h2>
        {runs.length === 0 ? (
          <p className="bo-hint" style={{ marginTop: 8 }}>
            Nicio rulare încă — pornește o simulare din panoul de mai sus.
          </p>
        ) : (
          <div className="bo-table-wrap" style={{ marginTop: 10 }}>
            <table className="bo-table">
              <thead>
                <tr><th>Pornit</th><th>Versiune</th><th>Rezultat</th><th>Plan</th><th>Config</th><th aria-label="Detalii" /></tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id}>
                    <td className="bo-muted">{new Date(r.started_at).toLocaleString("ro-RO")}</td>
                    <td className="bo-mono">v{r.version_no}</td>
                    <td>
                      <Pill kind={r.status === "SUCCEEDED" ? "ok" : r.status === "PARTIAL" ? "warn" : "danger"}>
                        {r.status}
                      </Pill>
                    </td>
                    <td className="bo-mono">{shortHash(r.plan_hash)}</td>
                    <td className="bo-mono">cv{r.config_version}</td>
                    <td>
                      <Link href={`/bo/runs/${r.id}`} className="bo-btn" aria-label="Deschide rularea">
                        Timeline <IconBO name="chevron-right" size={14} />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </BoPage>
  );
}
