from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from composition_correction_agent.agent import (  # noqa: E402
    CompositionCorrectionInput,
    run_sample_workflow,
)


def main() -> None:
    image_path = REPO_ROOT / "samples" / "handwritten_english_composition.svg"
    result = run_sample_workflow(
        CompositionCorrectionInput(image_path=str(image_path)),
        storage_dir=REPO_ROOT / "tmp" / "composition_correction_records",
        human_review_response="APPROVE",
        log_steps=True,
    )

    print("\nFINAL_CORRECTION_REPORT")
    print(json.dumps(result.report.model_dump(mode="json"), indent=2))
    print(f"\nPERSISTED_RECORD_PATH\n{result.record.path}")


if __name__ == "__main__":
    main()
