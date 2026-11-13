# AST-first 复杂项目回归报告

测试源码位于 `fixtures/ast_complex_project/`，只读取和解析，不执行其中的函数或装饰器。
沿用仓库已有 `tests/` 目录，新增 `test_ast_complex_regression.py` 的 18 项测试。

覆盖多行/堆叠装饰器、异步函数、嵌套函数、局部类、嵌套类、类属性、property
getter/setter、条件定义、许可证文件头、多行字符串伪定义、超长函数、重复语句、
超长单行、纯注释过滤、CRLF、中文、Tab、末尾无换行、非 1 起始行号、字符偏移、
AST 段无重复覆盖及每个实际代码 Token 的完整覆盖。

## 实际结果

| 文件 | chunk_size=1000 / overlap=150 | chunk_size=180 / overlap=30 |
| --- | ---: | ---: |
| service.py | 12 | 14 |
| long_tasks.py | 4 | 10 |
| strings.py | 2 | 2 |
| 合计 | 18 | 26 |

默认参数下 `create_token` 包含 10～24 行的说明注释、全部装饰器和内层函数。
`TokenService.verify` 包含 37～40 行；类属性分别保留在类级段；嵌套方法使用
`TokenService.Policy.accepts` 与正确的 `parent_class`。getter/setter 虽然同名，
仍形成两个独立 AST 段。条件定义保留在模块控制流中，局部类保留在外层函数中。

所有输出 Chunk 均校验 `source[start_index:end_index] == page_content`，
行号根据原始源码中的换行独立计算。二次切分允许配置的 overlap；AST 一级段不重叠。

## 发现与改进

初次新增测试：16 项通过，2 项失败。字符串中的 U+2028 或换页符会被
`str.splitlines()` 错认成源码换行，造成 AST 行号与字符映射错位，甚至把函数的
`return` 切成 module Chunk。已改为只按 CR/LF/CRLF 建立源码行边界。
这两个用例修复后通过，切分算法版本从 4 升至 5。

最终执行 `python -m unittest discover -s tests -p 'test_ast*.py' -v`：
36 项全部通过，无跳过，包含 18 项新增测试、15 项既有 AST-first 测试和 3 项 AstTagger 测试。
`git diff --check` 通过。测试使用 PyCharm 配置的 Python 3.13.12 环境。

当前保持 AST-first 主流程及切分参数。长注释块超过阈值时，二次切分可能产生纯注释
片段并被过滤，因此并不保证任意长度的前置注释全部保留；条件定义不独立打符号标签，
也是当前策略的限制。上述结果证明边界和 metadata 的行为，不代表检索 Recall/MRR 已提升。

## 复现

在项目根目录执行：

```powershell
python -m unittest discover -s tests -p 'test_ast_complex_regression.py' -v
python -c "import runpy; runpy.run_path('tests/inspect_ast_complex.py', run_name='__main__')"
```

第二条命令输出逐 Chunk 的 JSON 报告，包括行号、字符区间、长度、符号及内容预览。
不访问向量数据库或调用 LLM。已有向量索引尚未重建；下一次 `sync_documents()`
会因版本 5 触发重新切分与 Embedding。保留旧索引做 A/B 对比时应先选择独立 Collection。
