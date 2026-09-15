"""Small, fail-closed RecipeStage materialization-gate policy."""

from __future__ import annotations

from shared.models.factory import AssemblyRecipeStage


PRE_ROOF_PASS_EXECUTION_GATE = "PRE_ROOF_PASS"


class UnsupportedExecutionGateError(RuntimeError):
    """Raised when a RecipeStage gate has no approved materialization semantics."""


def initial_materializable_stages(
    stages: list[AssemblyRecipeStage],
) -> list[AssemblyRecipeStage]:
    """Return only stages allowed at Job creation; never silently ignore a gate."""
    initial: list[AssemblyRecipeStage] = []
    for stage in stages:
        if stage.execution_gate is None:
            initial.append(stage)
        elif stage.execution_gate == PRE_ROOF_PASS_EXECUTION_GATE:
            continue
        else:
            raise UnsupportedExecutionGateError(
                f"Unsupported AssemblyRecipeStage.execution_gate={stage.execution_gate!r} "
                f"at stage_order={stage.stage_order}."
            )
    return initial
