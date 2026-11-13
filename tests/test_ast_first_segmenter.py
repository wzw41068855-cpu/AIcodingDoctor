"""AST-first 第一阶段纯标准库测试；不需要 LangChain 或 API Key。

运行：python -m unittest discover -s tests -p test_ast_first_segmenter.py -v
"""
import unittest

from rag.splitter.ast_segmenter import AstCodeSegmenter, has_python_code


class TestAstFirstSegmenter(unittest.TestCase):
    def setUp(self):
        self.splitter = AstCodeSegmenter()

    def test_decorators_class_methods_and_module_comments(self):
        source = (
            '# top comment\n'
            'import os\n'
            '\n'
            '@class_decorator\n'
            'class UserService:\n'
            '    """A class."""\n'
            '    key = 123\n'
            '\n'
            '    @staticmethod\n'
            '    def login(name):\n'
            '        return name\n'
            '\n'
            '    # a comment between methods\n'
            '    def logout(self):\n'
            '        return True\n'
            '\n'
            'DEFAULT = 42\n'
        )
        parts = self.splitter.split(source)
        texts = [source[s.start_index:s.end_index] for s in parts]
        self.assertEqual(''.join(texts).replace('\n\n', '\n').count('def login'), 1)
        login = next(s for s in parts if s.qualified_name == 'UserService.login')
        self.assertTrue(source[login.start_index:login.end_index].startswith('    @staticmethod'))
        self.assertEqual(login.symbol_type, 'method')
        head = next(s for s in parts if s.qualified_name == 'UserService')
        self.assertIn('@class_decorator', source[head.start_index:head.end_index])
        self.assertIn('key = 123', source[head.start_index:head.end_index])
        self.assertTrue(any('DEFAULT = 42' in t for t in texts))
        self.assertTrue(any('comment between methods' in t for t in texts))

    def test_nested_functions_do_not_duplicate_source(self):
        source = (
            'def outer():\n'
            '    def inner():\n'
            '        return 5\n'
            '    return inner()\n'
            '\n'
            'async def fetch():\n'
            '    return 1\n'
        )
        parts = self.splitter.split(source)
        self.assertEqual([s.qualified_name for s in parts], ['outer', 'fetch'])
        self.assertEqual(parts[0].symbol_type, 'function')
        self.assertEqual(source[parts[0].start_index:parts[0].end_index].count('inner'), 2)

    def test_nested_classes_and_crlf_offsets(self):
        source = (
            'class Outer:\r\n'
            '    class Inner:\r\n'
            '        @property\r\n'
            '        def value(self):\r\n'
            '            return 2\r\n'
        )
        parts = self.splitter.split(source)
        member = next(s for s in parts if s.qualified_name == 'Outer.Inner.value')
        self.assertEqual(member.parent_class, 'Outer.Inner')
        self.assertEqual(member.symbol_type, 'method')
        self.assertTrue(source[member.start_index:member.end_index].startswith('        @property'))
        self.assertIn('\r\n', source[member.start_index:member.end_index])

    def test_no_nonwhitespace_is_lost(self):
        source = (
            '"""module doc"""\n'
            'x = 1\n'
            'def first():\n'
            '    pass\n'
            '# after\n'
            'class S:\n'
            '    y = 3\n'
            '    def a(self):\n'
            '        return 2\n'
            '    z = 4\n'
            '\n'
            'print(x)\n'
        )
        parts = self.splitter.split(source)
        covered = [' '] * len(source)
        for part in parts:
            self.assertEqual(''.join(covered[part.start_index:part.end_index]).strip(), '')
            covered[part.start_index:part.end_index] = source[part.start_index:part.end_index]
        for original, stored in zip(source, covered):
            if not original.isspace():
                self.assertEqual(original, stored)

    def test_invalid_python_raises_for_caller_fallback(self):
        with self.assertRaises(SyntaxError):
            self.splitter.split('def broken(:\n')

    def test_leading_comments_and_sibling_boundaries(self):
        source = ('x = 1\n# generate token\n\n@decorate\n'
                  'async def create_token():\n    return "abc"\n'
                  '\n# verify token\ndef verify():\n    return True\n')
        parts = self.splitter.split(source)
        self.assertEqual([s.symbol_name for s in parts], ['', 'create_token', 'verify'])
        self.assertTrue(source[parts[1].start_index:].startswith('# generate token'))
        self.assertTrue(source[parts[2].start_index:].startswith('# verify token'))
        previous_end = 0
        for part in parts:
            self.assertGreaterEqual(part.start_index, previous_end)
            self.assertFalse(source[previous_end:part.start_index].strip())
            previous_end = part.end_index
        self.assertFalse(source[previous_end:].strip())

    def test_header_directives_and_strings_not_attached(self):
        for header in ('# Copyright example\n# license MIT\n', '# noqa\n',
                       '#!/usr/bin/env python\n# coding: utf-8\n'):
            source = header + 'def run():\n    pass\n'
            parts = self.splitter.split(source)
            self.assertEqual(len(parts), 1)
            self.assertEqual(parts[0].start_index, len(header))
        source = 'text = """\n# not a comment\n"""\ndef run():\n    pass\n'
        parts = self.splitter.split(source)
        self.assertEqual(parts[1].start_index, source.index('def run'))

    def test_method_comments_do_not_steal_previous_body(self):
        source = ('class S:\n    def first(self):\n        pass\n'
                  '        # belongs to first\n'
                  '    # describes second\n    @staticmethod\n'
                  '    def second():\n        pass\n')
        parts = self.splitter.split(source)
        second = next(p for p in parts if p.qualified_name == 'S.second')
        self.assertTrue(source[second.start_index:].startswith('    # describes second'))

    def test_comment_filter_preserves_docstrings_and_incomplete_code(self):
        self.assertFalse(has_python_code('# comment\n\n# another\n'))
        self.assertEqual(self.splitter.split('# comment only\n'), [])
        self.assertTrue(has_python_code('"""module documentation"""'))
        self.assertTrue(has_python_code('# comment\ndef f():\n    pass\n'))
        self.assertTrue(has_python_code('value = ('))


if __name__ == '__main__':
    unittest.main()
