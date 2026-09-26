"""Observation-only delivery through the existing durable outbox."""
import json
import os
from urllib.request import ProxyHandler, Request, build_opener

from openexecutive.bo.execution.guardian import _link_config
from openexecutive.bo.pilot.provider import NoRedirect
from openexecutive.bo.telemetry.adapter import TelemetryDisabledError, get_adapter


def deliver(tenant, envelope, db_path=None):
    if not get_adapter().enabled:
        raise TelemetryDisabledError("telemetry disabled")
    base, _, timeout, _ = _link_config(tenant, db_path)
    if not base:
        raise ValueError("Endpoint Guardian neconfigurat")
    service = envelope["schemaVersion"] == "bo.service-observation.v1"
    token = os.environ.get("BO_PILOT_OBSERVATION_TOKEN" if service else "BO_TELEMETRY_TOKEN", "")
    if not token:
        raise ValueError("Credential observație/telemetrie neconfigurat")
    request = Request(base + ("/v1/observations" if service else "/v1/telemetry/events"),
                      data=json.dumps(envelope).encode(),
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError
            return json.loads(raw)
    except Exception:
        # Never persist transport exception text that might echo credentials.
        raise ValueError("Livrarea observației sintetice nu a fost confirmată") from None
