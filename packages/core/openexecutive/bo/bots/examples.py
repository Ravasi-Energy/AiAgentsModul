"""Synthetic BoBot examples for the Valul 1 slice.

``HEARTBEAT_STALE`` is the mandated demo: a service whose heartbeat is older
than a threshold produces a VerificationFinding — inside the simulator, with
zero external effects.
"""
from __future__ import annotations

from typing import Any

HEARTBEAT_STALE: dict[str, Any] = {
    "name": "Heartbeat vechi — serviciu",
    "description": "Semnalează un serviciu al cărui ultim heartbeat depășește pragul.",
    "kind": "BOT",
    "content": {
        "schema_version": "bo.bobot.v1",
        "trigger": {"type": "event", "event": "heartbeat.stale"},
        "predicates": {
            "all": [
                {"op": "exists", "path": "service.name"},
                {"op": "gt", "path": "service.last_heartbeat_age_minutes",
                 "value": 30},
            ]
        },
        "steps": [
            {
                "id": "check_age",
                "type": "check",
                "predicate": {
                    "op": "gt",
                    "path": "service.last_heartbeat_age_minutes",
                    "value": 30,
                },
                "on_false": "stop",
            },
            {
                "id": "flag",
                "type": "emit_finding",
                "finding_key": "heartbeat-stale",
                "category": "availability",
                "severity": "MEDIUM",
                "message": "Serviciul {service.name} nu a mai trimis heartbeat "
                           "de {service.last_heartbeat_age_minutes} minute",
            },
            {
                "id": "mark",
                "type": "set_field",
                "path": "findings.heartbeat_stale",
                "value": True,
            },
            {
                "id": "notify",
                "type": "notify",
                "channel": "internal",
                "message": "Ar fi notificat ownerul serviciului {service.name} "
                           "(doar simulat)",
            },
        ],
        "capability_refs": [],
        "policy_refs": [],
    },
}
