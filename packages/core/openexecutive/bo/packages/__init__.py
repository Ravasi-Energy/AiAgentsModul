"""BO package import slice (VAL2-01): contract bo.package.v1 + import→verify→quarantine.

Implements the coordinator-ratified contract decision
(CONTRACT-PACHETE-DECIZIE-V1): canonicalization BO-C14N-v1, Ed25519 signatures,
tenant trust store, quarantine/draft lifecycle. No external effects: import
reads a server-local directory and writes only to the BO database and the
quarantine content store.
"""
