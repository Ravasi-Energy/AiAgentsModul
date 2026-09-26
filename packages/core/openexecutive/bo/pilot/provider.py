"""Bounded HTTP diagnostic; the service is synthetic and explicitly allowlisted."""
import json
import os
import re
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from openexecutive.bo.db import get_conn
from openexecutive.bo.execution import store
from openexecutive.bo.execution.synth import ProviderError, ProviderTimeout
from openexecutive.bo.pilot import service
from openexecutive.bo.routing import store as outbox


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PilotProvider:
    name = service.RESOURCE
    idempotent = True
    # Resume performs readback only. An uncertain effect is never resubmitted.
    retry_unknown = False

    def __init__(self, tenant, run, db_path=None):
        self.tenant, self.run, self.db_path = tenant, run, db_path

    def _request(self, path, body=None):
        try:
            if body is None:
                # Readback is not a new effect: preserve recovery after uninstall.
                from openexecutive.bo.pilot.config import endpoint
                config = service.configuration(self.tenant, self.db_path)
                target = endpoint(config["endpoint"])
                if config["profile"] != "synthetic-loopback" or not target \
                        or target not in json.loads(config["allowlist"]) \
                        or target != self.run["steps"][0]["payload"].get("endpoint"):
                    raise ProviderError("Readback cere endpointul original încă allowlisted")
            else:
                config, _, _ = service.guard(self.tenant, self.run["steps"][0]["payload"], self.db_path)
            if body is not None and config["supervision"] == "required" and not any(
                m.guardian_ref for m in store.mandate_chain(self.tenant, self.run["mandate_id"], db_path=self.db_path)
            ):
                raise ProviderError("Autoritate Guardian obligatorie pentru pilot")
            token = os.environ.get(config["secret_ref"], "")
            if not token:
                raise ProviderError("Secretul serviciului sintetic lipsește")
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("Configurație/activare pilot invalidă sau modificată") from None
        request = Request(config["endpoint"] + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=config["timeout_s"]) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError
                return json.loads(raw)
        except HTTPError as exc:
            if exc.code in (401, 403, 409) or 300 <= exc.code < 400:
                raise ProviderError(f"Serviciul sintetic refuză cererea ({exc.code})") from None
            raise ProviderTimeout("Serviciul sintetic nu furnizează dovadă") from None
        except Exception:
            raise ProviderTimeout("Răspuns sintetic absent sau invalid") from None

    def _accept(self, response, key, digest):
        # No raw service payload enters logs, UI or the shared outbox.
        observation = response.get("observation") if isinstance(response, dict) else None
        if observation:
            self._observation(observation)
        receipt = response.get("receipt") if isinstance(response, dict) else None
        if not isinstance(receipt, dict) or receipt.get("tenant") != self.tenant \
                or receipt.get("effect_key") != key or receipt.get("digest") != digest \
                or receipt.get("provider") != self.name or receipt.get("amount") != 1 \
                or not re.fullmatch(r"rcp_[a-z0-9]{1,60}", str(receipt.get("receipt_ref", ""))):
            raise ProviderTimeout("Succesul declarat nu are receipt corelat; UNKNOWN")
        try:
            stamp = datetime.fromisoformat(receipt["received_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp > datetime.now(UTC):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise ProviderTimeout("Receipt fără timestamp valid") from None
        return {k: receipt[k] for k in ("receipt_ref", "provider", "effect_key", "digest", "amount", "received_at")}

    def _observation(self, event):
        # Contract published by SOL-03: exact service-observation envelope.
        from openexecutive.bo.pilot.observation import validate
        validate(event, self.tenant, self.run)
        with get_conn(self.db_path) as conn:
            conn.execute("""INSERT INTO bo_pilot_observations(tenant,run_id,event_json)
                VALUES (?,?,?) ON CONFLICT(tenant,run_id) DO NOTHING""",
                (self.tenant, self.run["run_id"], json.dumps(event)))
        outbox.enqueue_outbox(self.tenant, "service", event["eventId"], event, db_path=self.db_path)
        from openexecutive.bo.telemetry.schema import validate_event
        for kind, data in [
            ("Heartbeat", {"sequence": int(datetime.fromisoformat(event["observedAt"].replace("Z", "+00:00")).timestamp() * 1000),
                "status": "DEGRADED" if event["service"]["queuePending"] else "HEALTHY", "observedAt": event["observedAt"]}),
            ("RunFinished", {"executionStatus": "SUCCEEDED", "verificationStatus": "UNKNOWN"}),
        ]:
            telemetry = {k: event[k] for k in ("producerId", "installationId", "tenantRef", "product", "correlationId")}
            telemetry.update(schemaVersion="bo.telemetry.v1", eventId=event["eventId"] + ("-hb" if kind == "Heartbeat" else "-run"),
                kind=kind, occurredAt=event["observedAt"], agentRef="synthetic-erp", runRef=self.run["run_id"],
                configVersion=self.run["steps"][0]["payload"]["config_hash"], data=data)
            validate_event(telemetry)
            outbox.enqueue_outbox(self.tenant, "pilot-telemetry", telemetry["eventId"], telemetry, db_path=self.db_path)

    def submit(self, *, tenant, idempotency_key, payload_digest, amount):
        if tenant != self.tenant or amount != 1:
            raise ProviderError("Scope diagnostic invalid")
        response = self._request("/probe", {
            "tenantRef": tenant,
            "idempotencyKey": idempotency_key, "payloadDigest": payload_digest,
            "executionRef": self.run["run_id"], "correlationId": self.run["correlation_id"],
            "producerId": os.environ.get("BO_TELEMETRY_PRODUCER_ID", "boagents"),
            "installationId": os.environ.get("BO_INSTALLATION_ID", "local-installation"),
        })
        return self._accept(response, idempotency_key, payload_digest)

    def receipt_for(self, *, tenant, idempotency_key):
        if tenant != self.tenant:
            return None
        entries = store.list_ledger(tenant, self.run["run_id"], db_path=self.db_path)
        entry = next((e for e in entries if e["idempotency_key"] == idempotency_key), None)
        if not entry:
            return None
        try:
            return self._accept(self._request("/receipts/" + quote(idempotency_key, safe="")),
                                idempotency_key, entry["payload_digest"])
        except (ProviderError, ProviderTimeout, ValueError):
            return None
