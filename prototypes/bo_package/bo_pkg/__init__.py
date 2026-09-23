"""Prototip izolat VAL2-00: pachete BO semnate (bo.package.v1).

NU este funcționalitate de produs și NU este activată în runtime.
Nu importă și nu este importat de openexecutive.
"""

from bo_pkg.contract import validate_manifest
from bo_pkg.errors import PackageReject
from bo_pkg.registry import TrustRegistry
from bo_pkg.verify import Verdict, verify_package

__all__ = [
    "PackageReject",
    "TrustRegistry",
    "Verdict",
    "validate_manifest",
    "verify_package",
]
