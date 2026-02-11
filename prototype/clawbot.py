#!/usr/bin/env python3
"""ClawBot prototype: agent-scheduled DouShop crawling + dynamic auto-run bot."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import random
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class MerchantSnapshot:
    ts: str
    gmv: float
    traffic: int
    conversion_rate: float
    refund_rate: float
    ad_roi: float


@dataclass
class MarketSnapshot:
    ts: str
    industry_gmv_trend: float
    competitor_price_index: float
    competitor_content_freq: float


@dataclass
class StrategyRule:
    source: str
    text: str


@dataclass
class CrawlPageSignal:
    page_name: str
    page_path: str
    metrics: Dict[str, float]
    excerpt: str


@dataclass
class LLMAnalysisResult:
    provider: str
    model: str
    summary: str
    recommendations: List[str]
    raw_text: str


@dataclass
class ToolResult:
    tool_name: str
    summary: str
    findings: List[str]
    recommendations: List[str]


@dataclass
class DiagnosticDimension:
    name: str
    metric: str
    criteria: str
    root_causes: List[str]
    advices: List[str]


@dataclass
class GeneratedToolSpec:
    tool_name: str
    domain: str
    question: str
    dimensions: List[DiagnosticDimension]
    strategy_map: Dict[str, str]
    self_test_cases: List[Dict[str, str]]


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS merchant_snapshots (
                ts TEXT PRIMARY KEY,
                gmv REAL,
                traffic INTEGER,
                conversion_rate REAL,
                refund_rate REAL,
                ad_roi REAL
            );

            CREATE TABLE IF NOT EXISTS market_snapshots (
                ts TEXT PRIMARY KEY,
                industry_gmv_trend REAL,
                competitor_price_index REAL,
                competitor_content_freq REAL
            );

            CREATE TABLE IF NOT EXISTS strategy_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                text TEXT
            );

            CREATE TABLE IF NOT EXISTS crawl_page_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                page_name TEXT,
                page_path TEXT,
                metrics_json TEXT,
                excerpt TEXT
            );

            CREATE TABLE IF NOT EXISTS diagnosis_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                health_score REAL,
                risks TEXT,
                recommendations TEXT,
                llm_provider TEXT,
                llm_model TEXT,
                llm_summary TEXT,
                llm_raw_text TEXT
            );
            """
        )
        self.conn.commit()
        self._ensure_diagnosis_columns()

    def _ensure_diagnosis_columns(self) -> None:
        expected = {
            "llm_provider": "TEXT",
            "llm_model": "TEXT",
            "llm_summary": "TEXT",
            "llm_raw_text": "TEXT",
        }
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(diagnosis_reports)").fetchall()}
        for col, typ in expected.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE diagnosis_reports ADD COLUMN {col} {typ}")
        self.conn.commit()

    def get_last_diagnosis(self) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM diagnosis_reports ORDER BY id DESC LIMIT 1").fetchone()

    def get_recent_scores(self, limit: int = 5) -> List[float]:
        rows = self.conn.execute("SELECT health_score FROM diagnosis_reports ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [float(row[0]) for row in rows]

    def save_merchant_snapshot(self, snapshot: MerchantSnapshot) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO merchant_snapshots
            (ts, gmv, traffic, conversion_rate, refund_rate, ad_roi)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (snapshot.ts, snapshot.gmv, snapshot.traffic, snapshot.conversion_rate, snapshot.refund_rate, snapshot.ad_roi),
        )
        self.conn.commit()

    def save_market_snapshot(self, snapshot: MarketSnapshot) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO market_snapshots
            (ts, industry_gmv_trend, competitor_price_index, competitor_content_freq)
            VALUES (?, ?, ?, ?)
            """,
            (snapshot.ts, snapshot.industry_gmv_trend, snapshot.competitor_price_index, snapshot.competitor_content_freq),
        )
        self.conn.commit()

    def save_page_signals(self, ts: str, signals: List[CrawlPageSignal]) -> None:
        self.conn.executemany(
            """
            INSERT INTO crawl_page_signals(ts, page_name, page_path, metrics_json, excerpt)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(ts, s.page_name, s.page_path, json.dumps(s.metrics, ensure_ascii=False), s.excerpt) for s in signals],
        )
        self.conn.commit()

    def replace_rules(self, rules: List[StrategyRule]) -> None:
        self.conn.execute("DELETE FROM strategy_rules")
        self.conn.executemany("INSERT INTO strategy_rules(source, text) VALUES (?, ?)", [(r.source, r.text) for r in rules])
        self.conn.commit()

    def save_diagnosis(self, ts: str, health_score: float, risks: List[str], recommendations: List[str], llm_result: LLMAnalysisResult) -> None:
        self.conn.execute(
            """
            INSERT INTO diagnosis_reports(
                ts, health_score, risks, recommendations,
                llm_provider, llm_model, llm_summary, llm_raw_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                health_score,
                " | ".join(risks),
                " | ".join(recommendations),
                llm_result.provider,
                llm_result.model,
                llm_result.summary,
                llm_result.raw_text,
            ),
        )
        self.conn.commit()


class DouShopPageSchedulerAgent:
    def __init__(self, page_config: Path) -> None:
        self.page_config = page_config

    def build_tasks(self) -> List[Dict[str, str]]:
        payload = json.loads(self.page_config.read_text(encoding="utf-8"))
        return sorted(payload.get("pages", []), key=lambda x: x.get("priority", 100))


class DouShopDataCrawlerAgent:
    def __init__(
        self,
        mode: str,
        scheduler: DouShopPageSchedulerAgent,
        base_url: str,
        login_path: str,
        account: str,
        password: str,
        mock_snapshot_file: Path,
        headless: bool = True,
    ) -> None:
        self.mode = mode
        self.scheduler = scheduler
        self.base_url = base_url.rstrip("/")
        self.login_path = login_path
        self.account = account
        self.password = password
        self.mock_snapshot_file = mock_snapshot_file
        self.headless = headless

    def collect(self) -> List[CrawlPageSignal]:
        tasks = self.scheduler.build_tasks()
        if self.mode == "doushop-playwright":
            return asyncio.run(self._collect_by_playwright(tasks))
        return self._collect_by_mock(tasks)

    def _collect_by_mock(self, tasks: List[Dict[str, str]]) -> List[CrawlPageSignal]:
        payload = json.loads(self.mock_snapshot_file.read_text(encoding="utf-8"))
        page_texts = payload.get("pages", {})
        result: List[CrawlPageSignal] = []
        for task in tasks:
            txt = page_texts.get(task["path"], "")
            result.append(CrawlPageSignal(task["name"], task["path"], self._parse_metrics(txt), txt[:240]))
        return result

    async def _collect_by_playwright(self, tasks: List[Dict[str, str]]) -> List[CrawlPageSignal]:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("playwright 未安装，请先使用 doushop-mock 模式验证") from exc

        signals: List[CrawlPageSignal] = []
        async with async_playwright() as p:  # pragma: no cover
            browser = await p.chromium.launch(headless=self.headless)
            page = await browser.new_page()
            await page.goto(f"{self.base_url}{self.login_path}")
            await page.fill('input[type="text"]', self.account)
            await page.fill('input[type="password"]', self.password)
            await page.click('button:has-text("登录")')
            await page.wait_for_timeout(3000)
            for task in tasks:
                await page.goto(f"{self.base_url}{task['path']}")
                await page.wait_for_timeout(1500)
                txt = await page.inner_text("body")
                signals.append(CrawlPageSignal(task["name"], task["path"], self._parse_metrics(txt), txt[:240]))
            await browser.close()
        return signals

    def _parse_metrics(self, text: str) -> Dict[str, float]:
        def pick(pattern: str, pct: bool = False) -> Optional[float]:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if not m:
                return None
            v = float(m.group(1).replace(",", ""))
            return v / 100 if pct else v

        d = {
            "gmv": pick(r"(?:GMV|成交额)[^\d]{0,8}([\d,.]+)"),
            "traffic": pick(r"(?:访客数|流量|UV)[^\d]{0,8}([\d,.]+)"),
            "conversion_rate": pick(r"(?:转化率|支付转化率)[^\d]{0,8}([\d.]+)%", True),
            "refund_rate": pick(r"(?:退款率|售后率)[^\d]{0,8}([\d.]+)%", True),
            "ad_roi": pick(r"(?:ROI|投产比)[^\d]{0,8}([\d.]+)"),
            "industry_gmv_trend": pick(r"(?:行业趋势|行业GMV趋势)[^\-\d]{0,8}([\-\d.]+)%", True),
            "competitor_price_index": pick(r"(?:竞品价格指数|价格指数)[^\d]{0,8}([\d.]+)"),
            "competitor_content_freq": pick(r"(?:竞品内容频率指数|内容频率指数)[^\d]{0,8}([\d.]+)"),
        }
        return {k: v for k, v in d.items() if v is not None}


class MerchantDataAgent:
    def from_page_signals(self, signals: List[CrawlPageSignal]) -> MerchantSnapshot:
        merged = self._merge_metric_map(signals)
        now = dt.datetime.now().isoformat(timespec="seconds")
        return MerchantSnapshot(
            ts=now,
            gmv=round(merged.get("gmv", random.uniform(10000, 25000)), 2),
            traffic=int(merged.get("traffic", random.uniform(2000, 8000))),
            conversion_rate=round(merged.get("conversion_rate", random.uniform(0.02, 0.04)), 4),
            refund_rate=round(merged.get("refund_rate", random.uniform(0.04, 0.10)), 4),
            ad_roi=round(merged.get("ad_roi", random.uniform(1.4, 2.8)), 3),
        )

    @staticmethod
    def _merge_metric_map(signals: List[CrawlPageSignal]) -> Dict[str, float]:
        merged: Dict[str, List[float]] = {}
        for signal in signals:
            for k, v in signal.metrics.items():
                merged.setdefault(k, []).append(v)
        return {k: sum(vals) / len(vals) for k, vals in merged.items()}


class MarketCompetitorAgent:
    def from_page_signals(self, signals: List[CrawlPageSignal]) -> MarketSnapshot:
        merged = MerchantDataAgent._merge_metric_map(signals)
        now = dt.datetime.now().isoformat(timespec="seconds")
        return MarketSnapshot(
            ts=now,
            industry_gmv_trend=round(merged.get("industry_gmv_trend", random.uniform(-0.05, 0.12)), 4),
            competitor_price_index=round(merged.get("competitor_price_index", random.uniform(0.9, 1.1)), 4),
            competitor_content_freq=round(merged.get("competitor_content_freq", random.uniform(0.7, 1.4)), 4),
        )


class KnowledgeLearningAgent:
    def __init__(self, knowledge_dir: Path) -> None:
        self.knowledge_dir = knowledge_dir

    def extract_rules(self) -> List[StrategyRule]:
        rules: List[StrategyRule] = []
        for md_file in sorted(self.knowledge_dir.glob("*.md")):
            for line in md_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if re.match(r"^\d+\.", line):
                    rules.append(StrategyRule(md_file.name, line))
        return rules


class DiagnosisStrategyAgent:
    def baseline_diagnose(self, merchant: MerchantSnapshot, market: MarketSnapshot, rules: List[StrategyRule]) -> Tuple[float, List[str], List[str]]:
        score = 100.0
        risks: List[str] = []
        recs: List[str] = []
        if merchant.conversion_rate < 0.025:
            score -= 18
            risks.append("转化率偏低")
            recs.append("优化主图与详情页，补强评价内容")
        if merchant.refund_rate > 0.10:
            score -= 20
            risks.append("退款率偏高")
            recs.append("按SKU分层排查退款原因，修正商品描述与履约")
        if merchant.ad_roi < 1.8:
            score -= 15
            risks.append("广告ROI偏低")
            recs.append("缩减低转化词包，做人群分层重建")
        if market.industry_gmv_trend > 0.04 and merchant.gmv < 12000:
            score -= 12
            risks.append("行业上涨但店铺未跟涨")
            recs.append("补齐活动节奏与内容更新频率")
        for r in rules[:3]:
            recs.append(f"策略知识参考：{r.text}")
        return round(max(0.0, score), 2), (risks or ["暂无显著风险"]), (recs or ["维持当前策略并持续观察"])


class LLMAnalyzerAgent:
    def __init__(self, provider: str, model: str, api_key: str, endpoint: str, skill_file: Path, timeout_seconds: int = 25) -> None:
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.endpoint = endpoint
        self.skill_file = skill_file
        self.timeout_seconds = timeout_seconds

    def analyze(
        self,
        merchant: MerchantSnapshot,
        market: MarketSnapshot,
        page_signals: List[CrawlPageSignal],
        baseline_risks: List[str],
        baseline_recommendations: List[str],
    ) -> LLMAnalysisResult:
        if self.provider == "none" or not self.api_key:
            return LLMAnalysisResult("none", "none", "未配置 LLM API Key，当前仅输出规则诊断。", baseline_recommendations[:3], "")

        prompt = self._build_prompt(
            merchant.__dict__, market.__dict__, [x.__dict__ for x in page_signals], baseline_risks, baseline_recommendations
        )
        try:
            raw = self._call_provider(prompt)
            summary, recs = self._parse_llm_text(raw)
            return LLMAnalysisResult(self.provider, self.model, summary, recs or baseline_recommendations[:3], raw)
        except Exception as exc:
            return LLMAnalysisResult(self.provider, self.model, f"LLM 调用失败，回退基线建议: {exc}", baseline_recommendations[:3], "")

    def decide_should_run(self, decision_context: Dict[str, object]) -> Optional[Tuple[bool, str]]:
        """Return None when not applicable; otherwise tuple(should_run, reason)."""
        if self.provider == "none" or not self.api_key:
            return None
        prompt = (
            "你是抖店巡检调度助手，请根据输入判断现在是否需要触发一次巡检分析。"
            "只输出JSON：{\"should_run\":true/false,\"reason\":\"...\"}。\n"
            f"输入: {json.dumps(decision_context, ensure_ascii=False)}"
        )
        try:
            raw = self._call_provider(prompt)
            data = json.loads(self._extract_json(raw))
            return bool(data.get("should_run", False)), str(data.get("reason", "llm_decision"))
        except Exception:
            return None

    def _build_prompt(
        self,
        merchant_payload: Dict[str, object],
        market_payload: Dict[str, object],
        page_payload: List[Dict[str, object]],
        risks: List[str],
        recs: List[str],
    ) -> str:
        skill_payload = json.loads(self.skill_file.read_text(encoding="utf-8"))
        payload = {
            "merchant": merchant_payload,
            "market": market_payload,
            "page_signals": page_payload,
            "baseline_risks": risks,
            "baseline_recommendations": recs,
            "analysis_skills": skill_payload,
        }
        return (
            "你是抖店经营分析专家。先给一句总评，再给3-5条建议；"
            "每条包含【动作】【预期收益】【风险】。\n"
            f"输入JSON:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

    def _call_provider(self, prompt: str) -> str:
        if self.provider == "openai":
            return self._call_openai(prompt)
        if self.provider == "gemini":
            return self._call_gemini(prompt)
        raise RuntimeError(f"unsupported provider: {self.provider}")

    def _call_openai(self, prompt: str) -> str:
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(
                {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "You are a senior e-commerce analyst."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"openai http {exc.code}") from exc
        return payload["choices"][0]["message"]["content"].strip()

    def _call_gemini(self, prompt: str) -> str:
        endpoint = self.endpoint
        if "key=" not in endpoint:
            endpoint = f"{endpoint}{'&' if '?' in endpoint else '?'}key={self.api_key}"
        req = urllib.request.Request(
            endpoint,
            data=json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"gemini http {exc.code}") from exc
        return payload["candidates"][0]["content"]["parts"][0]["text"].strip()

    def _parse_llm_text(self, text: str) -> Tuple[str, List[str]]:
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        summary = lines[0] if lines else "LLM 未返回有效内容"
        recs = [x.lstrip("- ") for x in lines if x.startswith("-")]
        return summary, recs[:5]

    @staticmethod
    def _extract_json(text: str) -> str:
        m = re.search(r"\{.*\}", text, flags=re.S)
        return m.group(0) if m else text


class BaseDiagnosisTool:
    name = "base"

    def run(
        self,
        merchant: MerchantSnapshot,
        market: MarketSnapshot,
        page_signals: List[CrawlPageSignal],
        question: str,
        llm_analyzer: LLMAnalyzerAgent,
    ) -> ToolResult:
        raise NotImplementedError


class TrafficConversionTool(BaseDiagnosisTool):
    name = "traffic_conversion_tool"

    def run(
        self,
        merchant: MerchantSnapshot,
        market: MarketSnapshot,
        page_signals: List[CrawlPageSignal],
        question: str,
        llm_analyzer: LLMAnalyzerAgent,
    ) -> ToolResult:
        findings: List[str] = []
        recs: List[str] = []
        if merchant.traffic < 3000:
            findings.append("流量规模偏低")
            recs.append("增加短视频高点击素材测试，优先拉升自然流量入口")
        if merchant.conversion_rate < 0.025:
            findings.append("转化率偏低")
            recs.append("重做详情页首屏卖点，补充强背书评价与问大家")
        if market.industry_gmv_trend > 0.03 and merchant.conversion_rate < 0.03:
            findings.append("行业向上但店铺承接不足")
            recs.append("排查活动期落地页承接路径，减少下单链路阻力")
        return ToolResult(
            tool_name=self.name,
            summary="流量-转化链路诊断完成",
            findings=findings or ["流量转化链路暂无明显异常"],
            recommendations=recs or ["保持流量结构，持续监控转化漏斗"],
        )


class RefundFulfillmentTool(BaseDiagnosisTool):
    name = "refund_fulfillment_tool"

    def run(
        self,
        merchant: MerchantSnapshot,
        market: MarketSnapshot,
        page_signals: List[CrawlPageSignal],
        question: str,
        llm_analyzer: LLMAnalyzerAgent,
    ) -> ToolResult:
        findings: List[str] = []
        recs: List[str] = []
        if merchant.refund_rate > 0.10:
            findings.append("退款率偏高，可能存在商品预期差或履约问题")
            recs.append("按SKU拆解退款原因，优先治理尺码/材质/时效相关投诉")
        if merchant.refund_rate > 0.08 and merchant.conversion_rate < 0.025:
            findings.append("存在高退款与低转化叠加风险")
            recs.append("暂停高售后SKU投放，先完成商品页承诺与客服话术修复")
        return ToolResult(
            tool_name=self.name,
            summary="售后与履约风险诊断完成",
            findings=findings or ["售后与履约风险可控"],
            recommendations=recs or ["维持当前售后SLA，并按周抽检问题订单"],
        )


class ROICompetitionTool(BaseDiagnosisTool):
    name = "roi_competition_tool"

    def run(
        self,
        merchant: MerchantSnapshot,
        market: MarketSnapshot,
        page_signals: List[CrawlPageSignal],
        question: str,
        llm_analyzer: LLMAnalyzerAgent,
    ) -> ToolResult:
        findings: List[str] = []
        recs: List[str] = []
        if merchant.ad_roi < 1.8:
            findings.append("投放ROI偏低")
            recs.append("重构投放计划，剔除低转化词包并按人群分层出价")
        if market.competitor_price_index < 0.95:
            findings.append("竞品价格带更激进")
            recs.append("优先用组合装/赠品策略对冲，而非直接降价")
        if market.competitor_content_freq > 1.2:
            findings.append("竞品内容更新频率较高")
            recs.append("提高内容上新频次，建立周级选题与素材复盘机制")
        return ToolResult(
            tool_name=self.name,
            summary="投放ROI与竞品压力诊断完成",
            findings=findings or ["投放和竞品压力暂无显著异常"],
            recommendations=recs or ["维持当前投放节奏并观察竞品变化"],
        )


class QuestionKnowledgeTool(BaseDiagnosisTool):
    name = "question_knowledge_tool"

    def run(
        self,
        merchant: MerchantSnapshot,
        market: MarketSnapshot,
        page_signals: List[CrawlPageSignal],
        question: str,
        llm_analyzer: LLMAnalyzerAgent,
    ) -> ToolResult:
        if not question.strip():
            return ToolResult(self.name, "未提供具体问题", ["跳过问题型知识检索"], [])
        if llm_analyzer.provider == "none" or not llm_analyzer.api_key:
            return ToolResult(
                self.name,
                "LLM 未配置，无法执行问题知识检索",
                [f"问题：{question}"],
                ["配置 OpenAI/Gemini 后可获得该问题的SOTA知识补充"],
            )
        prompt = (
            "你是电商经营研究助手。请围绕下面问题给出简洁结论：\n"
            f"问题：{question}\n"
            "输出格式：\n"
            "1) 关键信息（3条）\n"
            "2) 适用于抖店商家的可执行建议（3条）"
        )
        try:
            raw = llm_analyzer._call_provider(prompt)
            lines = [x.strip() for x in raw.splitlines() if x.strip()]
            return ToolResult(
                tool_name=self.name,
                summary="基于问题的外部知识检索完成",
                findings=lines[:3] or ["已获取问题相关知识"],
                recommendations=lines[3:6] or ["请结合店铺实际数据验证上述建议"],
            )
        except Exception as exc:
            return ToolResult(
                tool_name=self.name,
                summary=f"问题知识检索失败: {exc}",
                findings=[f"问题：{question}"],
                recommendations=["稍后重试或切换模型端点"],
            )


class ToolOrchestrator:
    def __init__(self, tools: List[BaseDiagnosisTool]) -> None:
        self.tools = tools

    def run_all(
        self,
        merchant: MerchantSnapshot,
        market: MarketSnapshot,
        page_signals: List[CrawlPageSignal],
        question: str,
        llm_analyzer: LLMAnalyzerAgent,
    ) -> List[ToolResult]:
        return [tool.run(merchant, market, page_signals, question, llm_analyzer) for tool in self.tools]




class RuntimeGeneratedDiagnosticTool(BaseDiagnosisTool):
    def __init__(self, spec: GeneratedToolSpec) -> None:
        self.spec = spec
        self.name = spec.tool_name

    def run(
        self,
        merchant: MerchantSnapshot,
        market: MarketSnapshot,
        page_signals: List[CrawlPageSignal],
        question: str,
        llm_analyzer: LLMAnalyzerAgent,
    ) -> ToolResult:
        metric_context = {
            "gmv": merchant.gmv,
            "traffic": merchant.traffic,
            "conversion_rate": merchant.conversion_rate,
            "refund_rate": merchant.refund_rate,
            "ad_roi": merchant.ad_roi,
            "industry_gmv_trend": market.industry_gmv_trend,
            "competitor_price_index": market.competitor_price_index,
            "competitor_content_freq": market.competitor_content_freq,
        }
        findings: List[str] = []
        recommendations: List[str] = []

        for dim in self.spec.dimensions:
            strategy = self.spec.strategy_map.get(dim.name, "deterministic")
            metric_value = metric_context.get(dim.metric)
            if strategy == "deterministic":
                if metric_value is not None:
                    findings.append(f"{dim.name}: {dim.metric}={metric_value}, 标准={dim.criteria}")
                if dim.root_causes:
                    findings.append(f"{dim.name}根因: {dim.root_causes[0]}")
                if dim.advices:
                    recommendations.append(f"{dim.name}: {dim.advices[0]}")
            else:
                semantic_hint = self._semantic_check(dim, metric_context, llm_analyzer)
                findings.append(f"{dim.name}(语义): {semantic_hint}")
                if dim.advices:
                    recommendations.append(f"{dim.name}: {dim.advices[0]}")

        return ToolResult(
            tool_name=self.name,
            summary=f"KDTS 生成工具执行完成({self.spec.domain})",
            findings=findings[:6] or ["未发现异常"],
            recommendations=recommendations[:6] or ["维持当前策略并观察"],
        )

    def _semantic_check(self, dim: DiagnosticDimension, metric_context: Dict[str, float], llm_analyzer: LLMAnalyzerAgent) -> str:
        if llm_analyzer.provider == "none" or not llm_analyzer.api_key:
            return "未配置LLM，已回退为规则摘要"
        prompt = (
            "你是抖店经营诊断助手。请判断该维度是否存在风险，输出一句话结论。\n"
            f"维度: {dim.name}\n"
            f"标准: {dim.criteria}\n"
            f"指标上下文: {json.dumps(metric_context, ensure_ascii=False)}"
        )
        try:
            resp = llm_analyzer._call_provider(prompt)
            return resp.splitlines()[0].strip() if resp.strip() else "语义判定无返回"
        except Exception as exc:
            return f"语义判定失败: {exc}"


class KDTSMetaToolFactory:
    """Knowledge-Driven Tool Synthesis (KDTS):
    1) knowledge crystallization
    2) strategy mapping
    3) tool synthesis
    4) self-reflection
    """

    def __init__(self, llm_analyzer: LLMAnalyzerAgent) -> None:
        self.llm_analyzer = llm_analyzer

    def build(self, domain: str, question: str) -> GeneratedToolSpec:
        dims = self._knowledge_crystallization(domain, question)
        strategy_map = self._strategy_mapping(dims)
        self_tests = self._self_reflection(dims, strategy_map)
        tool_name = f"kdts_{domain.lower().replace(' ', '_')}_tool"
        return GeneratedToolSpec(
            tool_name=tool_name,
            domain=domain,
            question=question,
            dimensions=dims,
            strategy_map=strategy_map,
            self_test_cases=self_tests,
        )

    def _knowledge_crystallization(self, domain: str, question: str) -> List[DiagnosticDimension]:
        if self.llm_analyzer.provider != "none" and self.llm_analyzer.api_key:
            prompt = (
                f"作为一个{domain}专家，请列出分析该问题最核心的5个诊断维度，并输出JSON数组。"
                "每项包含 name, metric, criteria, root_causes(list), advices(list)。"
                f"问题: {question}"
            )
            try:
                raw = self.llm_analyzer._call_provider(prompt)
                data = json.loads(self.llm_analyzer._extract_json(raw))
                if isinstance(data, list) and data:
                    dims: List[DiagnosticDimension] = []
                    for item in data[:6]:
                        dims.append(
                            DiagnosticDimension(
                                name=str(item.get("name", "未知维度")),
                                metric=str(item.get("metric", "conversion_rate")),
                                criteria=str(item.get("criteria", "需优于行业中位")),
                                root_causes=[str(x) for x in item.get("root_causes", [])][:3],
                                advices=[str(x) for x in item.get("advices", [])][:3],
                            )
                        )
                    if dims:
                        return dims
            except Exception:
                pass

        return [
            DiagnosticDimension("流量质量", "traffic", ">=3000 且来源稳定", ["自然流量不足", "内容吸引力弱"], ["增加高点击素材测试"]),
            DiagnosticDimension("转化效率", "conversion_rate", ">=2.5%", ["详情页说服不足", "价格策略不佳"], ["优化主图与首屏卖点"]),
            DiagnosticDimension("退款健康", "refund_rate", "<=10%", ["商品预期差", "履约时效不稳"], ["按SKU拆解退款原因并治理"]),
            DiagnosticDimension("投放产出", "ad_roi", ">=1.8", ["词包泛化", "人群不精准"], ["重建人群包并清理低效词"]),
            DiagnosticDimension("竞品压力", "competitor_price_index", ">=0.95", ["价格带被压制", "活动节奏落后"], ["采用组合装和促销节奏对齐"]),
        ]

    @staticmethod
    def _strategy_mapping(dims: List[DiagnosticDimension]) -> Dict[str, str]:
        semantic_metrics = {"content_quality", "copy_quality", "service_experience"}
        mapping: Dict[str, str] = {}
        for d in dims:
            mapping[d.name] = "semantic" if d.metric in semantic_metrics else "deterministic"
        return mapping

    @staticmethod
    def _self_reflection(dims: List[DiagnosticDimension], strategy_map: Dict[str, str]) -> List[Dict[str, str]]:
        cases = []
        for d in dims[:3]:
            cases.append(
                {
                    "dimension": d.name,
                    "strategy": strategy_map.get(d.name, "deterministic"),
                    "expected": "tool should return finding and recommendation",
                }
            )
        return cases

class AutoRunDecisionAgent:
    """Dynamically decide whether to trigger a diagnosis run."""

    def __init__(
        self,
        storage: Storage,
        llm_analyzer: LLMAnalyzerAgent,
        min_interval_minutes: int,
        max_interval_minutes: int,
        volatility_threshold: float,
    ) -> None:
        self.storage = storage
        self.llm_analyzer = llm_analyzer
        self.min_interval_minutes = min_interval_minutes
        self.max_interval_minutes = max_interval_minutes
        self.volatility_threshold = volatility_threshold

    def should_run_now(self, now: dt.datetime) -> Tuple[bool, str]:
        last = self.storage.get_last_diagnosis()
        if last is None:
            return True, "首次运行"

        last_ts = dt.datetime.fromisoformat(str(last["ts"]))
        minutes_since = (now - last_ts).total_seconds() / 60

        if minutes_since >= self.max_interval_minutes:
            return True, f"超过最大间隔 {self.max_interval_minutes} 分钟"
        if minutes_since < self.min_interval_minutes:
            return False, f"未到最小间隔 {self.min_interval_minutes} 分钟"

        scores = self.storage.get_recent_scores(limit=5)
        volatility = 0.0
        if len(scores) >= 2:
            volatility = max(scores) - min(scores)
        if volatility >= self.volatility_threshold:
            return True, f"健康分波动较大({volatility:.2f})"

        decision_context = {
            "minutes_since_last_run": round(minutes_since, 2),
            "recent_scores": scores,
            "volatility": round(volatility, 2),
            "last_llm_summary": str(last["llm_summary"] or ""),
            "last_risks": str(last["risks"] or ""),
        }
        llm_decision = self.llm_analyzer.decide_should_run(decision_context)
        if llm_decision is not None:
            return llm_decision

        return False, "无明显异常且未达到强制运行条件"


class ClawBotOrchestrator:
    def __init__(
        self,
        storage: Storage,
        crawler: DouShopDataCrawlerAgent,
        llm_analyzer: LLMAnalyzerAgent,
        tool_orchestrator: ToolOrchestrator,
        focus_question: str,
        kdts_domain: str,
        enable_kdts_factory: bool,
        knowledge_dir: Path,
        report_dir: Path,
    ) -> None:
        self.storage = storage
        self.crawler = crawler
        self.llm_analyzer = llm_analyzer
        self.tool_orchestrator = tool_orchestrator
        self.focus_question = focus_question
        self.kdts_domain = kdts_domain
        self.enable_kdts_factory = enable_kdts_factory
        self.kdts_factory = KDTSMetaToolFactory(llm_analyzer)
        self.merchant_agent = MerchantDataAgent()
        self.market_agent = MarketCompetitorAgent()
        self.learning_agent = KnowledgeLearningAgent(knowledge_dir)
        self.diagnosis_agent = DiagnosisStrategyAgent()
        self.report_dir = report_dir
        self.report_dir.mkdir(parents=True, exist_ok=True)

    async def run_once(self) -> None:
        page_signals = self.crawler.collect()
        merchant = self.merchant_agent.from_page_signals(page_signals)
        market = self.market_agent.from_page_signals(page_signals)
        rules = self.learning_agent.extract_rules()
        self.storage.save_merchant_snapshot(merchant)
        self.storage.save_market_snapshot(market)
        self.storage.replace_rules(rules)
        self.storage.save_page_signals(merchant.ts, page_signals)

        score, risks, recs = self.diagnosis_agent.baseline_diagnose(merchant, market, rules)
        tool_results = self.tool_orchestrator.run_all(
            merchant=merchant,
            market=market,
            page_signals=page_signals,
            question=self.focus_question,
            llm_analyzer=self.llm_analyzer,
        )

        kdts_spec: Optional[GeneratedToolSpec] = None
        if self.enable_kdts_factory and self.focus_question.strip():
            kdts_spec = self.kdts_factory.build(self.kdts_domain, self.focus_question)
            kdts_tool = RuntimeGeneratedDiagnosticTool(kdts_spec)
            tool_results.append(
                kdts_tool.run(
                    merchant=merchant,
                    market=market,
                    page_signals=page_signals,
                    question=self.focus_question,
                    llm_analyzer=self.llm_analyzer,
                )
            )
        tool_findings = [f"{t.tool_name}: {f}" for t in tool_results for f in t.findings[:2]]
        tool_recommendations = [f"{t.tool_name}: {r}" for t in tool_results for r in t.recommendations[:2]]

        llm_result = self.llm_analyzer.analyze(merchant, market, page_signals, risks + tool_findings, recs + tool_recommendations)
        final_recs = recs + tool_recommendations + [f"LLM增强建议：{item}" for item in llm_result.recommendations[:3]]
        ts = dt.datetime.now().isoformat(timespec="seconds")
        self.storage.save_diagnosis(ts, score, risks, final_recs, llm_result)

        report_file = self.report_dir / f"diagnosis_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        report_file.write_text(
            self._render_report(
                ts, merchant, market, page_signals, score, risks + tool_findings, final_recs, llm_result, tool_results, kdts_spec
            ),
            encoding="utf-8",
        )
        print(f"[ClawBot] Report generated: {report_file}")

    @staticmethod
    def _render_report(
        ts: str,
        merchant: MerchantSnapshot,
        market: MarketSnapshot,
        page_signals: List[CrawlPageSignal],
        score: float,
        risks: List[str],
        recommendations: List[str],
        llm_result: LLMAnalysisResult,
        tool_results: List[ToolResult],
        kdts_spec: Optional[GeneratedToolSpec],
    ) -> str:
        visited = "\n".join(f"- {x.page_name} ({x.page_path}) 指标: {json.dumps(x.metrics, ensure_ascii=False)}" for x in page_signals)
        risks_md = "\n".join(f"- {x}" for x in risks)
        recs_md = "\n".join(f"- {x}" for x in recommendations)
        tools_md = "\n".join(
            f"- [{t.tool_name}] {t.summary} | 发现: {'；'.join(t.findings[:2])} | 建议: {'；'.join(t.recommendations[:2])}"
            for t in tool_results
        )
        kdts_md = "未启用KDTS元工具工厂"
        if kdts_spec is not None:
            kdts_md = (
                f"工具名: {kdts_spec.tool_name}\n"
                f"- 域: {kdts_spec.domain}\n"
                f"- 问题: {kdts_spec.question}\n"
                f"- 维度数: {len(kdts_spec.dimensions)}\n"
                f"- 自检样例: {json.dumps(kdts_spec.self_test_cases, ensure_ascii=False)}"
            )
        return f"""# ClawBot 诊断报告 ({ts})

## Agent 调度访问页面
{visited}

## 商家核心指标
- GMV: {merchant.gmv}
- 流量: {merchant.traffic}
- 转化率: {merchant.conversion_rate:.2%}
- 退款率: {merchant.refund_rate:.2%}
- 广告ROI: {merchant.ad_roi}

## 行业/竞品信号
- 行业GMV趋势: {market.industry_gmv_trend:.2%}
- 竞品价格指数: {market.competitor_price_index}
- 竞品内容频率指数: {market.competitor_content_freq}

## 经营健康分
- **{score} / 100**

## 规则诊断风险
{risks_md}

## KDTS 元工具工厂输出
{kdts_md}

## 工具化诊断结果
{tools_md}

## LLM增强分析
- Provider: {llm_result.provider}
- Model: {llm_result.model}
- 总结: {llm_result.summary}

## 行动建议（规则 + Tools + LLM）
{recs_md}
"""


async def run_dynamic_bot(
    orchestrator: ClawBotOrchestrator,
    decision_agent: AutoRunDecisionAgent,
    tick_seconds: int,
    max_ticks: Optional[int],
) -> None:
    ticks = 0
    while True:
        now = dt.datetime.now()
        should_run, reason = decision_agent.should_run_now(now)
        print(f"[ClawBot-Bot] tick={ticks + 1} should_run={should_run} reason={reason}")
        if should_run:
            await orchestrator.run_once()
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        await asyncio.sleep(tick_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ClawBot prototype.")
    parser.add_argument("--db", default="clawbot.db")
    parser.add_argument("--knowledge-dir", default="knowledge")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--page-config", default="prototype/doushop_pages.json")
    parser.add_argument("--mock-snapshot-file", default="prototype/mock_doushop_pages.json")
    parser.add_argument("--analysis-skill-file", default="knowledge/analysis_skills.json")

    parser.add_argument("--crawl-mode", default="doushop-mock", choices=["doushop-mock", "doushop-playwright"])
    parser.add_argument("--base-url", default="https://fxg.jinritemai.com")
    parser.add_argument("--login-path", default="/login/common")
    parser.add_argument("--account", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--headless", action="store_true")

    parser.add_argument("--llm-provider", default="none", choices=["none", "openai", "gemini"])
    parser.add_argument("--llm-model", default="gpt-4o-mini")
    parser.add_argument("--llm-endpoint", default="https://api.openai.com/v1/chat/completions")
    parser.add_argument("--llm-api-key", default="")

    parser.add_argument("--mode", default="once", choices=["once", "dynamic-bot"], help="once: run immediately; dynamic-bot: run by decision")
    parser.add_argument("--tick-seconds", type=int, default=30, help="dynamic-bot polling interval")
    parser.add_argument("--max-ticks", type=int, default=3, help="dynamic-bot ticks, 0 means infinite")
    parser.add_argument("--min-interval-minutes", type=int, default=30)
    parser.add_argument("--max-interval-minutes", type=int, default=240)
    parser.add_argument("--volatility-threshold", type=float, default=12.0)
    parser.add_argument("--focus-question", default="", help="具体经营问题，用于问题知识检索工具")
    parser.add_argument("--enable-kdts-factory", action="store_true", help="启用KDTS元工具自动生成")
    parser.add_argument("--kdts-domain", default="抖店经营分析", help="KDTS知识结晶领域")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.crawl_mode == "doushop-playwright" and (not args.account or not args.password):
        raise ValueError("doushop-playwright 模式必须提供 --account 与 --password")

    api_key = args.llm_api_key or os.getenv("OPENAI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    storage = Storage(Path(args.db))
    scheduler = DouShopPageSchedulerAgent(Path(args.page_config))
    crawler = DouShopDataCrawlerAgent(
        args.crawl_mode,
        scheduler,
        args.base_url,
        args.login_path,
        args.account,
        args.password,
        Path(args.mock_snapshot_file),
        args.headless,
    )
    llm_analyzer = LLMAnalyzerAgent(args.llm_provider, args.llm_model, api_key, args.llm_endpoint, Path(args.analysis_skill_file))
    tool_orchestrator = ToolOrchestrator(
        tools=[TrafficConversionTool(), RefundFulfillmentTool(), ROICompetitionTool(), QuestionKnowledgeTool()]
    )
    orchestrator = ClawBotOrchestrator(
        storage,
        crawler,
        llm_analyzer,
        tool_orchestrator,
        args.focus_question,
        args.kdts_domain,
        args.enable_kdts_factory,
        Path(args.knowledge_dir),
        Path(args.report_dir),
    )

    if args.mode == "once":
        await orchestrator.run_once()
        return

    decision_agent = AutoRunDecisionAgent(
        storage=storage,
        llm_analyzer=llm_analyzer,
        min_interval_minutes=args.min_interval_minutes,
        max_interval_minutes=args.max_interval_minutes,
        volatility_threshold=args.volatility_threshold,
    )
    max_ticks = None if args.max_ticks == 0 else args.max_ticks
    await run_dynamic_bot(orchestrator, decision_agent, args.tick_seconds, max_ticks)


if __name__ == "__main__":
    asyncio.run(main())
