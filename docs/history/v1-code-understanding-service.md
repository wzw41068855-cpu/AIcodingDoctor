# V1 统一代码理解服务

当前实现仅覆盖 V1：检索索引快照、读取当前源码、精确定位符号、显式更新索引。
不包含修复图、写源码、执行命令、MCP、Docker 或 Web 服务。无新增依赖。

## 使用方式

```python
from services.code_search import CodeSearchService

# 不传 Manager 也能读取源码、定位符号、获取摘要，不需要模型 API。
service = CodeSearchService(repository_root="你的仓库绝对路径")
context = service.get_code_context("agent/code_agent.py", start_line=20, end_line=40)
symbols = service.find_symbol("CodeAgent.ask", path="agent/code_agent.py")
summary = service.get_repository_summary(max_files=100)

# manager 为已配置的 VectorStoreManager；不自动选择或创建其它集合。
service = CodeSearchService(repository_root="你的仓库绝对路径", vector_manager=manager)
results = service.search_code("Agent 如何执行诊断", top_k=5)
stats = service.sync_changed_files(["src/new.py", "src/changed.py", "docs/deleted.md"])
```

构造参数 `retriever` 可注入现有 `HybridCodeRetriever`（也会复用其 Manager），
`splitter` 可注入现有 `CodeSplitter`。同时指定 Retriever 和 Manager 时必须共享同一对象。
不传 Retriever 时，第一次搜索才创建 Hybrid Retriever；需要既有模型 API 配置。
`max_file_bytes` 默认 1 MiB，可显式调整为正整数。

| 接口 | 返回及失败行为 |
| --- | --- |
| `search_code(query, top_k=HYBRID_TOP_K)` | `list[CodeResult]`，最多 top_k 条；空查询/非法数量抛 `ValueError`；缺少依赖抛 `RuntimeError`；底层失败原样传播 |
| `find_symbol(name, path=None)` | 按短名称或 `Class.method` 等限定名称精确匹配，多定义返回多项，未找到返回空列表；空名称抛 `ValueError`；语法/读取错误显式传播 |
| `get_code_context(path, start_line=None, end_line=None)` | 当前磁盘内容；一基首尾闭区间，省略端点取文件首尾；非法范围抛 `ValueError`；不存在抛 `FileNotFoundError` |
| `get_repository_summary(max_files=100)` | `{"files": [...], "truncated": bool}`；只列可访问的 `.py/.md` 路径，不读取内容；数量范围 1～1000，否则抛 `ValueError` |
| `sync_changed_files(paths)` | `{"added": int, "updated": int, "skipped": int, "deleted": int}`；显式路径去重；空列表返回全零；加载部分失败抛 `RuntimeError`，整批不提交 |

## 元数据与读取语义

`CodeResult` 是 Pydantic 模型，可用 `model_dump()` 转为字典：

- `relative_path`：相对仓库路径，统一 `/`；`source`：磁盘来源；`file_type`：后缀。
- `symbol_name`、`qualified_name`、`symbol_type`：来自现有标签或精确 AST 分析。
- `start_line`、`end_line`：一基闭区间；空文件没有真实行，均为 `None`。
- `content`：源码或索引 Chunk 正文，无展示前缀。
- `score`：当前 Hybrid Retriever 不公开最终得分，因此保持 `None`。
- 未提供的元数据保持 `None`，不推测符号、行号或概率。

语义检索返回已提交索引快照：未经同步的磁盘修改不会自动反映到搜索中。
`get_code_context()` 和 `find_symbol()` 每次读取磁盘，不从 Chroma 拼装源码。
精确符号扫描复用 `AstTagger.collect_symbols()` 的现有访问器，包括完整类、方法、
异步函数、嵌套定义及同名定义；结果包含装饰器，不额外扩展前置注释范围。
全仓库扫描遇到语法错误会报错，避免把部分结果伪装成完整定位；可指定文件缩小范围。

读取和同步只允许 `.py/.md`，不额外扩展配置文本。拒绝隐藏路径、忽略目录、
常见密钥/凭据文件名、越界路径、指向仓库外或隐藏目标的符号链接、超限文件、
含 NUL 或非文本控制字符的内容。Markdown 使用 UTF-8，Python 遵循编码声明；
解码错误显式失败。CRLF/CR 归一化为 LF，行号按物理换行计算，字符串内 U+2028
不作为 Python 源码行分隔符。路径校验不是内容脱敏：允许的源码本身不应包含密钥。

## 同步规则与恢复

本服务仅接收显式变更集，始终调用原有 `sync_documents(..., remove_missing=False)`。
不会扫描后根据“没加载到的文件”猜测删除，也不会把部分变更集当全仓库快照。

1. 先校验所有路径，再用 `Loader.load_files()` 读取所有存在的文件，包括空文件。
2. 任一加载失败均登记 `failed_files`，取消整批；不把加载失败、超限或权限错误当删除。
3. 只有明确提供且 `stat()` 确认不存在的路径进入 `deleted_paths`。
4. 新增/修改复用文件 Hash、signature、Chunk ID 和旧同步流程；相同内容跳过切分及 Embedding。
5. 空白/纯注释 Python 文件保留 Hash/signature、使用空 ID 列表，清除旧版本 Chunk。
6. 显式删除可单独提交，因此最后一个文件删除无需向空输入开放全量清库。
7. 新向量写入失败或 manifest 保存失败：旧 manifest 和旧 Chunk 保留，尽力回滚暂存 ID。
8. manifest 提交后才清理旧 Chunk；清理失败抛出异常，但新 manifest 已提交，不能解释为整批回滚。
   下次同步会重试孤儿清理。回滚清理失败也可在下一次同步恢复。

Hybrid Retriever 继续只接受 manifest 有效 ID；BM25 按 manifest 指纹自动重建。
因此提交后清理失败遗留的旧 Chunk 不会成为有效搜索结果。
`top_k` 只控制最后截断/重排数量，Dense/BM25/RRF 候选上限沿用配置，不承诺一定返回足量结果。

旧 `Loader.load()` 与 `main.py` 入口保持兼容。直接使用旧全量同步时，调用方仍必须确认
完整扫描成功且 `failed_files` 为空；不能将其部分结果传给 `remove_missing=True`。
新服务不提供隐式全量删除接口。

## 仓库与 Collection 边界

一个 Collection/manifest 只对应一个本地仓库，只支持单进程、顺序更新。
首次成功同步将规范化绝对根路径保存为现有 manifest 的 `repository_root` 字段，
不另建索引存储协议。旧 Loader 调用可由 `source` 和 `relative_path` 推导根目录。

Service 初始化和搜索只读验证归属，不写 manifest。旧 manifest 没有归属字段时，
必须逐个验证其所有有效 Chunk 的 `source` 与目标仓库匹配；缺失 Chunk、无来源或
无法验证的零 Chunk 条目均拒绝接入。必须恢复正确数据或显式选择独立 Collection；
不自动清库、不修改默认 Collection、不自动迁移旧数据。仓库移动后需要人工处理归属，
不能直接将它当作同一绝对路径仓库接入。

纯模拟 Retriever 无 Manager 时由注入方保证索引来源；服务仍校验结果路径和来源一致性。
文件系统校验不提供对恶意并发替换链接的操作系统级防护；当前边界为可信本地单用户、
无并发索引写入。manifest 原子替换与 Chroma 之间不是跨存储事务。

## 测试方法

在项目 SDK 环境执行：

```powershell
python -m pytest tests/test_code_search_service.py tests/test_hybrid_retriever.py -q
python -m pytest tests -q
```

本次使用 PyCharm SDK `C:\Users\wzw41\develop\miniconda\envs\langgraph\python.exe`。
系统默认临时目录不可访问时，本次实际采用如下参数，GUID 保证每轮使用独立目录：

```powershell
$v1TestTemp = Join-Path (Get-Location) ('.pytest_cache/v1-final-' + [guid]::NewGuid().ToString('N'))
& 'C:\Users\wzw41\develop\miniconda\envs\langgraph\python.exe' -m pytest tests -q --tb=short --basetemp $v1TestTemp
```

全量结果：122 passed、1 skipped、3 subtests passed；唯一跳过项为真实符号链接创建权限不足。
无需该权限的解析后越界测试另行通过。真实 Chroma、Embedding、Rerank、LLM 集成未执行；
当前 SDK 缺少 `langchain_chroma`，本任务未安装依赖或修改模型配置。

新增测试使用临时源码、临时 manifest、内存向量存储及假重排器，调用真实同步与混合检索逻辑，
不请求 Embedding/Rerank/LLM，不操作现有 Collection。覆盖新增、修改、重复同步、删除到零文件、
空文件、加载失败、写入/提交/清理失败、恢复、仓库冲突、路径/行号、符号与 Agent 旧入口。
Windows 未授予符号链接创建权限时相应测试明确跳过。

Chroma 客户端延迟至真实数据库初始化时导入，离线测试不依赖已安装该可选运行组件；
生产索引仍需要 `requirements.txt` 中的 `langchain-chroma`。真实 API 与真实 Chroma 集成需
另选隔离临时 Collection 并明确配置凭据后执行；离线测试结果不能代表真实检索质量或性能。

## V2 接入建议（未实现）

Planner 用搜索和有界摘要收集线索；Coder 在任何编辑前调用 `get_code_context()` 读取最新文件，
并由 V2 单独承担编辑授权、并发与版本校验；Reviewer 用 `find_symbol()` 和当前源码核对定义。
同一工作流共享 Manager、Retriever 和服务实例，编辑成功后显式传入变更路径同步。
现有 CodeAgent 工具仍直接共享底层 Hybrid Retriever，工具名称、`content_and_artifact`、
Middleware、InMemorySaver 和 Token 统计均不变。
