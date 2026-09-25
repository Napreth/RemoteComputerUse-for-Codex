import copy
import json
from functools import lru_cache

from .config import DEFAULT_MODEL_DISPLAY_NAME, REPO_ROOT
from .models import ScenarioManifest, validate_wire


@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads((REPO_ROOT / "labs" / "catalog.json").read_text(encoding="utf-8"))


def list_scenarios() -> list[dict]:
    return [validate_wire(ScenarioManifest, {**item, "workspaceTemplatePath": str(REPO_ROOT / "labs" / item["templateDirectory"])})
            for item in catalog()["scenarios"]]


def instructions(lab_id: str, current_url: str) -> str:
    definition = next(item for item in catalog()["scenarios"] if item["labId"] == lab_id)
    return "\n".join([
        f"You are operating an isolated remote computer for a {DEFAULT_MODEL_DISPLAY_NAME} CUA demo harness.",
        "You must inspect the remote framebuffer through exec_py before you answer.",
        "Use the persistent computer object from exec_py for all screenshots and input. Coordinates refer to screenshot pixels.",
        f"The lab was requested at {current_url}. Inspect the screenshot to confirm it is visible.",
        *copy.deepcopy(definition["instructions"]),
    ])
