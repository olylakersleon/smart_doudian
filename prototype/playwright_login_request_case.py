#!/usr/bin/env python3
"""End-to-end Playwright test case:
1) open website and login
2) wait for user request(s)
3) execute request(s) and output result

Supports both interactive mode and batch mode via --requests-file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class LoginConfig:
    login_url: str
    account: str
    password: str
    account_selector: str
    password_selector: str
    submit_selector: str
    login_success_selector: str


class PlaywrightLoginRequestCase:
    def __init__(self, headless: bool = True, timeout_ms: int = 15000) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms

    async def run(self, cfg: LoginConfig, requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("playwright 未安装，请先安装 playwright") from exc

        results: List[Dict[str, Any]] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            page = await browser.new_page()
            await self._login(page, cfg)

            for req in requests:
                results.append(await self._execute_request(page, req))

            await browser.close()
        return results

    async def _login(self, page: Any, cfg: LoginConfig) -> None:
        await page.goto(cfg.login_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        await page.fill(cfg.account_selector, cfg.account)
        await page.fill(cfg.password_selector, cfg.password)
        await page.click(cfg.submit_selector)
        if cfg.login_success_selector:
            await page.wait_for_selector(cfg.login_success_selector, timeout=self.timeout_ms)

    async def _execute_request(self, page: Any, req: Dict[str, Any]) -> Dict[str, Any]:
        action = req.get("action", "")
        try:
            if action == "goto":
                url = req["url"]
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                return {"ok": True, "action": action, "url": page.url}

            if action == "click":
                selector = req["selector"]
                await page.click(selector, timeout=self.timeout_ms)
                return {"ok": True, "action": action, "selector": selector}

            if action == "type":
                selector = req["selector"]
                value = req.get("value", "")
                await page.fill(selector, value, timeout=self.timeout_ms)
                return {"ok": True, "action": action, "selector": selector, "value": value}

            if action == "wait":
                ms = int(req.get("ms", 1000))
                await page.wait_for_timeout(ms)
                return {"ok": True, "action": action, "ms": ms}

            if action == "extract_text":
                selector = req["selector"]
                text = await page.inner_text(selector, timeout=self.timeout_ms)
                return {"ok": True, "action": action, "selector": selector, "text": text}

            if action == "screenshot":
                path = req.get("path", "prototype/playwright_case.png")
                await page.screenshot(path=path, full_page=True)
                return {"ok": True, "action": action, "path": path}

            return {"ok": False, "action": action, "error": "unknown action"}
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "action": action, "error": str(exc), "request": req}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Playwright login + request execution case")
    parser.add_argument("--login-url", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--account-selector", default="input[type='text']")
    parser.add_argument("--password-selector", default="input[type='password']")
    parser.add_argument("--submit-selector", default="button[type='submit']")
    parser.add_argument("--login-success-selector", default="")
    parser.add_argument("--requests-file", default="", help="JSON file with request list")
    parser.add_argument("--headless", action="store_true")
    return parser.parse_args()


def load_requests_from_file(path: Path) -> List[Dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("requests"), list):
        return payload["requests"]
    raise ValueError("requests file must be list or {'requests': [...]} format")


def interactive_requests() -> List[Dict[str, Any]]:
    print("[Interactive] 请输入 JSON 请求（例如 {'action':'goto','url':'https://example.com'}），输入 exit 结束")
    reqs: List[Dict[str, Any]] = []
    while True:
        line = input("request> ").strip()
        if line.lower() in {"exit", "quit"}:
            break
        if not line:
            continue
        reqs.append(json.loads(line))
    return reqs


async def main() -> None:
    args = parse_args()

    cfg = LoginConfig(
        login_url=args.login_url,
        account=args.account,
        password=args.password,
        account_selector=args.account_selector,
        password_selector=args.password_selector,
        submit_selector=args.submit_selector,
        login_success_selector=args.login_success_selector,
    )

    if args.requests_file:
        requests = load_requests_from_file(Path(args.requests_file))
    else:
        requests = interactive_requests()

    case = PlaywrightLoginRequestCase(headless=args.headless)
    results = await case.run(cfg, requests)
    print(json.dumps({"count": len(results), "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
