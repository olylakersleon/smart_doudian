# 抖店商家 ClawBot（动态自动化 Bot 原型）

这是一个“后台自动化运行，但不是无时无刻都跑”的抖店诊断 Bot：

- Agent 自动调度访问页面并解析经营数据；
- 规则引擎 + GPT/Gemini 增强分析；
- 动态决策是否执行本轮巡检（最小间隔、最大间隔、波动阈值、可选 LLM 决策）。

## 核心模式

- `--mode once`：立即执行一次完整诊断。
- `--mode dynamic-bot`：按 tick 轮询，由决策 Agent 判断“此刻要不要跑”。

## 快速运行

### 1) 单次运行

```bash
python3 prototype/clawbot.py \
  --mode once \
  --crawl-mode doushop-mock \
  --llm-provider none
```

### 2) 动态 Bot 运行（推荐）

```bash
python3 prototype/clawbot.py \
  --mode dynamic-bot \
  --crawl-mode doushop-mock \
  --llm-provider none \
  --tick-seconds 2 \
  --max-ticks 3 \
  --min-interval-minutes 1 \
  --max-interval-minutes 30 \
  --volatility-threshold 8
```

## LLM 增强分析

### OpenAI (GPT)

```bash
export OPENAI_API_KEY='your_key'
python3 prototype/clawbot.py \
  --mode dynamic-bot \
  --llm-provider openai \
  --llm-model gpt-4o-mini \
  --llm-endpoint https://api.openai.com/v1/chat/completions
```

### Gemini

```bash
export GEMINI_API_KEY='your_key'
python3 prototype/clawbot.py \
  --mode dynamic-bot \
  --llm-provider gemini \
  --llm-model gemini-1.5-pro \
  --llm-endpoint 'https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent'
```

## 动态调度逻辑

每个 tick 会按顺序判断：

1. 首次运行 => 立即跑。
2. 距离上次运行超过 `max-interval-minutes` => 强制跑。
3. 未达到 `min-interval-minutes` => 跳过。
4. 近期健康分波动超过 `volatility-threshold` => 触发跑。
5. 若配置了 LLM，可让 LLM 基于最近风险和摘要做“是否运行”判断。
6. 其余情况跳过，等下一轮 tick。

## 关键参数

- `--mode`：`once` / `dynamic-bot`
- `--tick-seconds`：动态模式轮询周期
- `--max-ticks`：运行多少轮（`0` 为无限）
- `--min-interval-minutes`：最小触发间隔
- `--max-interval-minutes`：最大强制间隔
- `--volatility-threshold`：健康分波动触发阈值
- `--llm-provider`：`none/openai/gemini`


## Tools 化分析能力

当前内置 4 个可扩展诊断 tools：

- `traffic_conversion_tool`：流量与转化漏斗诊断
- `refund_fulfillment_tool`：退款与履约风险诊断
- `roi_competition_tool`：投放ROI与竞品压力诊断
- `question_knowledge_tool`：基于具体经营问题调用 SOTA LLM 补充知识

可以通过 `--focus-question` 传入一个具体问题，例如：

```bash
python3 prototype/clawbot.py \
  --mode once \
  --crawl-mode doushop-mock \
  --llm-provider openai \
  --focus-question '如何提升女装类目活动期转化率并控制退款率？'
```


## KDTS 元工具工厂（Knowledge-Driven Tool Synthesis）

新增 `KDTSMetaToolFactory`，支持四阶段自动生成诊断 tool：

1. 知识结晶（Knowledge Crystallization）
2. 策略映射（Strategy Mapping）
3. 工具合成（Tool Synthesis）
4. 自检封装（Self-Reflection）

启用方式：

```bash
python3 prototype/clawbot.py \
  --mode once \
  --crawl-mode doushop-mock \
  --focus-question '活动期如何提升转化并控制退款？' \
  --enable-kdts-factory \
  --kdts-domain '抖店经营诊断'
```

开启后，报告中会增加“KDTS 元工具工厂输出”区块，展示自动生成工具的维度与自检样例。


## 新增模块：AutoWeb 对话操作 Bot

新增 `prototype/web_autoweb_bot.py`，用于：

- 解析不同前端框架页面快照（AntD/Element/Layui/Vuetify/Generic）；
- 将用户对话需求转成结构化 intent；
- 抽象标准 WebDSL（`set_text`/`click`/`pick_date`/`observe`）；
- 通过框架适配层将 WebDSL 映射到不同组件实现（如 AntD/Element 的日期选择器）；
- 支持 dry-run 执行日志，便于接入真实 Playwright 执行层。

示例：

```bash
python3 prototype/web_autoweb_bot.py \
  --snapshot prototype/order_page_snapshot.json \
  --utterance '请在订单管理页查看订单号 ORDER_1001 的详情' \
  --dry-run
```


日期筛选示例（Element Plus）：

```bash
python3 prototype/web_autoweb_bot.py \
  --snapshot prototype/order_page_snapshot_element_plus.json \
  --utterance '请筛选 2025-02-15 的订单' \
  --dry-run
```


## WebDSL 适配策略（参考主流框架实践）

本模块按主流框架组件约定做了 selector 与交互抽象：

- Ant Design：`ant-picker-input`、`ant-picker-ok`、`ant-select-selector`
- Element Plus：`el-date-editor`、`el-picker-panel__footer`、`el-select`
- Layui：`lay-key`、`laydate-btns-confirm`、`layui-form-select`
- 通用兜底：`aria-label` / `data-testid` / placeholder 模式

建议在真实接入时优先给关键控件加 `data-testid`，可显著提升稳定性与可维护性。


## 长任务代码生成与复用模块

新增 `prototype/web_long_task_agent.py`，用于将用户一次“多页面长任务”指令编译成代码并缓存。

能力：

- 输入一条长任务对话需求（可包含“然后”串联）；
- 自动生成任务计划并编译成 Python 代码 (`prototype/generated_tasks/task_<id>.py`)；
- 同时落地任务元数据 (`task_<id>.json`)；
- 后续收到相同指令 + 相同页面快照时直接命中缓存，不再重新推理。

示例：

```bash
python3 prototype/web_long_task_agent.py \
  --instruction '先筛选 2025-02-15 订单，然后查看订单号 ORDER_1001 详情' \
  --snapshots prototype/order_page_snapshot_element_plus.json prototype/order_page_snapshot.json \
  --dry-run
```

该模式适合交付“可自主执行的独立工作 Agent”，并将底层代码产物对客户透明化。
