"""mini-swe-agent policy that preserves a working patch at resource limits."""

from __future__ import annotations

from typing import Any

from minisweagent.agents.interactive import InteractiveAgent
from minisweagent.exceptions import LimitsExceeded, Submitted, TimeExceeded


class AutoSubmitInteractiveAgent(InteractiveAgent):
    """Auto-submit the current tracked-file diff before a limit destroys it."""

    def query(self) -> dict[str, Any]:
        try:
            return super().query()
        except (LimitsExceeded, TimeExceeded) as limit:
            output = self.env.execute({"command": "git diff -- ."})
            patch = output.get("output", "") if output.get("returncode") == 0 else ""
            if not patch.strip():
                raise
            limit_name = type(limit).__name__
            raise Submitted(
                {
                    "role": "exit",
                    "content": patch,
                    "extra": {
                        "exit_status": f"AutoSubmitted{limit_name}",
                        "submission": patch,
                    },
                }
            ) from limit
