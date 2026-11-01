# V0.2.1 — AST metadata 标签升级

## 为什么要做

原有 `PythonCodeTextSplitter` 按字符切分，可以保留字符偏移、源文件路径和起止行号，但不了解函数、类、方法之间的层级关系。现在使用 Python 标准库 `ast` 只提取结构，不重写 LangChain 切分器。

## 修改文件

- `rag/splitter/ast_tagger.py`（新增）：使用 `ast.NodeVisitor` 提取 `ClassDef`、`FunctionDef`、`AsyncFunctionDef`；根据代码 Chunk 的 `[start_line, end_line]` 与结构的行号交集赋予主要符号和关联符号。
- `rag/splitter/code_splitter.py`：保留原字符偏移/行号算法；每个文件的 Chunk 全部计算完行号后调用 `AstTagger.enrich()`。
- `rag/vector_store.py`：`splitter_version` 由 1 升级至 2；旧源文件即使 Hash 不变，也需要重新索引以写入新 metadata。
- `agent/tools.py`：`create_retriever_tool` 的 `document_prompt` 添加模块、主要符号、类型和关联符号，仍然使用官方 Retriever 工具。
- `tests/test_ast_tagger.py`：包含函数/类/嵌套函数、非 Python 文件、语法错误回退测试。

## metadata 含义

- `module_path`：模块路径，例如 `app.services.user`。
- `symbol_name`：与当前 Chunk 重叠最多的主要函数、方法或类的短名称。
- `symbol_type`：`function` / `method` / `nested_function` / `class` / `module` / `document`。
- `qualified_name`：限定名称，例如 `UserService.login`。
- `parent_class`：外围类的名称，嵌套类可带层级。
- `symbol_names`：该 Chunk 涉及的全部符号的 JSON **字符串**，为兼容 Chroma metadata 类型限制，**不是** Python 列表。

一个 Chunk 可能跨越多个函数或同时包含类和方法，**主要符号只是提示，不保证 Chunk 全部内容只属于该函数**。精确查找任意符号仍应使用后续的 `search_symbol` 工具。`ast.parse` 不会执行源码，语法不完整时退化成普通文本索引。

## 如何使用

1. 备份当前索引（特别是 `.env`、`data/chroma`），将本压缩包源码合并到原项目。
2. 保留原 API Key 配置。首次升级正常执行原本的 `Loader -> VectorStoreManager.sync_documents(documents, splitter)`。无需手工清空 Chroma：因索引版本变更，旧文件将重新切分和索引，消耗一定 Embedding Token。
3. **先执行索引同步，再启动 Agent 问答**。若还没同步，新版 `document_prompt` 可能因旧向量缺少 `symbol_name` 等字段而报错。
4. 打印任意新生成 Chunk 的 `metadata`，检查 `qualified_name`、`start_line`、`end_line`。
5. 在你的 PyCharm 项目环境中执行：`python -m unittest discover -s tests -p test_ast_tagger.py -v`。

## 当前边界

- 本版并未按 AST 函数边界重新切块；原有 Chunk 行号和文本内容保持不变。
- AST 的 `lineno` 通常从 `def`/`class` 行开始，装饰器行不完全包含在定义起始行内。
- 标签来自静态 AST，不代表运行时真实调用关系（动态导入、装饰器、依赖注入等）。
- 若用户手动修改了 `AstTagger` 规则，应再次提升 `splitter_version`，否则相同源码可能被 Hash 增量索引跳过。
- 当前运行环境未安装 LangChain，已完成 Python 静态编译和使用 Document stub 的三项纯逻辑测试，**未执行真实 Chroma/Embedding 联调**。
