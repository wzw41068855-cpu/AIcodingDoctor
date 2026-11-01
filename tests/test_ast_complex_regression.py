"""复杂项目回归测试：解析 fixture 源码，不导入或执行 fixture。"""
import ast
import io
from pathlib import Path
import tokenize
import unittest

from langchain_core.documents import Document
from rag.splitter.ast_segmenter import AstCodeSegmenter, has_python_code
from rag.splitter.code_splitter import CodeSplitter

FIXTURES = Path(__file__).parent / 'fixtures' / 'ast_complex_project'


class TestComplexAstRegression(unittest.TestCase):
    def source(self, name='service.py'):
        return (FIXTURES / name).read_text(encoding='utf-8')

    def chunks(self, source, size=10000, overlap=0, base=1):
        return CodeSplitter(size, overlap).split([Document(
            page_content=source, metadata={'file_type': '.py',
            'relative_path': 'sample/service.py', 'start_line': base})])

    def named(self, chunks, name):
        return [c for c in chunks if c.metadata['qualified_name'] == name]

    def assert_positions(self, source, chunks, base=1):
        for i, chunk in enumerate(chunks):
            m, text = chunk.metadata, chunk.page_content
            a, b = m['start_index'], m['end_index']
            self.assertEqual(source[a:b], text)
            self.assertEqual(m['chunk_index'], i)
            first = a + len(text) - len(text.lstrip())
            last = b - (len(text) - len(text.rstrip())) - 1
            self.assertEqual(m['start_line'], base + source[:first].count('\n'))
            self.assertEqual(m['end_line'], base + source[:last].count('\n'))

    def test_01_multiline_stacked_decorators(self):
        c = self.named(self.chunks(self.source()), 'create_token')[0]
        self.assertTrue(c.page_content.startswith('# 生成令牌'))
        self.assertIn('@trace(\n', c.page_content)
        self.assertIn('@cache\nasync def', c.page_content)
        self.assertEqual(c.metadata['symbol_type'], 'function')

    def test_02_nested_sync_and_async_functions_stay_in_outer(self):
        chunks = self.chunks(self.source())
        c = self.named(chunks, 'create_token')[0]
        self.assertIn('def normalize', c.page_content)
        self.assertIn('async def persist', c.page_content)
        self.assertFalse(self.named(chunks, 'normalize'))
        self.assertEqual(sum(x.page_content.count('async def persist') for x in chunks), 1)

    def test_03_decorated_class_header_and_attributes(self):
        c = self.named(self.chunks(self.source()), 'TokenService')[0]
        self.assertTrue(c.page_content.startswith('# 服务类说明\n@dataclass'))
        self.assertIn('ttl: int', c.page_content)
        self.assertIn('"enabled": True', c.page_content)
        self.assertNotIn('def verify', c.page_content)

    def test_04_attributes_between_and_after_methods(self):
        chunks = self.chunks(self.source())
        class_text = ''.join(c.page_content for c in self.named(chunks, 'TokenService'))
        self.assertIn('label = "令牌验证"', class_text)
        self.assertIn('final_flag = True', class_text)
        self.assertNotIn('label =', self.named(chunks, 'TokenService.verify')[0].page_content)

    def test_05_property_getter_setter_not_merged(self):
        chunks = self.named(self.chunks(self.source()), 'TokenService.expires')
        self.assertEqual(len(chunks), 2)
        self.assertIn('@property', chunks[0].page_content)
        self.assertIn('@expires.setter', chunks[1].page_content)
        self.assertTrue(all(c.metadata['parent_class'] == 'TokenService' for c in chunks))

    def test_06_nested_class_metadata(self):
        c = self.named(self.chunks(self.source()), 'TokenService.Policy.accepts')[0]
        self.assertEqual(c.metadata['parent_class'], 'TokenService.Policy')
        self.assertEqual(c.metadata['symbol_type'], 'method')
        self.assertIn('# 嵌套类方法\n        @classmethod', c.page_content)

    def test_07_local_class_stays_in_factory(self):
        chunks = self.chunks(self.source())
        self.assertIn('class Local:', self.named(chunks, 'factory')[0].page_content)
        self.assertFalse(self.named(chunks, 'factory.Local.run'))

    def test_08_conditional_definition_stays_in_module(self):
        chunks = self.chunks(self.source())
        c = next(c for c in chunks if 'def conditional()' in c.page_content)
        self.assertEqual(c.metadata['symbol_type'], 'module')
        self.assertIn('if DEFAULT', c.page_content)

    def test_09_file_header_not_attached_to_function(self):
        chunks = self.chunks(self.source())
        self.assertIn('SPDX-License', chunks[0].page_content)
        self.assertIn('复杂切分测试项目', chunks[0].page_content)
        self.assertNotIn('SPDX', self.named(chunks, 'create_token')[0].page_content)

    def test_10_strings_with_fake_definitions(self):
        source = self.source('strings.py')
        chunks = self.chunks(source)
        self.assertEqual([c.metadata['symbol_name'] for c in chunks], ['', 'render'])
        self.assertTrue(chunks[1].page_content.startswith('# 真正函数'))
        self.assertIn('# def fake', chunks[0].page_content)

    def test_11_long_function_parts_and_offsets(self):
        source = self.source('long_tasks.py')
        chunks = self.chunks(source, 180, 30)
        parts = self.named(chunks, 'build_report')
        self.assertGreater(len(parts), 5)
        self.assertEqual([c.metadata['ast_part_index'] for c in parts], list(range(len(parts))))
        self.assertTrue(all(c.metadata['ast_part_count'] == len(parts) for c in parts))
        self.assertTrue(all(len(c.page_content) <= 180 for c in chunks))
        self.assertFalse(any('def finish' in c.page_content for c in parts))
        self.assert_positions(source, chunks)

    def test_12_all_fixture_lines_and_offsets(self):
        for path in sorted(FIXTURES.glob('*.py')):
            with self.subTest(file=path.name):
                source = self.source(path.name)
                self.assert_positions(source, self.chunks(source))

    def test_13_crlf_unicode_and_base_line(self):
        source = self.source().replace('\n', '\r\n')
        chunks = self.chunks(source, 220, 20, base=41)
        self.assert_positions(source, chunks, 41)
        self.assertTrue(any('令牌' in c.page_content for c in chunks))

    def test_14_no_final_newline_and_tabs(self):
        source = 'class Tabs:\n\t# 中文说明\n\tdef run(self):\n\t\treturn "中文"'
        chunks = self.chunks(source)
        self.assertTrue(self.named(chunks, 'Tabs.run')[0].page_content.startswith('\t#'))
        self.assert_positions(source, chunks)
        self.assertEqual(chunks[-1].metadata['end_index'], len(source))

    def test_15_token_coverage_without_duplicate_ast_segments(self):
        source = self.source()
        segments = AstCodeSegmenter().split(source)
        for a, b in zip(segments, segments[1:]):
            self.assertLessEqual(a.end_index, b.start_index)
        offsets = [0]
        for line in source.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        ignored = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                   tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in ignored:
                continue
            offset = offsets[token.start[0] - 1] + token.start[1]
            self.assertEqual(sum(s.start_index <= offset < s.end_index for s in segments), 1)

    def test_16_long_comments_and_long_single_line(self):
        source = '# explanation\n' * 50 + 'def huge():\n    return "' + '中' * 500 + '"\n'
        chunks = self.chunks(source, 100, 10)
        self.assertTrue(chunks)
        self.assertTrue(all(has_python_code(c.page_content) for c in chunks))
        self.assert_positions(source, chunks)

    def test_17_unicode_line_separator_inside_string(self):
        source = 'TEXT = "中文\u2028文本"\n# 函数说明\ndef run():\n    return TEXT\n'
        chunks = self.chunks(source)
        self.assertEqual([c.metadata['symbol_name'] for c in chunks], ['', 'run'])
        self.assertTrue(chunks[1].page_content.startswith('# 函数说明'))
        self.assert_positions(source, chunks)

    def test_18_formfeed_inside_string(self):
        source = 'TEXT = "a\fb"\ndef run():\n    return TEXT\n'
        ast.parse(source)
        chunks = self.chunks(source)
        self.assertEqual([c.metadata['symbol_name'] for c in chunks], ['', 'run'])
        self.assert_positions(source, chunks)


if __name__ == '__main__':
    unittest.main()
