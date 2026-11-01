# AICodingDoctor V0.2 - Agent 中间件与 Callback 改造

> 以提交的 V0.1 实际代码为基线，只更改 Agent 与观测相关文件。所有已有 Loader、Splitter、Chroma 增量索引、RAGChain 等模块保留。

## 一、首先怎么运行

1. 用本次包里的 `agent/code_agent.py`、`agent/middlewares.py`、`agent/prompts.py`、`utils/agent_observability.py`、`config.py` 和 `main.py` 更新原工程。`rag/vector_store.py` 加了 `load_dotenv` 的调用，其他核心功能不改。
2. 不要覆盖现有的 `data/chroma`、`data/token_usage.jsonl`、`logs` 或 `.env`。**交付包刻意没有包含这些用户私有的运行数据和 Git 历史**。
3. 安装依赖：`python -m pip install -r requirements.txt`。
4. 保持 PyCharm 环境变量 `DASHSCOPE_API_KEY`，或者复制 `.env.example` 为 `.env` 后填写密钥。
5. 执行 `python main.py`，默认调用 `my_test_agent()`；已有 Chroma 索引仍可继续使用。
6. 输入 `quit`、`exit` 或 `q` 可以直接退出，不进入 LLM，避免之前的无意义 Token 消耗。

## 二、各文件与职责

| 文件 | 职责 |
|---|---|
| `agent/prompts.py` | `AgentContext(mode=...)`、三套提示词、公共安全规则 |
| `agent/middlewares.py` | 内置上下文清理、历史摘要、调用限制；`@dynamic_prompt`；组装配置 |
| `agent/code_agent.py` | 仅创建 ChatOpenAI、官方检索工具、Checkpointer 和 Agent；`ask()` 不再处理 Token 统计细节 |
| `utils/agent_observability.py` | 通过 `BaseCallbackHandler` 记录模型/工具，复用 `UsageMetadataCallbackHandler` 汇总每轮 Token 并写 JSONL |
| `utils/logger.py` | 原有项目日志系统，统一控制 DEBUG 和日志文件，没有重复替换 |
| `utils/token_tracker.py` | 原有 JSONL 写入与累计方法继续复用 |
| `config.py` | 中间件阈值以及模型调用、工具调用次数限制 |

## 三、六个能力为什么要这么设计

### 1. ContextEditingMiddleware（清理旧 ToolMessage）
- 默认 `CONTEXT_EDIT_TRIGGER_TOKENS=8000`、`CONTEXT_EDIT_KEEP_TOOL_RESULTS=3`。
- 原因：向量检索返回的每个代码片段都会被短期记忆保留；它们经常比用户问题长得多。先清理旧工具返回**不需要额外调用摘要模型**。
- 注意：旧代码被替换为占位文本；想要原样内容，需要再调用检索工具。

### 2. SummarizationMiddleware（压缩较早对话）
- 默认 `SUMMARY_TRIGGER_TOKENS=12000`、`SUMMARY_KEEP_MESSAGES=8`。
- 原因：即使删除旧工具结果，对话历史也会继续增加；将较早消息摘要可以保留话题连贯性。
- **代价：摘要本身会额外调用一次 LLM，可能消耗 Token**；因此不是每轮都会省钱，也不是越早摘要越好。
- 目前复用同一个聊天模型生成摘要；之后额度紧张时可换成本更低的摘要模型。
- 内置 token 估算与阿里云精确计费可能有差异，实际根据 `token_usage.jsonl` 调整阈值。
- 当消息摘要或清理后，旧代码的引用/行号不能保证仍然保留，请重新检索核对。

### 3. @dynamic_prompt（不需要额外的 LLM 意图分类）
- 通过 `agent.ask(question, thread_id, mode=...)` 指定 `normal`、`debug`、`review`。
- 使用 `context_schema=AgentContext`；`AgentRunMonitor` 在单次 `invoke(context=AgentContext(...))` 中传入模式，内置 `@dynamic_prompt` 每次模型调用前选择规则。
- `mode` 是**本次执行的运行时依赖**，不会附加进历史 `messages`；这样既省 Token，也不会污染记忆。
- `CodeAgent` 不再同时传固定 `system_prompt`，避免两份系统规则重复/冲突。

### 4. ModelCallLimitMiddleware（限制 LLM 调用）
- `AGENT_MODEL_CALL_LIMIT=5`、`exit_behavior='end'`。
- 限制的是**一轮执行中 Agent 的模型调用数**，不是整个会话累计数，也不是 Token 的精确预算。
- 触及上限时内置机制结束本次执行，结果可能不完整；应向用户说明需缩小问题范围。
- 摘要自身、第三方内部 API 的额外请求和失败重试不一定计入这个 Agent 图模型调用限制，实际费用要看服务商用量。

### 5. ToolCallLimitMiddleware（限制代码搜索）
- `AGENT_SEARCH_TOOL_LIMIT=4`、仅限制 `search_code`、`exit_behavior='end'`。
- 同一轮如果模型反复尝试检索，超额后会终止，不让它无限搜索；同一个线程下一轮重新计数。
- **并行工具调用也会消耗额度**，所以比依赖 `recursion_limit` 更直观。

### 6. Callback（日志和 Token 与业务解耦）
- `AgentLoggingCallback` 记录模型输入、原始响应、工具参数与结果。
- INFO 只记录概要，DEBUG 输出完整源码等；现有 `config.DEBUG_MODE=True` / `config.LOG_TO_FILE=True` 控制输出。
- `UsageMetadataCallbackHandler()` 是官方的用量统计工具，每次提问**重新创建**，避免多轮重复计算。
- `AgentRunMonitor` 在成功或失败时汇总本轮 Token，用原来的 `TokenUsageTracker` 追加 JSONL。模型未返回用量时记为未知而非零。
- 回调捕获的 Token 不等于阿里云账户余额，也不包含检索时的 Embedding 成本；失败/重试可能缺少服务商 usage。

## 四、控制台操作

```
你：mode debug
任务模式已切换为：debug
你：为什么登录失败会返回 401？
... Agent 检索代码并诊断 ...
你：mode review
任务模式已切换为：review
你：审查登录接口的异常处理
... Agent 进行审查 ...
你：new
已创建新对话
你：history
... 查看完整消息（含工具调用） ...
你：quit
# 直接退出，不调用模型
```

若想只测试 Prompt 组合，不调用模型：

```python
from agent.prompts import build_system_prompt
print(build_system_prompt('normal'))
print(build_system_prompt('debug'))
print(build_system_prompt('review'))
```

## 五、验收清单（需要真实百炼 API 环境）

- [ ] 启动后 `create_agent()` 无 Middleware 参数异常。
- [ ] 输入 `quit` 不产生新的 Token 用量日志。
- [ ] 使用同一 thread_id 连续追问保留上下文；输入 `new` 后历史隔离。
- [ ] `mode debug` / `mode review` 的回复结构不同，但沿用同一会话。
- [ ] Debug 模式可以查看完整 Tool 调用及检索输出；关闭 DEBUG 则隐藏详情。
- [ ] 人为降低 `CONTEXT_EDIT_TRIGGER_TOKENS` 验证工具清理会触发。
- [ ] 人为降低 `SUMMARY_TRIGGER_TOKENS`，多轮对话后验证摘要触发（会额外消耗模型额度）。
- [ ] 提一个可能触发多次检索的复杂问题，观察 4 次 Tool / 5 次 Model 限制。
- [ ] `token_usage.jsonl` 每轮只新增一次记录，累计值没有重复计算。

## 六、版本说明与限制

- 依赖 LangChain **1.x** 的现代 Agent/Middleware API；安装旧版 0.x 不能运行。
- 开发环境未配置百炼 API，也无法从此环境安装 LangChain，因此已通过 Python 编译检查和依赖无关单元测试，**尚未声称完成真实 API 联调**。请在自己的 PyCharm 运行环境按上一节验收。
- 本次没有实施流式输出、持久化 Checkpointer、严格 Token 预算、Token 计费查询和仓库版本追踪。
- 持久化 Chroma 向量库和当前源码版本之间的一致性仍沿用 V0.1 的 `sync_documents()` 设计；没有改变数据索引。
- `DEBUG_MODE=True` 可能记录完整私人代码、数据库连接信息和返回文本；私有日志不可上传 GitHub 或云端追踪平台，建议日常使用时设为 `False`。
