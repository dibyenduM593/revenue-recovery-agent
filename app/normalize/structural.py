"""Stage 1: structural mapping.

Pulls values out of a provider's raw, nested payload using the dot-paths
configured in field_mappings, and applies each mapping's named transform.
Different entity kinds under the same provider (payment vs invoice vs
subscription vs refund, in Razorpay's case) nest under different top-level
keys, so their field_mappings rows simply live side by side: a path that
does not exist in a given event's payload is skipped, not an error, since
it just means that mapping does not apply to this event's shape.
"""

from typing import Any

from app.canonical.vocabulary import NormalizationStage
from app.models import FieldMapping
from app.normalize.transforms import TRANSFORMS

_MISSING = object()


class NormalizationError(Exception):
    def __init__(self, stage: NormalizationStage, message: str):
        self.stage = stage
        self.message = message
        super().__init__(f"[{stage.value}] {message}")


def _get_path(payload: dict[str, Any], path: str) -> Any:
    node: Any = payload
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    return node


def apply_mapping(payload: dict[str, Any], mappings: list[FieldMapping]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for mapping in mappings:
        value = _get_path(payload, mapping.source_field)
        if value is _MISSING or value is None:
            continue

        transform = TRANSFORMS.get(mapping.transform)
        if transform is None:
            raise NormalizationError(
                NormalizationStage.STRUCTURAL,
                f"unknown transform {mapping.transform!r} for source_field {mapping.source_field!r}",
            )
        try:
            result[mapping.canonical_field] = transform(value)
        except Exception as exc:
            raise NormalizationError(
                NormalizationStage.STRUCTURAL,
                f"transform {mapping.transform!r} failed on {mapping.source_field!r}: {exc}",
            ) from exc

    return result
