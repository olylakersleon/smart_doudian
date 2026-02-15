#!/usr/bin/env python3
"""AutoWeb Bot: framework-adaptive WebDSL for dialogue-driven page operations.

Design goals:
- normalize operations into WebDSL actions (set_text/click/pick_date/select/observe)
- adapt popular UI frameworks (Ant Design, Element Plus, Layui, Vuetify, generic)
- improve action accuracy using selector candidate ranking + role fallback
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class PageElement:
    name: str
    selector: str
    role: str
    text: str = ""


@dataclass
class WebPageModel:
    page_name: str
    url: str
    framework: str
    elements: List[PageElement]


@dataclass
class UserIntent:
    action: str
    target_page: str
    filters: Dict[str, str]
    confidence: float = 0.7


@dataclass
class WebDSLAction:
    name: str
    args: Dict[str, str]


@dataclass
class ActionStep:
    op: str
    selector: str
    value: str = ""
    desc: str = ""


class LLMClient:
    def __init__(self, provider: str, model: str, endpoint: str, api_key: str, timeout_seconds: int = 25) -> None:
        self.provider = provider
        self.model = model
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def enabled(self) -> bool:
        return self.provider != "none" and bool(self.api_key)

    def ask(self, prompt: str) -> str:
        if not self.enabled():
            raise RuntimeError("LLM not configured")
        if self.provider == "openai":
            return self._openai(prompt)
        if self.provider == "gemini":
            return self._gemini(prompt)
        raise RuntimeError(f"unsupported provider: {self.provider}")

    def _openai(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a web automation planner."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()

    def _gemini(self, prompt: str) -> str:
        endpoint = self.endpoint
        if "key=" not in endpoint:
            endpoint = f"{endpoint}{'&' if '?' in endpoint else '?'}key={self.api_key}"
        req = urllib.request.Request(
            endpoint,
            data=json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()


class FrameworkDetector:
    def detect(self, raw_html: str) -> str:
        text = raw_html.lower()
        if "ant-" in text:
            return "antd"
        if "el-" in text:
            return "element-plus"
        if "layui" in text:
            return "layui"
        if "v-btn" in text or "v-text-field" in text:
            return "vuetify"
        return "generic"


class PageParser:
    def parse(self, snapshot_file: Path) -> WebPageModel:
        payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
        raw = payload.get("raw_html", "")
        framework = payload.get("framework") or FrameworkDetector().detect(raw)
        elements = [PageElement(**x) for x in payload.get("elements", [])]
        return WebPageModel(
            page_name=payload.get("page_name", "unknown"),
            url=payload.get("url", ""),
            framework=framework,
            elements=elements,
        )


class IntentParser:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def parse(self, utterance: str) -> UserIntent:
        if self.llm.enabled():
            prompt = (
                "将用户网页操作需求解析成JSON: action,target_page,filters,confidence(0~1)。"
                "action可选:view_order/filter_order/open_order_detail/filter_by_date/filter_by_status。"
                f"用户输入:{utterance}"
            )
            try:
                text = self.llm.ask(prompt)
                m = re.search(r"\{.*\}", text, flags=re.S)
                if m:
                    data = json.loads(m.group(0))
                    return UserIntent(
                        action=str(data.get("action", "view_order")),
                        target_page=str(data.get("target_page", "订单管理")),
                        filters={str(k): str(v) for k, v in dict(data.get("filters", {})).items()},
                        confidence=float(data.get("confidence", 0.8)),
                    )
            except Exception:
                pass

        action = "view_order"
        filters: Dict[str, str] = {}
        conf = 0.75

        if "详情" in utterance:
            action = "open_order_detail"
            conf += 0.05
        if "筛选" in utterance or "过滤" in utterance:
            action = "filter_order"
        m_order = re.search(r"(订单号|order)\s*[:：]?\s*([A-Za-z0-9_-]+)", utterance)
        if m_order:
            filters["order_id"] = m_order.group(2)
            conf += 0.1

        m_date = re.search(r"(\d{4}-\d{2}-\d{2})", utterance)
        if m_date:
            filters["date"] = m_date.group(1)
            action = "filter_by_date"
            conf += 0.1

        m_status = re.search(r"(待发货|待付款|已完成|已取消)", utterance)
        if m_status:
            filters["status"] = m_status.group(1)
            action = "filter_by_status"
            conf += 0.08

        return UserIntent(action=action, target_page="订单管理", filters=filters, confidence=min(conf, 0.98))


class UIFwkSupport:
    """Component interaction best-practice adapter.

    Best-practice notes:
    - Prefer explicit role-provided selector first.
    - Then fallback to framework-common stable selectors.
    - Then fallback to generic ARIA/data-testid selectors.
    """

    def _framework_candidates(self, framework: str) -> Dict[str, List[str]]:
        generic = {
            "search_input": ["input[placeholder*='订单']", "input[aria-label*='订单']", "[data-testid='order-search-input']"],
            "search_button": ["button:has-text('搜索')", "button[aria-label='search']", "[data-testid='order-search-btn']"],
            "order_row": ["table tr:nth-child(1)", "[data-testid='order-row-0']"],
            "detail_button": ["button:has-text('详情')", "[data-testid='order-detail-btn']"],
            "date_input": ["input[placeholder*='日期']", "input[aria-label*='date']", "[data-testid='date-input']"],
            "date_confirm": ["button:has-text('确定')", "button:has-text('OK')", "[data-testid='date-confirm']"],
            "status_select": ["[data-testid='order-status-select']", "select[name='status']"],
        }
        if framework == "antd":
            generic["date_input"] = [".ant-picker-input input", ".ant-picker input"] + generic["date_input"]
            generic["date_confirm"] = [".ant-picker-ok .ant-btn-primary", ".ant-picker-ok button"] + generic["date_confirm"]
            generic["status_select"] = [".ant-select-selector", "#order-status"] + generic["status_select"]
        elif framework == "element-plus":
            generic["date_input"] = [".el-date-editor .el-input__inner", ".el-date-editor input"] + generic["date_input"]
            generic["date_confirm"] = [".el-picker-panel__footer .el-button--primary"] + generic["date_confirm"]
            generic["status_select"] = [".el-select .el-input__inner", "#order-status"] + generic["status_select"]
        elif framework == "layui":
            generic["date_input"] = ["input[lay-key]", ".layui-input"] + generic["date_input"]
            generic["date_confirm"] = [".laydate-btns-confirm"] + generic["date_confirm"]
            generic["status_select"] = [".layui-form-select"] + generic["status_select"]
        return generic

    def resolve(self, page: WebPageModel) -> Dict[str, str]:
        by_role = {x.role: x.selector for x in page.elements}
        known_selectors = {x.selector for x in page.elements}
        cands = self._framework_candidates(page.framework)
        resolved: Dict[str, str] = {}
        for role, options in cands.items():
            if role in by_role:
                resolved[role] = by_role[role]
                continue
            hit = next((s for s in options if s in known_selectors), None)
            resolved[role] = hit or options[0]
        return resolved

    def date_steps(self, framework: str, selectors: Dict[str, str], date_val: str) -> List[ActionStep]:
        return [
            ActionStep("click", selectors["date_input"], desc=f"打开{framework}日期控件"),
            ActionStep("type", selectors["date_input"], value=date_val, desc="输入日期"),
            ActionStep("click", selectors["date_confirm"], desc="确认日期"),
        ]


class WebDSLPlanner:
    def from_intent(self, intent: UserIntent) -> List[WebDSLAction]:
        dsl: List[WebDSLAction] = []
        if "order_id" in intent.filters:
            dsl += [
                WebDSLAction("set_text", {"target": "search_input", "value": intent.filters["order_id"]}),
                WebDSLAction("click", {"target": "search_button"}),
            ]
        if intent.action == "open_order_detail":
            dsl += [WebDSLAction("click", {"target": "order_row"}), WebDSLAction("click", {"target": "detail_button"})]
        if intent.action == "filter_by_date" and "date" in intent.filters:
            dsl += [WebDSLAction("pick_date", {"target": "date_input", "value": intent.filters["date"]})]
            dsl += [WebDSLAction("click", {"target": "search_button"})]
        if intent.action == "filter_by_status" and "status" in intent.filters:
            dsl += [
                WebDSLAction("set_text", {"target": "status_select", "value": intent.filters["status"]}),
                WebDSLAction("click", {"target": "search_button"}),
            ]
        if not dsl:
            dsl.append(WebDSLAction("observe", {"target": "order_row"}))
        return dsl


class ActionPlanner:
    def __init__(self) -> None:
        self.fwk = UIFwkSupport()
        self.dsl = WebDSLPlanner()

    def plan(self, page: WebPageModel, intent: UserIntent) -> Tuple[List[ActionStep], float]:
        selectors = self.fwk.resolve(page)
        dsl_actions = self.dsl.from_intent(intent)
        steps: List[ActionStep] = []
        for action in dsl_actions:
            target = action.args.get("target", "")
            if action.name == "set_text":
                steps.append(ActionStep("type", selectors[target], action.args.get("value", ""), f"输入 {target}"))
            elif action.name == "click":
                steps.append(ActionStep("click", selectors[target], desc=f"点击 {target}"))
            elif action.name == "pick_date":
                steps.extend(self.fwk.date_steps(page.framework, selectors, action.args.get("value", "")))
            elif action.name == "observe":
                steps.append(ActionStep("observe", selectors.get(target, target), desc="观察目标区域"))
        confidence = min(0.99, 0.6 + 0.05 * len(steps) + 0.2 * intent.confidence)
        return steps, confidence


class ActionExecutor:
    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run

    def execute(self, steps: List[ActionStep]) -> List[str]:
        logs: List[str] = []
        mode = "dry-run" if self.dry_run else "exec"
        for i, step in enumerate(steps, start=1):
            logs.append(f"[{mode}#{i}] {step.op} {step.selector} value={step.value} desc={step.desc}")
        return logs


class AutoWebConversationBot:
    def __init__(self, llm: LLMClient, dry_run: bool = True) -> None:
        self.page_parser = PageParser()
        self.intent_parser = IntentParser(llm)
        self.planner = ActionPlanner()
        self.executor = ActionExecutor(dry_run=dry_run)

    def run(self, snapshot_file: Path, utterance: str) -> Dict[str, object]:
        page = self.page_parser.parse(snapshot_file)
        intent = self.intent_parser.parse(utterance)
        steps, score = self.planner.plan(page, intent)
        logs = self.executor.execute(steps)
        return {
            "page": page.page_name,
            "framework": page.framework,
            "intent": intent.__dict__,
            "plan_confidence": round(score, 3),
            "steps": [s.__dict__ for s in steps],
            "logs": logs,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AutoWeb conversation bot")
    parser.add_argument("--snapshot", default="prototype/order_page_snapshot.json")
    parser.add_argument("--utterance", required=True)
    parser.add_argument("--llm-provider", default="none", choices=["none", "openai", "gemini"])
    parser.add_argument("--llm-model", default="gpt-4o-mini")
    parser.add_argument("--llm-endpoint", default="https://api.openai.com/v1/chat/completions")
    parser.add_argument("--llm-api-key", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = args.llm_api_key or os.getenv("OPENAI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    llm = LLMClient(args.llm_provider, args.llm_model, args.llm_endpoint, api_key)
    bot = AutoWebConversationBot(llm=llm, dry_run=args.dry_run)
    result = bot.run(Path(args.snapshot), args.utterance)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
