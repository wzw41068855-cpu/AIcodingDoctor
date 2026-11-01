# V0.2.4 P0：最终模型请求中的 Chunk 去重

本次只压缩发给模型的重复检索内容。查询缓存、并发合并、索引更新均未实现。
Checkpoint 和工具执行结果仍保留完整内容与 artifact；模型请求使用消息副本。

## 改动及职责

- `agent/tool_documents.py`：原有 Document 模板与分隔符共用，使用官方 `format_document()`。
- `agent/tools.py`：仍使用官方 `create_retriever_tool()`，仅启用 `content_and_artifact`。
- `agent/result_dedup.py`：单一模型请求 Middleware，同步和异步调用共用纯消息转换逻辑。
- `agent/middlewares.py`：在包装链内侧加入去重，更新真实 Hook 顺序说明。
- `tests/test_tool_result_dedup.py`：29 项参数展开后的测试，含真实 Agent 调度和字符评估。
- `tests/test_agent_middleware_wiring.py`：改为验证去重位置，不硬编码旧 Middleware 数量。

Callback、CodeAgent、向量数据库和 AST 切分器没有修改。测试统计只存在于测试辅助函数中。

## 可见性、身份与失效

每次模型请求从零扫描 `request.messages`，只把此前实际保留全文的 Chunk 记入局部集合。
不共享运行状态，也不保存“见过”标记。不同 invoke 可以引用同一 thread 中仍可见的历史全文；
旧全文被清理或摘要移除后，后续副本完整发送。

仅识别已配对的 `search_code` 成功 ToolMessage。artifact 必须是 Document 列表，或默认
InMemorySaver 恢复的严格标准 Document 字段字典；字典只在局部转为 Document，原 artifact 不变。
使用官方模板重新渲染并检查整个 content 完全相等，不通过字符串分隔来猜测 Chunk 边界。
清理标记、格式不一致、缺元数据、非标准 artifact、错误状态均安全透传。

身份由已有 Document ID、来源路径、字符区间或行号区间、内容 SHA256 以及已有
repository/collection/source/file_hash/signature 字段组成。没有 ID 时允许可靠区间回退；
来源或标识不足则不压缩。哈希仅用于当前请求比较，不创建另一套向量索引 ID。

## 实际 Hook 顺序

已检查项目安装的 LangChain 1.2.12、langchain-core 1.2.18、LangGraph 1.1.2、
langgraph-checkpoint 4.0.1 的实现：

1. `Summarization.before_model` 在模型节点前更新消息状态。
2. 进入模型包装链，`ContextEditing.wrap_model_call` 修改请求副本。
3. 动态提示词后，最内侧 `ChunkDedup.wrap_model_call` 读取修改后的请求。
4. 最终调用模型；Checkpoint 中不写入压缩结果。

配置列表中 ContextEditing 虽在 Summarization 前面，其 Hook 执行阶段不同，不能据列表推断
“先清理再摘要”。模型包装器实际以外层包内层组合。工具仍由官方 ToolNode 调度，调用限额不变。

集成测试使用真实 `create_agent()`，覆盖生产 `build_middlewares()` 配置，以及同步、异步的
清理/摘要场景。假模型记录实际输入，证明旧全文消失时后续结果重新完整发送。

## 前后示例

第一次结果完整保留模板、来源和源码。第二次相同 Chunk 的模型请求内容变为：

```text
[重复代码，已在本次模型上下文中提供]
文件：a.py
主要符号：run
行号：10-71
请使用先前工具调用 one 的第 1 个结果中的完整代码。
```

同一工具结果混合新旧 Chunk 时仅替换重复部分；同消息内重复可以引用前面已显示的结果序号。
所有原始 tool_call_id、AIMessage.tool_calls、消息 ID、状态及 artifact 均保留。

## 测试与可复现评估

完整结果：**73 passed, 3 subtests passed**，无跳过。`git diff --check` 通过。
使用项目配置的 Python 3.13.12。常规 pytest 临时目录在当前环境不可访问，改为本次新建的
项目内临时目录后完整通过；不需要修改测试或操作现有数据。

测试覆盖身份冲突、混合结果、非标准结果、Document 字典重载、消息和 artifact 不变、
并行工具结果、跨 thread/跨 invoke、摘要及清理后恢复全文、工具限额、检索失败、全部重复时结束。
每条引用均检查在同次模型请求中有此前可见的完整副本。

固定工具调用序列：`q1 → [A,B]`、`q2 → [A,C]`、`q3 → [A,B,C]`，然后最终回答。
启用与禁用去重均执行真实 Agent 图和相同的假模型输出，模型输入被逐次记录。
这里统计的是 Unicode 字符数，不是字节数或 Token。

| 模型请求 | 工具内容字符（前→后） | 完整 Chunk（前→后） | 重复完整 Chunk（前→后） | 悬空引用 |
| --- | ---: | ---: | ---: | ---: |
| 首次提问 | 0 → 0 | 0 → 0 | 0 → 0 | 0 |
| q1 后 | 2457 → 2457 | 2 → 2 | 0 → 0 | 0 |
| q2 后 | 4914 → 3764 | 4 → 3 | 1 → 0 | 0 |
| q3 后 | 8603 → 4003 | 7 → 3 | 4 → 0 | 0 |

跨模型请求累计发送字符为 **15974 → 10224**，减少 **5750（约 36.0%）**。
两组 Retriever 执行次数均为 3，返回 Document 数均为 7。没有真实 API 调用，
真实 input_tokens 和真实答案质量均未验证；假模型的固定回答只能验证协议及来源内容保留。

在项目根目录复现（使用 PyCharm 配置的解释器）：

```powershell
# 精确复现评估，不需要临时目录或 API：
python -m pytest tests/test_tool_result_dedup.py -k fixed_sequence -q -s -p no:cacheprovider

# 全量测试：为 --basetemp 指定新建且为空的临时目录，避免覆盖已有目录。
python -m pytest tests -q -p no:cacheprovider --basetemp <新建的空临时目录>
```

## 限制

- P0 不降低检索次数或 Checkpoint 存储量；摘要模型的输入仍可能含重复全文。
- 非标准结果、身份不明确或模板不一致时放弃压缩，可能保留重复内容。
- 对极短 Chunk，引用文本可能比源码更长，不能保证每个案例字符都减少。
- 升级 LangChain 后应重跑 Hook 顺序和 checkpoint 序列化测试。
- 未执行真实模型评估；没有改变 Chroma、`.env`，没有执行 Git commit/push/reset。
