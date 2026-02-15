#!/usr/bin/env python3
"""AutoWeb Bot: framework-aware WebDSL for dialogue-driven web operations."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


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


@dataclass
class ActionStep:
    op: str
    selector: str
    value: str = ""
    desc: str = ""


@dataclass
class WebDSLAction:
    name: str
    args: Dict[str, str]


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
                {"role": "system", "content": "You are a web automation DSL planner."},
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
        if "mui-" in text:
            return "mui"
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
                "将用户操作需求解析为JSON，字段 action,target_page,filters。"
                "action 可选：view_order/filter_order/open_order_detail/filter_by_date。"
                f"用户输入：{utterance}"
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
                    )
            except Exception:
                pass

        filters: Dict[str, str] = {}
        action = "view_order"
        if "详情" in utterance:
            action = "open_order_detail"
        if "筛选" in utterance or "过滤" in utterance:
            action = "filter_order"
        if "日期" in utterance or "时间" in utterance:
            action = "filter_by_date"

        m_order = re.search(r"(订单号|order)\s*[:：]?\s*([A-Za-z0-9_-]+)", utterance)
        if m_order:
            filters["order_id"] = m_order.group(2)

        m_date = re.search(r"(\d{4}-\d{2}-\d{2})", utterance)
        if m_date:
            filters["date"] = m_date.group(1)
            action = "filter_by_date"

        return UserIntent(action=action, target_page="订单管理", filters=filters)


class UIFwkSupport:
    """Framework component adapters for standardized WebDSL actions."""

    def selectors(self, page: WebPageModel) -> Dict[str, str]:
        by_role = {e.role: e.selector for e in page.elements}
        base = {
            "search_input": by_role.get("search_input", "input[placeholder*='订单']"),
            "search_button": by_role.get("search_button", "button:has-text('搜索')"),
            "order_row": by_role.get("order_row", "table tr:nth-child(1)"),
            "detail_button": by_role.get("detail_button", "button:has-text('详情')"),
            "date_input": by_role.get("date_input", "input[placeholder*='日期']"),
            "date_confirm": by_role.get("date_confirm", "button:has-text('确定')"),
        }

        framework = page.framework
        if framework == "antd":
            base["date_input"] = by_role.get("date_input", ".ant-picker input")
            base["date_confirm"] = by_role.get("date_confirm", ".ant-picker-ok button")
        elif framework == "element-plus":
            base["date_input"] = by_role.get("date_input", ".el-date-editor input")
            base["date_confirm"] = by_role.get("date_confirm", ".el-picker-panel__footer .el-button--primary")
        elif framework == "layui":
            base["date_input"] = by_role.get("date_input", "input[lay-key]")
            base["date_confirm"] = by_role.get("date_confirm", ".laydate-btns-confirm")
        return base

    def date_pick_steps(self, framework: str, selectors: Dict[str, str], date_value: str) -> List[ActionStep]:
        if framework == "antd":
            return [
                ActionStep("click", selectors["date_input"], desc="打开 AntD 日期面板"),
                ActionStep("type", selectors["date_input"], value=date_value, desc="输入日期"),
                ActionStep("click", selectors["date_confirm"], desc="确认日期"),
            ]
        if framework == "element-plus":
            return [
                ActionStep("click", selectors["date_input"], desc="打开 Element 日期面板"),
                ActionStep("type", selectors["date_input"], value=date_value, desc="输入日期"),
                ActionStep("click", selectors["date_confirm"], desc="确认日期"),
            ]
        if framework == "layui":
            return [
                ActionStep("click", selectors["date_input"], desc="打开 Layui 日期面板"),
                ActionStep("type", selectors["date_input"], value=date_value, desc="输入日期"),
                ActionStep("click", selectors["date_confirm"], desc="确认日期"),
            ]
        return [ActionStep("type", selectors["date_input"], value=date_value, desc="通用日期输入")]


class WebDSLPlanner:
    def to_dsl(self, intent: UserIntent) -> List[WebDSLAction]:
        dsl: List[WebDSLAction] = []
        if "order_id" in intent.filters:
            dsl.append(WebDSLAction("set_text", {"target": "search_input", "value": intent.filters["order_id"]}))
            dsl.append(WebDSLAction("click", {"target": "search_button"}))
        if intent.action == "open_order_detail":
            dsl.append(WebDSLAction("click", {"target": "order_row"}))
            dsl.append(WebDSLAction("click", {"target": "detail_button"}))
        if intent.action == "filter_by_date" and "date" in intent.filters:
            dsl.append(WebDSLAction("pick_date", {"target": "date_input", "value": intent.filters["date"]}))
            dsl.append(WebDSLAction("click", {"target": "search_button"}))
        if not dsl:
            dsl.append(WebDSLAction("observe", {"target": "order_row"}))
        return dsl


class ActionPlanner:
    def __init__(self) -> None:
        self.fwk = UIFwkSupport()
        self.dsl_planner = WebDSLPlanner()

    def plan(self, page: WebPageModel, intent: UserIntent) -> List[ActionStep]:
        sel = self.fwk.selectors(page)
        dsl = self.dsl_planner.to_dsl(intent)
        steps: List[ActionStep] = []
        for action in dsl:
            target = action.args.get("target", "")
            if action.name == "set_text":
                steps.append(ActionStep("type", sel[target], action.args.get("value", ""), f"输入 {target}"))
            elif action.name == "click":
                steps.append(ActionStep("click", sel[target], desc=f"点击 {target}"))
            elif action.name == "pick_date":
                steps.extend(self.fwk.date_pick_steps(page.framework, sel, action.args.get("value", "")))
            elif action.name == "observe":
                steps.append(ActionStep("observe", sel.get(target, target), desc="观察目标区域"))
        return steps


class ActionExecutor:
    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run

    def execute(self, steps: List[ActionStep]) -> List[str]:
        out: List[str] = []
        for i, step in enumerate(steps, start=1):
            prefix = "dry-run" if self.dry_run else "exec"
            out.append(f"[{prefix}#{i}] {step.op} {step.selector} value={step.value} desc={step.desc}")
        return out


class AutoWebConversationBot:
    def __init__(self, llm: LLMClient, dry_run: bool = True) -> None:
        self.page_parser = PageParser()
        self.intent_parser = IntentParser(llm)
        self.planner = ActionPlanner()
        self.executor = ActionExecutor(dry_run=dry_run)

    def run(self, snapshot_file: Path, utterance: str) -> Dict[str, object]:
        page = self.page_parser.parse(snapshot_file)
        intent = self.intent_parser.parse(utterance)
        steps = self.planner.plan(page, intent)
        logs = self.executor.execute(steps)
        return {
            "page": page.page_name,
            "framework": page.framework,
            "intent": intent.__dict__,
            "steps": [x.__dict__ for x in steps],
            "logs": logs,
        }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run AutoWeb conversation module")
    p.add_argument("--snapshot", default="prototype/order_page_snapshot.json")
    p.add_argument("--utterance", required=True)
    p.add_argument("--llm-provider", default="none", choices=["none", "openai", "gemini"])
    p.add_argument("--llm-model", default="gpt-4o-mini")
    p.add_argument("--llm-endpoint", default="https://api.openai.com/v1/chat/completions")
    p.add_argument("--llm-api-key", default="")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    api_key = args.llm_api_key or os.getenv("OPENAI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    llm = LLMClient(args.llm_provider, args.llm_model, args.llm_endpoint, api_key)
    bot = AutoWebConversationBot(llm=llm, dry_run=args.dry_run)
    result = bot.run(Path(args.snapshot), args.utterance)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
