#!/usr/bin/env python3
"""Long-task AutoWeb agent: compile dialogue request to reusable task code.

Key features:
- convert one multi-step instruction into a long task plan
- generate runnable python task code and save to local cache
- reuse cached task code for identical instruction to skip re-planning
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from web_autoweb_bot import AutoWebConversationBot, LLMClient


@dataclass
class TaskStep:
    page_snapshot: str
    utterance: str


@dataclass
class LongTaskPlan:
    task_id: str
    instruction: str
    steps: List[TaskStep]


class LongTaskPlanner:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def plan(self, instruction: str, snapshots: List[str]) -> LongTaskPlan:
        task_id = hashlib.sha1((instruction + "|" + "|".join(snapshots)).encode("utf-8")).hexdigest()[:12]
        steps = self._rule_plan(instruction, snapshots)
        return LongTaskPlan(task_id=task_id, instruction=instruction, steps=steps)

    def _rule_plan(self, instruction: str, snapshots: List[str]) -> List[TaskStep]:
        parts = [p.strip() for p in instruction.replace("；", "，").split("然后") if p.strip()]
        if not parts:
            parts = [instruction]
        steps: List[TaskStep] = []
        for idx, part in enumerate(parts):
            snapshot = snapshots[idx] if idx < len(snapshots) else snapshots[-1]
            steps.append(TaskStep(page_snapshot=snapshot, utterance=part))
        return steps


class TaskCodeCompiler:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def compile_and_save(self, plan: LongTaskPlan) -> Dict[str, Path]:
        py_path = self.cache_dir / f"task_{plan.task_id}.py"
        meta_path = self.cache_dir / f"task_{plan.task_id}.json"

        code = self._render_code(plan)
        py_path.write_text(code, encoding="utf-8")
        meta_path.write_text(
            json.dumps(
                {
                    "task_id": plan.task_id,
                    "instruction": plan.instruction,
                    "steps": [s.__dict__ for s in plan.steps],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return {"code": py_path, "meta": meta_path}

    def find_cached(self, instruction: str, snapshots: List[str]) -> Dict[str, Path] | None:
        task_id = hashlib.sha1((instruction + "|" + "|".join(snapshots)).encode("utf-8")).hexdigest()[:12]
        py_path = self.cache_dir / f"task_{task_id}.py"
        meta_path = self.cache_dir / f"task_{task_id}.json"
        if py_path.exists() and meta_path.exists():
            return {"code": py_path, "meta": meta_path}
        return None

    @staticmethod
    def _render_code(plan: LongTaskPlan) -> str:
        step_lines = ",\n        ".join(
            [
                "{"
                f"'page_snapshot': {step.page_snapshot!r}, "
                f"'utterance': {step.utterance!r}"
                "}"
                for step in plan.steps
            ]
        )
        return f'''#!/usr/bin/env python3
"""Generated long task: {plan.task_id}"""

TASK_ID = {plan.task_id!r}
INSTRUCTION = {plan.instruction!r}
STEPS = [
        {step_lines}
]

def get_task_spec():
    return {{"task_id": TASK_ID, "instruction": INSTRUCTION, "steps": STEPS}}
'''


class LongTaskExecutor:
    def __init__(self, bot: AutoWebConversationBot) -> None:
        self.bot = bot

    def execute_meta(self, meta_file: Path) -> List[Dict[str, object]]:
        payload = json.loads(meta_file.read_text(encoding="utf-8"))
        outputs: List[Dict[str, object]] = []
        for step in payload.get("steps", []):
            out = self.bot.run(Path(step["page_snapshot"]), step["utterance"])
            outputs.append(
                {
                    "page_snapshot": step["page_snapshot"],
                    "utterance": step["utterance"],
                    "result": out,
                }
            )
        return outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile and execute reusable AutoWeb long task")
    parser.add_argument("--instruction", required=True, help="multi-page long task instruction")
    parser.add_argument(
        "--snapshots",
        nargs="+",
        required=True,
        help="ordered snapshot files, each step binds to one snapshot",
    )
    parser.add_argument("--cache-dir", default="prototype/generated_tasks")
    parser.add_argument("--llm-provider", default="none", choices=["none", "openai", "gemini"])
    parser.add_argument("--llm-model", default="gpt-4o-mini")
    parser.add_argument("--llm-endpoint", default="https://api.openai.com/v1/chat/completions")
    parser.add_argument("--llm-api-key", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    llm = LLMClient(args.llm_provider, args.llm_model, args.llm_endpoint, args.llm_api_key)
    planner = LongTaskPlanner(llm)
    compiler = TaskCodeCompiler(Path(args.cache_dir))

    cached = compiler.find_cached(args.instruction, args.snapshots)
    if cached is None:
        plan = planner.plan(args.instruction, args.snapshots)
        cached = compiler.compile_and_save(plan)
        print(f"[LongTask] compiled new task: {cached['code']}")
    else:
        print(f"[LongTask] hit cache, reuse: {cached['code']}")

    bot = AutoWebConversationBot(llm=llm, dry_run=args.dry_run)
    outputs = LongTaskExecutor(bot).execute_meta(cached["meta"])
    print(json.dumps({"task_files": {k: str(v) for k, v in cached.items()}, "outputs": outputs}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
