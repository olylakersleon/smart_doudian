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
