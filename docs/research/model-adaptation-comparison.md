# 主流 Agent 的模型适配：外部实现调查

调查日期：2026-10-08（Asia/Shanghai）。范围限于模型连接、协议、能力、历史回放、认证和恢复；不评估整体产品质量或声称这些项目代表所有主流产品。前文记录外部实现，末节记录当前工作区检查结果。引用聊天没有作为事实来源。

## 访问对象与版本

| 项目 | 已核实的仓库身份 | 本次固定源码版本 |
|---|---|---|
| DeepSeek Harness（DSH） | [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)，官方 README 声明由 DeepSeek AI 开发，并链接 deepseek.com | [`5badb15009ae1756c3afe0ae0cef1faafc290ccc`](https://github.com/deepseek-ai/deepseek-harness/commit/5badb15009ae1756c3afe0ae0cef1faafc290ccc)，master，提交时间 2026-10-03 |
| Hermes Agent | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | [`7dab93b06e2bb3757dc18229169efcee1b5b47a3`](https://github.com/NousResearch/hermes-agent/commit/7dab93b06e2bb3757dc18229169efcee1b5b47a3)，main，提交时间 2026-10-07 |
| Pi / pi-ai | 原 [badlogic/pi-mono](https://github.com/badlogic/pi-mono) 已重定向至 [earendil-works/pi](https://github.com/earendil-works/pi)，不是搜索结果中的第三方镜像仓库 | [`f10993bc7f28145df1375f3ff39c7f5c4cfc05f0`](https://github.com/earendil-works/pi/commit/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0)，main，提交时间 2026-10-07 |

版本通过 GitHub 第一方 API 查询默认分支 HEAD，然后按 SHA 读取文件。结论针对这些源码快照，未安装或运行这些产品，没有执行其测试。默认分支代码也不等于用户安装的发行版本。

## 主要结论

1. 成熟度差异主要体现在“完成真实多轮调用后还能可靠继续”，而不只在模型列表多寡。明确选择协议、请求参数差异、签名与 reasoning 回放、会话内换模型、失败恢复和回归测试，都已经有对应实现。
2. DSH 的多厂商协议层使用 pi-ai。其 `dsh-llm-pi-ai` 包声明依赖 `@earendil-works/pi-ai: ^0.87.1`，因此 DSH 与 Pi 并非完全独立的两套模型协议实现；DSH 在其上增加 Harness 服务、配置、凭据、回放封装和恢复插件。[DSH 包配置](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-pi-ai/package.json)、[适配包参考](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-pi-ai/README.md)。未核实此 DSH 快照实际安装的精确 pi-ai 版本，不能直接把 Pi 最新 HEAD 的全部特性归给 DSH。
3. Hermes 的突出能力是统一 provider registry 和运行时解析、主模型与辅助模型分别路由、认证来源及刷新、fallback chain 和大量具体异常恢复。Pi 的突出能力是统一消息事件协议、provider / API / model 类型分离，以及跨厂商历史转换。[Hermes 运行时文档](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/website/docs/developer-guide/provider-runtime.md)、[Pi 类型](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/ai/src/types.ts)、[Pi 历史转换](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/ai/src/api/transform-messages.ts)。

## DeepSeek Harness

### Provider、模型与协议

配置是按 provider route 建立的字典。已安装的 pi-ai provider 可提供协议、端点和模型目录默认值；自定义 route 明确声明 `api`、`baseURL` 和 `models`。自定义协议表支持 `openai-completions`、`openai-responses`、`anthropic-messages`。已登记 provider 不覆盖协议时复用其原生实现，因此原有 provider 的云认证与协议逻辑没有被简化为普通 API key 客户端。自定义 route 的表故意只提供可用 key、URL 和 headers 完整描述的三种协议。[provider.ts](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-pi-ai/src/provider.ts)。

Web Models 表单允许用户指定自定义 Provider ID、URL、API 协议、凭据及多个模型。配置下一次请求生效。每个自定义 provider 在此表单中只有一种协议；网关同时提供两种协议时，文档要求配置两个 provider。可以请求模型列表，也允许手动输入 ID；模型发现只提供候选元数据，不承诺实测能力。[用户配置指南](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/user/guide/providers.md)。

### 模型能力及兼容参数

支持模型目录继承、`models` 替换目录及 `modelOverrides` 修改单模型；配置可覆盖输入模态、上下文窗口、输出上限、reasoning 等级和线上的 effort 拼写。`compat` 按 route 及 model 合并，可处理 `system` / `developer`、`max_tokens` / `max_completion_tokens`、DeepSeek `thinking` 等差异。能力配置仍是对端点的声明，不是自动能力验证。[配置指南](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/user/guide/providers.md)、[配置源码](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-pi-ai/src/config.ts)。

### 认证、回放及恢复

`apiKeyEnv` 是每次请求解析的凭据引用。显式引用不存在时返回 `MISSING_CREDENTIAL`，不会静默交给 pi-ai 去发现另一个环境 key。适配器层支持 pi-ai 的已存储登录与 refresh locking，但 Web 添加 provider 指南仍说明 Codex 等 OAuth provider 暂不支持“这里”的配置；不能把适配器支持等同于 Web 入口全部完成。[index.ts](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-pi-ai/src/index.ts)、[适配包参考](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-pi-ai/README.md)、[Web 配置指南](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/user/guide/providers.md)。

成功响应持久化 versioned `ReplayEnvelope`，保存 API、provider、请求 model、response ID、每块 signature 和可选 provider thinking level。请求时检查来源身份、版本和内容块匹配；不可用的回放元数据降级为 provider-neutral 内容，并提供诊断。回放是适配器自己的机制，持久化正文仍是权威。[replay.ts](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-pi-ai/src/replay.ts)。

错误有稳定 code，区分 credential、unknown model、quota 与 rate limit。单次 SDK 重试关闭后由 `dsh-llm-retry` 统一处理 route 的恢复策略，支持 normal 与 always 模式、可取消等待、provider Retry-After 和持久化 retry 事件。这里证实的是重试执行器，不是泛化的自动跨厂商 fallback。[adapter.ts](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-pi-ai/src/adapter.ts)、[retry 插件](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-retry/src/index.ts)。

适配测试包括配置快照、私有 endpoint、未支持 effort 网络前拒绝、SDK 不重复重试等。provider E2E 文件有 OpenAI Responses、Anthropic Messages 的文字、usage、native replay 和 tool-call round trip；需要实际认证环境，存在文件不表示本次已通过。[adapter.spec.ts](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-pi-ai/tests/adapter.spec.ts)、[provider-apis.e2e.ts](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-pi-ai/tests/provider-apis.e2e.ts)。

## Hermes Agent

### Provider registry 与显式协议

`ProviderProfile` 集中声明认证变量、端点、API mode、默认模型等事实，registry 自动发现内置及用户 provider plugin。profile 可通过 hook 添加客户端、transport、模型发现、reasoning 和 extra body，而不必把所有特殊逻辑塞进 resolver。[插件约定](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/plugins/model-providers/README.md)、[ProviderProfile](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/providers/base.py)。

命名自定义 provider 可声明 endpoint、`transport: chat_completions / anthropic_messages / codex_responses`、多个模型、context length、extra body、headers、TLS、session affinity 和 `catalog_provider`。最后一项只继承已知模型的元数据，不改变 URL 或凭据。Custom Endpoint wizard 会明确询问协议，URL 检测是未填写时的后备。[官方 provider 文档的 Named Custom Providers](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/website/docs/integrations/providers.md#named-custom-providers)、[custom resolver](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/hermes_cli/runtime_provider_custom.py)。

### 多模型路由与认证

CLI、gateway、cron、ACP、辅助模型共用 runtime resolver。视觉、网页提取、压缩、记忆等辅助任务可以选择不同 provider/model。认证覆盖 API key、OAuth、ambient cloud credentials；自定义 provider 的 `key_cmd` 可按 expiry 重取短期 token，凭据不必写入配置。[运行时文档](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/website/docs/developer-guide/provider-runtime.md)、[Named Custom Providers](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/website/docs/integrations/providers.md#named-custom-providers)。

### Reasoning / tool-call 回放

字符串、字典、数组形式的 reasoning 被归一化；opaque `reasoning_details` 保留次序、nested signature 并进入 session replay。Chat Completions 的 wire copy 按路由剥离不支持的 reasoning metadata，避免中途换 provider 后 strict schema 报错；不会简单把历史内容原样发给所有端点。[运行时文档](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/website/docs/developer-guide/provider-runtime.md)、[chat_completions transport](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/agent/transports/chat_completions.py)。

DeepSeek thinking tool-call 的 `reasoning_content` 有专门回归测试，覆盖历史缺字段时的防御性补齐及 strict 非 thinking provider 的字段剥离。这里是 Hermes 实现的兼容策略，不推导为每个 DeepSeek API 版本均接受相同占位值。[DeepSeek 回放测试](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/tests/agent/test_deepseek_reasoning_content_echo.py)。

Anthropic 签名拒绝后持久化被拒 signature 指纹，只在 rebuilt request copy 中过滤，保留 canonical history；新生成的签名可继续回放。Codex invalid encrypted content / token expired 也有先剥离旧 replay 再尝试的修复路径。[Anthropic replay](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/agent/anthropic_thinking_replay.py)、[runtime 文档](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/website/docs/developer-guide/provider-runtime.md)。

断开的 tool-call history 会按只读或有副作用区分处理；有副作用的中断记录保留并补 UNKNOWN-effect 提示，防止恢复时误当未执行而重复操作。[replay_cleanup.py](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/agent/replay_cleanup.py)。

### Fallback 与限制

支持显式 fallback provider/model 链，保留 conversation 并继承自定义 provider 声明的协议。代码处理协议重判、凭据、冷却及耗尽状态。runtime 文档内部仍混有旧单 fallback 的描述，因而不能只按文档里的“一次激活”推断所有链路细节。子 Agent 和辅助任务有不同的继承、恢复规则，也不是一次设置适用所有执行路径。[fallback 配置文档](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/website/docs/integrations/providers.md#fallback-providers)、[fallback 实现](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/agent/chat_completion_helpers.py)、[runtime 文档](https://github.com/NousResearch/hermes-agent/blob/7dab93b06e2bb3757dc18229169efcee1b5b47a3/website/docs/developer-guide/provider-runtime.md)。

源码目录还有签名回放、fallback credential isolation、fallback API mode preservation 等专门回归套件。仅按目录和已读测试验证“有相应测试覆盖”，没有验证全部 suite 是否通过。[Hermes tests/agent](https://github.com/NousResearch/hermes-agent/tree/7dab93b06e2bb3757dc18229169efcee1b5b47a3/tests/agent)。

## Pi / pi-ai

### 统一连接及能力模型

类型区分 `Provider`、`Api`、`Model`，每个 Model 携带协议、provider、base URL、input、reasoning、capacity、pricing 和 protocol-specific compat。支持更多原生协议，例如 Google、Bedrock、Azure、Mistral，而不是全部改写成 OpenAI-compatible。自定义 `models.json` 可声明协议、URL、多个模型；特殊认证或 streaming 用 provider extension。[types.ts](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/ai/src/types.ts)、[model 配置](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/coding-agent/docs/models.md)。

compat 涵盖 developer role、max tokens 字段、thinking dialect、reasoning budget、tool result name、strict schema、finish reason、stream usage、cache retention 等。模型 sampling 默认值、按 thinking level 覆盖和 request sampling 分层合并。目录支持 bundled + 在线 overlay / offline cache；元数据丰富不意味着探测了每一个自定义 endpoint。[types.ts](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/ai/src/types.ts)、[models.md](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/coding-agent/docs/models.md)。

### 历史迁移与统一事件

`transformMessages` 同模型保留必要 signature；换 provider/API/model 时丢弃不可迁移的 redacted thinking，普通 thinking 降级为 text，剥离 tool thought signature 并归一化 tool-call ID，同时更新结果关联。对未回答 tool call 生成错误结果；跳过 aborted/error assistant；不支持图像的目标模型将历史 image 替换为说明文字。因此“会话能继续”包含可预期的信息降级，不能宣传成完全无损跨模型搬运。[transform-messages.ts](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/ai/src/api/transform-messages.ts)。

统一 stream event 区分 text、thinking、tool-call 增量和终结。AssistantMessage 记录来源 model / provider / API、usage、stop reason 和 raw provider stop reason，协议适配器负责 wire conversion。[types.ts](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/ai/src/types.ts)。

### 认证、重试与验证

Coding Agent 明确 runtime key、存储凭据、models.json、环境 / 云凭据的优先级，支持 OAuth、API key、credential command、AWS / Vertex ambient auth。[模型认证说明](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/coding-agent/docs/models.md#authenticate)、[providers.md](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/coding-agent/docs/providers.md)。

provider retry helper 按连接错误、408、409、429、5xx 和 `x-should-retry` 判断，尊重 Retry-After 并设置最大等待，等待可取消；这不是 automatically picking another vendor。[provider-retry.ts](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/ai/src/utils/provider-retry.ts)。

有跨 provider handoff、同 provider 不同 model、tool ID normalization、tool call 无结果、Responses reasoning replay、abort 等专门测试，包括需要真实模型凭据的测试。验证应同时看 mock 与 real multi-turn 测试，不能把一次 ping 当适配成功。[cross-provider-handoff.test.ts](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/ai/test/cross-provider-handoff.test.ts)、[tool-call-id-normalization.test.ts](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/ai/test/tool-call-id-normalization.test.ts)、[Responses replay E2E](https://github.com/earendil-works/pi/blob/f10993bc7f28145df1375f3ff39c7f5c4cfc05f0/packages/ai/test/openai-responses-reasoning-replay-e2e.test.ts)。

## 适合本项目后续对照的验收项（分析建议）

以下是依据上述实现推导的工程验收项，不是任何项目官方宣布的行业标准：

- Provider ID / credential / endpoint、wire protocol、model capability 可以分别配置；代理地址可显式选择协议。
- reasoning 参数及 output limit 等允许按 provider/model 覆盖；明确 unknown 与 unsupported，不把无法确认当 false。
- 至少通过一次真实的“reasoning → tool call → tool result → final answer → 下一轮”调用，再验证 streaming、resume 和换模型。
- 同模型保留必要 replay metadata；跨模型转换有明确降级规则；截断、缺结果、签名拒绝可恢复。
- 错误区分 auth、invalid parameter、rate limit、quota、timeout、context overflow，重试策略不将永久错误无限重复。
- 多模型路由按研究 Agent 的实际职责配置；fallback 另行声明触发范围、凭据来源与预算。

范围限制：未比较 Claude Code / Codex / OpenCode 等闭源或其他产品；未测 latency / cost / task success；未确认发行包与固定源码 HEAD 一致；没有据此给整个产品做优劣排名。

## 当前项目检查结果与建议

本节检查的是 2026-10-08 工作区文件，包含已有未提交修改，不仅是 Git HEAD。未调用真实模型 API；只做源码检查及一个离线请求构造实验。未修改业务代码或自动提交。

### 已有基础

- Agent 通过 LangChain `BaseChatModel`、统一消息与工具接口调用模型；`ProviderSummarizationMiddleware` 在执行时注入用户模型并使用实际模型进行摘要。因此项目已经具备协议适配边界，无需再复制一套 Pi 消息循环。[运行时模型](../../src/deep_data_research_agent/providers/models.py)
- 已有 Chat Completions、Responses、Anthropic 三种协议；YAML 支持厂商默认能力及精确模型覆盖；有流式输出、结构化输出策略和输出 token 参数转换。[能力配置](../../src/deep_data_research_agent/providers/model_profiles.yaml)
- 已有用户凭据加密、配置版本、按用户缓存与更新后失效，角色执行 profile，以及沙箱、异步任务、报告交付、记忆、观测、单元测试与 CI。这些是数据研究工作台的已有投入，不能因通用 Harness 的 provider 更多就否定。[README](../../README.md)、[模型测试](../../tests/test_model_provider.py)、[CI](../../.github/workflows/ci.yml)

### 主要缺口

| 缺口 | 当前源码证据 | 实际影响 |
|---|---|---|
| 连接身份与协议不能由用户分别指定 | API 请求只有 base_url/model_name/api_key；运行时按域名与模型匹配重算协议；未知域名固定 Chat Completions | 原生 Responses/Anthropic 中转地址无法仅通过用户配置正确接入 |
| 能力和参数覆盖较少 | 静态 YAML，未提供用户能力覆盖、reasoning effort/budget 或 sampling 配置；构建器固定 temperature=0 | 新模型或网关的差异需要代码/部署维护，难以按角色调节成本与效果 |
| reasoning 兼容处理依赖回顾捕获上下文 | ReviewCaptureMixin 三个转换方法均依赖 ACTIVE_CAPTURE；FailureReviewMiddleware 仅 supervisor/data-analyst 设置上下文 | reviewer、crawl-worker 和直接调用路径缺少独立保证；这不是所有已配置 Responses 模型均有缺陷的结论 |
| 同一用户只能配置一个在线模型 | UserModelProvider 以 user_id 为主键，各角色 get_runtime_model 都解析该用户单配置 | 目前不能分别为主 Agent、抓取、复核和后台回顾选择不同模型 |
| 换模型后的历史兼容策略未单独定义 | Provider 更新清缓存，业务层未见类似来源感知的历史转换机制；底层 SDK 会做部分转换 | 仍需验证签名、reasoning、tool ID 与未完成工具调用的迁移，不能直接断言所有切换必失败 |
| 用户连接测试仅覆盖一次文本请求 | test_provider_model 执行 Return exactly: OK；已有 mock 和单次任务 baseline，不等于跨厂商多轮兼容矩阵 | 测试通过不足以保证完整 Agent 多轮任务可用 |

定位：[请求 schema](../../src/deep_data_research_agent/api/schemas.py)、[域名解析](../../src/deep_data_research_agent/providers/model_profiles.py)、[解析服务](../../src/deep_data_research_agent/providers/service.py)、[用户配置表](../../src/deep_data_research_agent/database/models.py)、[推理转换](../../src/deep_data_research_agent/core/review_capture.py)、[回顾中间件](../../src/deep_data_research_agent/memory/service.py)、[任务评测](../../src/deep_data_research_agent/evaluation/runner.py)。

### 离线验证

显式用假 key 和 example.invalid 地址构造 HarnessChatOpenAI，传入 additional_kwargs 含 reasoning_content 的 AIMessage，仅调用 _get_request_payload，不发送网络请求。相同消息得到：

```text
without_capture_replays_reasoning: False
with_capture_replays_reasoning: True
```

这个实验确认了捕获上下文与兼容性行为的耦合；没有测量某个真实模型的任务成功率。

### 建议优先级（工程判断）

1. 首先允许显式协议覆盖，域名推断仅作为默认；将 reasoning 读取/回传从可选回顾捕获中独立出来，并覆盖全部调用路径。
2. 然后建立三种协议的多轮工具调用回归：thinking、流式、工具结果、结构化输出、恢复、上下文压缩及网关。沿用现有 task eval，补比较所需的多模型重复实验。
3. 再增加有限的 provider/model 参数与能力覆盖，以及按角色选模型。OAuth、更多云端原生协议、插件 registry 和跨厂商 fallback 由真实需求决定；fallback 前先验证历史迁移并声明费用、数据去向和触发条件。

结论属于工程成熟度评估：基础分层已经接近这些实现，主要欠缺可配置性、兼容策略的独立性与持续验证。没有共同任务、模型、工具和重复运行数据，不能估计产品效果差距的百分比。
