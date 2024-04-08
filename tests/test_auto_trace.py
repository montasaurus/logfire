import ast
import asyncio
import sys
from importlib.machinery import SourceFileLoader

import pytest
from inline_snapshot import snapshot

from logfire import DEFAULT_LOGFIRE_INSTANCE, AutoTraceModule, install_auto_tracing
from logfire._auto_trace import (
    AutoTraceModuleAlreadyImportedException,
    AutoTraceModuleAlreadyImportedWarning,
    LogfireFinder,
)
from logfire._auto_trace.import_hook import LogfireLoader
from logfire._auto_trace.rewrite_ast import rewrite_ast
from logfire.testing import TestExporter


def test_auto_trace_sample(exporter: TestExporter) -> None:
    meta_path = sys.meta_path.copy()

    install_auto_tracing('tests.auto_trace_samples')
    # Check that having multiple LogfireFinders doesn't break things
    install_auto_tracing('tests.blablabla')

    assert sys.meta_path[2:] == meta_path
    finder = sys.meta_path[1]
    assert isinstance(finder, LogfireFinder)

    assert finder.modules_filter(AutoTraceModule('tests.auto_trace_samples', '<filename>'))
    assert finder.modules_filter(AutoTraceModule('tests.auto_trace_samples.foo', '<filename>'))
    assert finder.modules_filter(AutoTraceModule('tests.auto_trace_samples.bar.baz', '<filename>'))
    assert not finder.modules_filter(AutoTraceModule('tests', '<filename>'))
    assert not finder.modules_filter(AutoTraceModule('tests_auto_trace_samples', '<filename>'))
    assert not finder.modules_filter(AutoTraceModule('tests.auto_trace_samples_foo', '<filename>'))

    from tests.auto_trace_samples import foo

    loader = foo.__loader__
    assert isinstance(loader, LogfireLoader)
    # The exact plain loader here isn't that essential.
    assert isinstance(loader.plain_spec.loader, SourceFileLoader)
    assert loader.plain_spec.name == foo.__name__ == foo.__spec__.name == 'tests.auto_trace_samples.foo'

    with pytest.raises(IndexError):  # foo.bar intentionally raises an error to test that it's recorded below
        asyncio.run(foo.bar())

    # insert_assert(exporter.exported_spans_as_dict(_include_pending_spans=True))
    assert exporter.exported_spans_as_dict(_include_pending_spans=True) == [
        {
            'name': 'Calling tests.auto_trace_samples.foo.bar (pending)',
            'context': {'trace_id': 1, 'span_id': 2, 'is_remote': False},
            'parent': {'trace_id': 1, 'span_id': 1, 'is_remote': False},
            'start_time': 1000000000,
            'end_time': 1000000000,
            'attributes': {
                'code.filepath': 'foo.py',
                'code.lineno': 123,
                'code.function': 'bar',
                'logfire.msg_template': 'Calling tests.auto_trace_samples.foo.bar',
                'logfire.msg': 'Calling tests.auto_trace_samples.foo.bar',
                'logfire.span_type': 'pending_span',
                'logfire.tags': ('auto-trace',),
                'logfire.pending_parent_id': '0000000000000000',
            },
        },
        {
            'name': 'Calling async_gen via @instrument (pending)',
            'context': {'trace_id': 1, 'span_id': 4, 'is_remote': False},
            'parent': {'trace_id': 1, 'span_id': 3, 'is_remote': False},
            'start_time': 2000000000,
            'end_time': 2000000000,
            'attributes': {
                'code.filepath': 'foo.py',
                'code.lineno': 123,
                'code.function': 'async_gen',
                'logfire.msg_template': 'Calling async_gen via @instrument',
                'logfire.span_type': 'pending_span',
                'logfire.msg': 'Calling async_gen via @instrument',
                'logfire.pending_parent_id': '0000000000000001',
            },
        },
        {
            'name': 'Calling tests.auto_trace_samples.foo.gen (pending)',
            'context': {'trace_id': 1, 'span_id': 6, 'is_remote': False},
            'parent': {'trace_id': 1, 'span_id': 5, 'is_remote': False},
            'start_time': 3000000000,
            'end_time': 3000000000,
            'attributes': {
                'code.filepath': 'foo.py',
                'code.lineno': 123,
                'code.function': 'gen',
                'logfire.msg_template': 'Calling tests.auto_trace_samples.foo.gen',
                'logfire.msg': 'Calling tests.auto_trace_samples.foo.gen',
                'logfire.span_type': 'pending_span',
                'logfire.tags': ('auto-trace',),
                'logfire.pending_parent_id': '0000000000000003',
            },
        },
        {
            'name': 'Calling tests.auto_trace_samples.foo.gen',
            'context': {'trace_id': 1, 'span_id': 5, 'is_remote': False},
            'parent': {'trace_id': 1, 'span_id': 3, 'is_remote': False},
            'start_time': 3000000000,
            'end_time': 4000000000,
            'attributes': {
                'code.filepath': 'foo.py',
                'code.lineno': 123,
                'code.function': 'gen',
                'logfire.msg_template': 'Calling tests.auto_trace_samples.foo.gen',
                'logfire.span_type': 'span',
                'logfire.tags': ('auto-trace',),
                'logfire.msg': 'Calling tests.auto_trace_samples.foo.gen',
            },
        },
        {
            'name': 'Calling async_gen via @instrument',
            'context': {'trace_id': 1, 'span_id': 3, 'is_remote': False},
            'parent': {'trace_id': 1, 'span_id': 1, 'is_remote': False},
            'start_time': 2000000000,
            'end_time': 5000000000,
            'attributes': {
                'code.filepath': 'foo.py',
                'code.lineno': 123,
                'code.function': 'async_gen',
                'logfire.msg_template': 'Calling async_gen via @instrument',
                'logfire.span_type': 'span',
                'logfire.msg': 'Calling async_gen via @instrument',
            },
        },
        {
            'name': 'Calling tests.auto_trace_samples.foo.bar',
            'context': {'trace_id': 1, 'span_id': 1, 'is_remote': False},
            'parent': None,
            'start_time': 1000000000,
            'end_time': 7000000000,
            'attributes': {
                'code.filepath': 'foo.py',
                'code.lineno': 123,
                'code.function': 'bar',
                'logfire.msg_template': 'Calling tests.auto_trace_samples.foo.bar',
                'logfire.span_type': 'span',
                'logfire.tags': ('auto-trace',),
                'logfire.msg': 'Calling tests.auto_trace_samples.foo.bar',
                'logfire.level_num': 17,
            },
            'events': [
                {
                    'name': 'exception',
                    'timestamp': 6000000000,
                    'attributes': {
                        'exception.type': 'IndexError',
                        'exception.message': 'list index out of range',
                        'exception.stacktrace': 'IndexError: list index out of range',
                        'exception.escaped': 'True',
                    },
                }
            ],
        },
    ]


def test_default_modules() -> None:
    # Check that nothing gets imported during this test,
    # because we don't want anything to get auto-traced here.
    imported_modules = set(sys.modules.items())

    # Test the default module filter argument of install_auto_tracing.
    # Since we're in `tests.test_auto_trace`, it should match anything in the `tests` package.
    # Since this has obviously already been imported, it requires `check_imported_modules='ignore'`.
    with pytest.raises(AutoTraceModuleAlreadyImportedException, match=r"The module 'tests' matches modules to trace"):
        install_auto_tracing()

    with pytest.raises(AutoTraceModuleAlreadyImportedWarning, match=r"The module 'tests' matches modules to trace"):
        install_auto_tracing(check_imported_modules='warn')

    with pytest.raises(ValueError):
        install_auto_tracing(check_imported_modules='other')

    # This should match anything in the `tests` package, so remove the finder at the end of the test.
    meta_path = sys.meta_path.copy()
    install_auto_tracing(check_imported_modules='ignore')
    assert sys.meta_path[1:] == meta_path
    finder = sys.meta_path[0]

    try:
        assert isinstance(finder, LogfireFinder)
        assert finder.modules_filter(AutoTraceModule('tests', '<filename>'))
        assert finder.modules_filter(AutoTraceModule('tests.foo', '<filename>'))
        assert finder.modules_filter(AutoTraceModule('tests.bar.baz', '<filename>'))
        assert not finder.modules_filter(AutoTraceModule('test', '<filename>'))
        assert not finder.modules_filter(AutoTraceModule('tests_foo', '<filename>'))
        assert not finder.modules_filter(AutoTraceModule('foo.bar.baz', '<filename>'))
    finally:
        assert sys.meta_path.pop(0) == finder
        assert sys.meta_path == meta_path
        assert set(sys.modules.items()) == imported_modules


# language=Python
nested_sample = """
def func():
    x = 1

    class Class:
        x = 2

        def method(self):
            y = 3
            return y

        async def method2(self):
            class Class2:
                z = 4

                async def method3(self):
                    a = 5
                    return a
            return Class2().method3()

    return (x, Class)

class Class3:
    x = 6

    def method4(self):
        b = 7
        return b
"""


def test_rewrite_ast():
    context_factories = []
    tree = rewrite_ast(
        nested_sample,
        'foo.py',
        'logfire_span',
        'module.name',
        DEFAULT_LOGFIRE_INSTANCE,
        context_factories,
        min_duration=0,
    )
    result = """
def func():
    with logfire_span[3]():
        x = 1

        class Class:
            x = 2

            def method(self):
                with logfire_span[0]():
                    y = 3
                    return y

            async def method2(self):
                with logfire_span[2]():

                    class Class2:
                        z = 4

                        async def method3(self):
                            with logfire_span[1]():
                                a = 5
                                return a
                    return Class2().method3()
        return (x, Class)

class Class3:
    x = 6

    def method4(self):
        with logfire_span[4]():
            b = 7
            return b
"""

    if sys.version_info >= (3, 9):  # pragma: no branch
        assert ast.unparse(tree).strip() == result.strip()

    # Python 3.8 doesn't have ast.unparse, and testing that the AST is equivalent is a bit tricky.
    assert (
        compile(tree, '<filename>', 'exec').co_code == compile(result, '<filename>', 'exec').co_code
        or ast.dump(tree, annotate_fields=False) == ast.dump(ast.parse(result), annotate_fields=False)
        or ast.dump(tree) == ast.dump(ast.parse(result))
    )

    assert [f.args for f in context_factories] == snapshot(
        [
            (
                'Calling module.name.func.<locals>.Class.method',
                {
                    'code.filepath': 'foo.py',
                    'code.lineno': 8,
                    'code.function': 'func.<locals>.Class.method',
                    'logfire.msg_template': 'Calling module.name.func.<locals>.Class.method',
                    'logfire.tags': ('auto-trace',),
                },
            ),
            (
                'Calling module.name.func.<locals>.Class.method2.<locals>.Class2.method3',
                {
                    'code.filepath': 'foo.py',
                    'code.lineno': 16,
                    'code.function': 'func.<locals>.Class.method2.<locals>.Class2.method3',
                    'logfire.msg_template': 'Calling module.name.func.<locals>.Class.method2.<locals>.Class2.method3',
                    'logfire.tags': ('auto-trace',),
                },
            ),
            (
                'Calling module.name.func.<locals>.Class.method2',
                {
                    'code.filepath': 'foo.py',
                    'code.lineno': 12,
                    'code.function': 'func.<locals>.Class.method2',
                    'logfire.msg_template': 'Calling module.name.func.<locals>.Class.method2',
                    'logfire.tags': ('auto-trace',),
                },
            ),
            (
                'Calling module.name.func',
                {
                    'code.filepath': 'foo.py',
                    'code.lineno': 2,
                    'code.function': 'func',
                    'logfire.msg_template': 'Calling module.name.func',
                    'logfire.tags': ('auto-trace',),
                },
            ),
            (
                'Calling module.name.Class3.method4',
                {
                    'code.filepath': 'foo.py',
                    'code.lineno': 26,
                    'code.function': 'Class3.method4',
                    'logfire.msg_template': 'Calling module.name.Class3.method4',
                    'logfire.tags': ('auto-trace',),
                },
            ),
        ]
    )


def test_parts_start_with():
    for mod in [
        'foo',
        'foo.spam',
        'bar',
        'bar.spam',
        'xxx',
        'xxx.spam',
    ]:
        assert AutoTraceModule(mod, None).parts_start_with(['foo', 'bar', 'x+'])

    for mod in [
        'spam',
        'spam.foo',
        'spam.bar',
        'spam.bar.foo',
        'spam.foo.bar',
        'spam.xxx',
    ]:
        assert not AutoTraceModule(mod, None).parts_start_with(['foo', 'bar', 'x+'])


# language=Python
no_auto_trace_sample = """
from logfire import no_auto_trace


@str
def traced_func():
    async def inner():
        pass
    return inner


@str
@no_auto_trace
@str
def not_traced_func():
    async def inner():
        pass
    return inner


@str
class TracedClass:
    async def traced_method(self):
        pass

    @no_auto_trace
    def not_traced_method(self):
        pass


@no_auto_trace
@str
class NotTracedClass:
    async def would_be_traced_method(self):
        def inner():
            pass
        return inner

    @no_auto_trace
    def definitely_not_traced_method(self):
        pass
"""


def get_calling_strings(sample: str):
    context_factories = []
    rewrite_ast(
        sample,
        'foo.py',
        'logfire_span',
        'module.name',
        DEFAULT_LOGFIRE_INSTANCE,
        context_factories,
        min_duration=0,
    )
    return {f.args[0] for f in context_factories}


def test_no_auto_trace():
    filtered_calling_strings = {
        'Calling module.name.traced_func',
        'Calling module.name.traced_func.<locals>.inner',
        'Calling module.name.TracedClass.traced_method',
    }

    # insert_assert(get_calling_strings(no_auto_trace_sample.replace('@no_auto_trace', '')))
    all_calling_strings = {
        'Calling module.name.not_traced_func',
        'Calling module.name.TracedClass.traced_method',
        'Calling module.name.NotTracedClass.would_be_traced_method',
        'Calling module.name.not_traced_func.<locals>.inner',
        'Calling module.name.traced_func',
        'Calling module.name.NotTracedClass.would_be_traced_method.<locals>.inner',
        'Calling module.name.traced_func.<locals>.inner',
        'Calling module.name.TracedClass.not_traced_method',
        'Calling module.name.NotTracedClass.definitely_not_traced_method',
    }
    assert filtered_calling_strings < all_calling_strings

    # @no_auto_trace and @logfire.no_auto_trace have the same effect
    assert get_calling_strings(no_auto_trace_sample) == filtered_calling_strings
    assert (
        get_calling_strings(no_auto_trace_sample.replace('@no_auto_trace', '@logfire.no_auto_trace'))
        == filtered_calling_strings
    )

    # But @other or @other.no_auto_trace have no effect
    assert get_calling_strings(no_auto_trace_sample.replace('no_auto_trace', 'other')) == all_calling_strings
    assert (
        get_calling_strings(no_auto_trace_sample.replace('@no_auto_trace', '@other.no_auto_trace'))
        == all_calling_strings
    )


def test_min_duration(exporter: TestExporter):
    install_auto_tracing('tests.auto_trace_samples.simple_nesting', min_duration=5)

    from tests.auto_trace_samples import simple_nesting

    assert simple_nesting.func1() == 42

    # The first time the functions are called, we only measure their durations,
    # so no spans are created.
    assert exporter.exported_spans == []

    assert simple_nesting.func1() == 42

    # In the first run, func1 and func2 are found to take at least 5 seconds,
    # (because the timestamp generator is advanced when measuring the duration)
    # so now they have spans, but func3 and func4 are still too short.
    assert exporter.exported_spans_as_dict() == snapshot(
        [
            {
                'name': 'Calling tests.auto_trace_samples.simple_nesting.func2',
                'context': {'trace_id': 1, 'span_id': 3, 'is_remote': False},
                'parent': {'trace_id': 1, 'span_id': 1, 'is_remote': False},
                'start_time': 10000000000,
                'end_time': 15000000000,
                'attributes': {
                    'code.filepath': 'simple_nesting.py',
                    'code.lineno': 123,
                    'code.function': 'func2',
                    'logfire.msg_template': 'Calling tests.auto_trace_samples.simple_nesting.func2',
                    'logfire.tags': ('auto-trace',),
                    'logfire.span_type': 'span',
                    'logfire.msg': 'Calling tests.auto_trace_samples.simple_nesting.func2',
                },
            },
            {
                'name': 'Calling tests.auto_trace_samples.simple_nesting.func1',
                'context': {'trace_id': 1, 'span_id': 1, 'is_remote': False},
                'parent': None,
                'start_time': 9000000000,
                'end_time': 16000000000,
                'attributes': {
                    'code.filepath': 'simple_nesting.py',
                    'code.lineno': 123,
                    'code.function': 'func1',
                    'logfire.msg_template': 'Calling tests.auto_trace_samples.simple_nesting.func1',
                    'logfire.tags': ('auto-trace',),
                    'logfire.span_type': 'span',
                    'logfire.msg': 'Calling tests.auto_trace_samples.simple_nesting.func1',
                },
            },
        ]
    )
