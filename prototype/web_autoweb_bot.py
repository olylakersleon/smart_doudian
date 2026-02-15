#!/usr/bin/env python3
"""AutoWeb Bot module: framework-aware page parsing + dialogue-driven web actions."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
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
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()


class FrameworkDetector:
    def detect(self, raw: str) -> str:
        text = raw.lower()
        if "ant-table" in text or "ant-btn" in text:
            return "antd"
        if "el-table" in text or "el-button" in text:
            return "element-plus"
        if "layui-table" in text:
            return "layui"
        return "generic"


class PageParser:
    def parse(self, snapshot_file: Path) -> WebPageModel:
        payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
        detector = FrameworkDetector()
        raw = payload.get("raw_html", "")
        framework = payload.get("framework") or detector.detect(raw)
        elements = [PageElement(**x) for x in payload.get("elements", [])]
        return WebPageModel(
            page_name=payload.get("page_name", "unknown_page"),
            url=payload.get("url", ""),
            framework=framework,
            elements=elements,
        )


class IntentParser:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def parse(self, user_utterance: str) -> UserIntent:
        if self.llm.enabled():
            prompt = (
                "将用户网页操作需求解析成JSON，字段: action,target_page,filters。"
                f"用户输入: {user_utterance}"
            )
            try:
                resp = self.llm.ask(prompt)
                m = re.search(r"\{.*\}", resp, flags=re.S)
                if m:
                    data = json.loads(m.group(0))
                    return UserIntent(
                        action=str(data.get("action", "view_order")),
                        target_page=str(data.get("target_page", "订单管理")),
                        filters={str(k): str(v) for k, v in dict(data.get("filters", {})).items()},
                    )
            except Exception:
                pass

        order_id = ""
        m_order = re.search(r"(订单号|order)\s*[:：]?\s*([A-Za-z0-9_-]+)", user_utterance)
        if m_order:
            order_id = m_order.group(2)
        action = "view_order"
        if "筛选" in user_utterance or "过滤" in user_utterance:
            action = "filter_order"
        if "详情" in user_utterance:
            action = "open_order_detail"
        return UserIntent(action=action, target_page="订单管理", filters={"order_id": order_id} if order_id else {})


class FrameworkActionAdapter:
    def selectors(self, page: WebPageModel) -> Dict[str, str]:
        by_role = {e.role: e.selector for e in page.elements}
        return {
            "search_input": by_role.get("search_input", "input[placeholder*='订单']"),
            "search_button": by_role.get("search_button", "button:has-text('搜索')"),
            "order_row": by_role.get("order_row", "table tr:nth-child(1)"),
            "detail_button": by_role.get("detail_button", "button:has-text('详情')"),
        }


class ActionPlanner:
    def __init__(self) -> None:
        self.adapter = FrameworkActionAdapter()

    def plan(self, page: WebPageModel, intent: UserIntent) -> List[ActionStep]:
        sel = self.adapter.selectors(page)
        steps: List[ActionStep] = []
        if intent.action in {"view_order", "filter_order", "open_order_detail"} and "order_id" in intent.filters:
            steps.append(ActionStep("type", sel["search_input"], intent.filters["order_id"], "输入订单号"))
            steps.append(ActionStep("click", sel["search_button"], desc="点击搜索"))
        if intent.action == "open_order_detail":
            steps.append(ActionStep("click", sel["order_row"], desc="点击订单行"))
            steps.append(ActionStep("click", sel["detail_button"], desc="打开订单详情"))
        if not steps:
            steps.append(ActionStep("observe", sel["order_row"], desc="查看订单列表首行"))
        return steps


class ActionExecutor:
    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run

    def execute(self, steps: List[ActionStep]) -> List[str]:
        logs: List[str] = []
        for idx, s in enumerate(steps, start=1):
            if self.dry_run:
                logs.append(f"[dry-run#{idx}] {s.op} {s.selector} value={s.value} desc={s.desc}")
            else:
                logs.append(f"[exec#{idx}] {s.op} {s.selector} value={s.value} desc={s.desc}")
        return logs


class AutoWebConversationBot:
    def __init__(self, llm: LLMClient, dry_run: bool = True) -> None:
        self.page_parser = PageParser()
        self.intent_parser = IntentParser(llm)
        self.planner = ActionPlanner()
        self.executor = ActionExecutor(dry_run=dry_run)

    def run(self, page_snapshot_file: Path, user_utterance: str) -> Dict[str, object]:
        page = self.page_parser.parse(page_snapshot_file)
        intent = self.intent_parser.parse(user_utterance)
        steps = self.planner.plan(page, intent)
        logs = self.executor.execute(steps)
        return {
            "page": page.page_name,
            "framework": page.framework,
            "intent": intent.__dict__,
            "steps": [step.__dict__ for step in steps],
            "logs": logs,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AutoWeb conversation bot module")
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
