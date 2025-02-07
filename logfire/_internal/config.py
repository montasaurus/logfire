from __future__ import annotations as _annotations

import atexit
import dataclasses
import functools
import json
import os
import re
import sys
import time
import warnings
from contextlib import suppress
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from threading import RLock, Thread
from typing import TYPE_CHECKING, Any, Callable, Literal, Sequence, cast
from urllib.parse import urljoin
from uuid import uuid4
from weakref import WeakSet

import requests
from opentelemetry import metrics, trace
from opentelemetry.environment_variables import OTEL_METRICS_EXPORTER, OTEL_TRACES_EXPORTER
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.environment_variables import (
    OTEL_BSP_SCHEDULE_DELAY,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_EXPORTER_OTLP_METRICS_ENDPOINT,
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
    OTEL_RESOURCE_ATTRIBUTES,
)
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    MeterProvider,
    ObservableCounter,
    ObservableGauge,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import AggregationTemporality, MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExponentialBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio
from opentelemetry.semconv.resource import ResourceAttributes
from rich.console import Console
from rich.prompt import Confirm, Prompt
from typing_extensions import Self

from logfire.exceptions import LogfireConfigError
from logfire.version import VERSION

from .auth import DEFAULT_FILE, DefaultFile, is_logged_in
from .config_params import ParamManager, PydanticPluginRecordValues
from .constants import (
    DEFAULT_FALLBACK_FILE_NAME,
    OTLP_MAX_BODY_SIZE,
    LevelName,
)
from .exporters.console import (
    ConsoleColorsValues,
    IndentedConsoleSpanExporter,
    ShowParentsConsoleSpanExporter,
    SimpleConsoleSpanExporter,
)
from .exporters.fallback import FallbackSpanExporter
from .exporters.file import FileSpanExporter
from .exporters.otlp import OTLPExporterHttpSession, RetryFewerSpansSpanExporter
from .exporters.processor_wrapper import MainSpanProcessorWrapper
from .exporters.quiet_metrics import QuietMetricExporter
from .exporters.remove_pending import RemovePendingSpansExporter
from .exporters.tail_sampling import TailSamplingOptions, TailSamplingProcessor
from .exporters.test import TestExporter
from .integrations.executors import instrument_executors
from .metrics import ProxyMeterProvider
from .scrubbing import NOOP_SCRUBBER, BaseScrubber, Scrubber, ScrubbingOptions, ScrubCallback
from .stack_info import warn_at_user_stacklevel
from .tracer import PendingSpanProcessor, ProxyTracerProvider
from .utils import UnexpectedResponse, ensure_data_dir_exists, get_version, read_toml_file, suppress_instrumentation

if TYPE_CHECKING:
    from .main import FastLogfireSpan, LogfireSpan

# NOTE: this WeakSet is the reason that FastLogfireSpan.__slots__ has a __weakref__ slot.
OPEN_SPANS: WeakSet[LogfireSpan | FastLogfireSpan] = WeakSet()

CREDENTIALS_FILENAME = 'logfire_credentials.json'
"""Default base URL for the Logfire API."""
COMMON_REQUEST_HEADERS = {'User-Agent': f'logfire/{VERSION}'}
"""Common request headers for requests to the Logfire API."""
PROJECT_NAME_PATTERN = r'^[a-z0-9]+(?:-[a-z0-9]+)*$'

METRICS_PREFERRED_TEMPORALITY = {
    Counter: AggregationTemporality.DELTA,
    UpDownCounter: AggregationTemporality.CUMULATIVE,
    Histogram: AggregationTemporality.DELTA,
    ObservableCounter: AggregationTemporality.DELTA,
    ObservableUpDownCounter: AggregationTemporality.CUMULATIVE,
    ObservableGauge: AggregationTemporality.CUMULATIVE,
}
"""This should be passed as the `preferred_temporality` argument of metric readers and exporters."""


@dataclass
class ConsoleOptions:
    """Options for controlling console output."""

    colors: ConsoleColorsValues = 'auto'
    span_style: Literal['simple', 'indented', 'show-parents'] = 'show-parents'
    """How spans are shown in the console."""
    include_timestamps: bool = True
    """Whether to include timestamps in the console output."""
    verbose: bool = False
    """Whether to show verbose output.

    It includes the filename, log level, and line number.
    """
    min_log_level: LevelName = 'info'
    """The minimum log level to show in the console."""


@dataclass
class PydanticPlugin:
    """Options for the Pydantic plugin."""

    record: PydanticPluginRecordValues = 'off'
    """The record mode for the Pydantic plugin.

    It can be one of the following values:

    * `off`: Disable instrumentation. This is default value.
    * `all`: Send traces and metrics for all events.
    * `failure`: Send metrics for all validations and traces only for validation failures.
    * `metrics`: Send only metrics.
    """
    include: set[str] = field(default_factory=set)
    """By default, third party modules are not instrumented. This option allows you to include specific modules."""
    exclude: set[str] = field(default_factory=set)
    """Exclude specific modules from instrumentation."""


def configure(
    *,
    send_to_logfire: bool | Literal['if-token-present'] | None = None,
    token: str | None = None,
    project_name: str | None = None,
    service_name: str | None = None,
    service_version: str | None = None,
    trace_sample_rate: float | None = None,
    console: ConsoleOptions | Literal[False] | None = None,
    show_summary: bool | None = None,
    config_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
    base_url: str | None = None,
    collect_system_metrics: None = None,
    id_generator: IdGenerator | None = None,
    ns_timestamp_generator: Callable[[], int] | None = None,
    processors: None = None,
    additional_span_processors: Sequence[SpanProcessor] | None = None,
    metric_readers: None = None,
    additional_metric_readers: Sequence[MetricReader] | None = None,
    pydantic_plugin: PydanticPlugin | None = None,
    fast_shutdown: bool = False,
    scrubbing_patterns: Sequence[str] | None = None,
    scrubbing_callback: ScrubCallback | None = None,
    scrubbing: ScrubbingOptions | Literal[False] | None = None,
    inspect_arguments: bool | None = None,
    tail_sampling: TailSamplingOptions | None = None,
) -> None:
    """Configure the logfire SDK.

    Args:
        send_to_logfire: Whether to send logs to logfire.dev. Defaults to the `LOGFIRE_SEND_TO_LOGFIRE` environment
            variable if set, otherwise defaults to `True`. If `if-token-present` is provided, logs will only be sent if
            a token is present.
        token: The project token. Defaults to the `LOGFIRE_TOKEN` environment variable.
        project_name: Name to request when creating a new project. Defaults to the `LOGFIRE_PROJECT_NAME` environment
            variable, or the current directory name.
            Project name accepts a string value containing alphanumeric characters and
            hyphens (-). The hyphen character must not be located at the beginning or end of the string and should
            appear in between alphanumeric characters.
        service_name: Name of this service. Defaults to the `LOGFIRE_SERVICE_NAME` environment variable.
        service_version: Version of this service. Defaults to the `LOGFIRE_SERVICE_VERSION` environment variable, or the
            current git commit hash if available.
        trace_sample_rate: Sampling ratio for spans. Defaults to the `LOGFIRE_SAMPLING_RATIO` environment variable, or
            the `OTEL_TRACES_SAMPLER_ARG` environment variable, or to `1.0`.
        console: Whether to control terminal output. If `None` uses the `LOGFIRE_CONSOLE_*` environment variables,
            otherwise defaults to `ConsoleOption(colors='auto', indent_spans=True, include_timestamps=True, verbose=False)`.
            If `False` disables console output. It can also be disabled by setting `LOGFIRE_CONSOLE` environment variable to `false`.
        show_summary: When to print a summary of the Logfire setup including a link to the dashboard. If `None` uses the `LOGFIRE_SHOW_SUMMARY` environment variable, otherwise
            defaults to `True`.
        config_dir: Directory that contains the `pyproject.toml` file for this project. If `None` uses the
            `LOGFIRE_CONFIG_DIR` environment variable, otherwise defaults to the current working directory.
        data_dir: Directory to store credentials, and logs. If `None` uses the `LOGFIRE_CREDENTIALS_DIR` environment variable, otherwise defaults to `'.logfire'`.
        base_url: Root URL for the Logfire API. If `None` uses the `LOGFIRE_BASE_URL` environment variable, otherwise defaults to https://logfire-api.pydantic.dev.
        collect_system_metrics: Legacy argument, use [`logfire.instrument_system_metrics()`](https://docs.pydantic.dev/logfire/integrations/system_metrics/) instead.
        id_generator: Generator for span IDs. Defaults to `RandomIdGenerator()` from the OpenTelemetry SDK.
        ns_timestamp_generator: Generator for nanosecond timestamps. Defaults to [`time.time_ns`][time.time_ns] from the
            Python standard library.
        processors: Legacy argument, use `additional_span_processors` instead.
        additional_span_processors: Span processors to use in addition to the default processor which exports spans to Logfire's API.
        metric_readers: Legacy argument, use `additional_metric_readers` instead.
        additional_metric_readers: Sequence of metric readers to be used in addition to the default reader
            which exports metrics to Logfire's API.
        pydantic_plugin: Configuration for the Pydantic plugin. If `None` uses the `LOGFIRE_PYDANTIC_PLUGIN_*` environment
            variables, otherwise defaults to `PydanticPlugin(record='off')`.
        fast_shutdown: Whether to shut down exporters and providers quickly, mostly used for tests. Defaults to `False`.
        scrubbing: Options for scrubbing sensitive data. Set to `False` to disable.
        scrubbing_patterns: Deprecated, use `scrubbing=logfire.ScrubbingOptions(extra_patterns=[...])` instead.
        scrubbing_callback: Deprecated, use `scrubbing=logfire.ScrubbingOptions(callback=...)` instead.
        inspect_arguments: Whether to enable
            [f-string magic](https://docs.pydantic.dev/logfire/guides/onboarding_checklist/add_manual_tracing/#f-strings).
            If `None` uses the `LOGFIRE_INSPECT_ARGUMENTS` environment variable.
            Defaults to `True` if and only if the Python version is at least 3.11.
        tail_sampling: Tail sampling options. Not ready for general use.
    """
    if processors is not None:  # pragma: no cover
        raise ValueError(
            'The `processors` argument has been replaced by `additional_span_processors`. '
            'Set `send_to_logfire=False` to disable the default processor.'
        )

    if metric_readers is not None:  # pragma: no cover
        raise ValueError(
            'The `metric_readers` argument has been replaced by `additional_metric_readers`. '
            'Set `send_to_logfire=False` to disable the default metric reader.'
        )

    if collect_system_metrics is False:
        raise ValueError(
            'The `collect_system_metrics` argument has been removed. '
            'System metrics are no longer collected by default.'
        )

    if collect_system_metrics is not None:
        raise ValueError(
            'The `collect_system_metrics` argument has been removed. '
            'Use `logfire.instrument_system_metrics()` instead.'
        )

    if scrubbing_callback or scrubbing_patterns:
        if scrubbing is not None:
            raise ValueError(
                'Cannot specify `scrubbing` and `scrubbing_callback` or `scrubbing_patterns` at the same time. '
                'Use only `scrubbing`.'
            )
        warnings.warn(
            'The `scrubbing_callback` and `scrubbing_patterns` arguments are deprecated. '
            'Use `scrubbing=logfire.ScrubbingOptions(callback=..., extra_patterns=[...])` instead.',
            DeprecationWarning,
        )
        scrubbing = ScrubbingOptions(callback=scrubbing_callback, extra_patterns=scrubbing_patterns)

    GLOBAL_CONFIG.configure(
        base_url=base_url,
        send_to_logfire=send_to_logfire,
        token=token,
        project_name=project_name,
        service_name=service_name,
        service_version=service_version,
        trace_sample_rate=trace_sample_rate,
        console=console,
        show_summary=show_summary,
        config_dir=Path(config_dir) if config_dir else None,
        data_dir=Path(data_dir) if data_dir else None,
        id_generator=id_generator,
        ns_timestamp_generator=ns_timestamp_generator,
        additional_span_processors=additional_span_processors,
        additional_metric_readers=additional_metric_readers,
        pydantic_plugin=pydantic_plugin,
        fast_shutdown=fast_shutdown,
        scrubbing=scrubbing,
        inspect_arguments=inspect_arguments,
        tail_sampling=tail_sampling,
    )


def _get_int_from_env(env_var: str) -> int | None:
    value = os.getenv(env_var)
    if not value:
        return None
    return int(value)  # pragma: no cover


@dataclasses.dataclass
class _LogfireConfigData:
    """Data-only parent class for LogfireConfig.

    This class can be pickled / copied and gives a nice repr,
    while allowing us to keep the ugly stuff only in LogfireConfig.

    In particular, using this dataclass as a base class of LogfireConfig allows us to use
    `dataclasses.asdict` in `integrations/executors.py` to get a dict with just the attributes from
    `_LogfireConfigData`, and none of the attributes added in `LogfireConfig`.
    """

    base_url: str
    """The base URL of the Logfire API"""

    send_to_logfire: bool | Literal['if-token-present']
    """Whether to send logs and spans to Logfire"""

    token: str | None
    """The Logfire API token to use"""

    project_name: str | None
    """The Logfire project name to use"""

    service_name: str
    """The name of this service"""

    service_version: str | None
    """The version of this service"""

    trace_sample_rate: float
    """The sampling ratio for spans"""

    console: ConsoleOptions | Literal[False] | None
    """Options for controlling console output"""

    show_summary: bool
    """Whether to show the summary when starting a new project"""

    data_dir: Path
    """The directory to store Logfire data in"""

    id_generator: IdGenerator
    """The ID generator to use"""

    ns_timestamp_generator: Callable[[], int]
    """The nanosecond timestamp generator to use"""

    additional_span_processors: Sequence[SpanProcessor] | None
    """Additional span processors"""

    pydantic_plugin: PydanticPlugin
    """Options for the Pydantic plugin"""

    fast_shutdown: bool
    """Whether to shut down exporters and providers quickly, mostly used for tests"""

    scrubbing: ScrubbingOptions | Literal[False]
    """Options for redacting sensitive data, or False to disable."""

    inspect_arguments: bool
    """Whether to enable f-string magic"""

    tail_sampling: TailSamplingOptions | None
    """Tail sampling options"""

    def _load_configuration(
        self,
        # note that there are no defaults here so that the only place
        # defaults exist is `__init__` and we don't forgot a parameter when
        # forwarding parameters from `__init__` to `load_configuration`
        base_url: str | None,
        send_to_logfire: bool | Literal['if-token-present'] | None,
        token: str | None,
        project_name: str | None,
        service_name: str | None,
        service_version: str | None,
        trace_sample_rate: float | None,
        console: ConsoleOptions | Literal[False] | None,
        show_summary: bool | None,
        config_dir: Path | None,
        data_dir: Path | None,
        id_generator: IdGenerator | None,
        ns_timestamp_generator: Callable[[], int] | None,
        additional_span_processors: Sequence[SpanProcessor] | None,
        additional_metric_readers: Sequence[MetricReader] | None,
        pydantic_plugin: PydanticPlugin | None,
        fast_shutdown: bool,
        scrubbing: ScrubbingOptions | Literal[False] | None,
        inspect_arguments: bool | None,
        tail_sampling: TailSamplingOptions | None,
    ) -> None:
        """Merge the given parameters with the environment variables file configurations."""
        param_manager = ParamManager.create(config_dir)

        self.base_url = param_manager.load_param('base_url', base_url)
        self.send_to_logfire = param_manager.load_param('send_to_logfire', send_to_logfire)
        self.token = param_manager.load_param('token', token)
        self.project_name = param_manager.load_param('project_name', project_name)
        self.service_name = param_manager.load_param('service_name', service_name)
        self.service_version = param_manager.load_param('service_version', service_version)
        self.trace_sample_rate = param_manager.load_param('trace_sample_rate', trace_sample_rate)
        self.show_summary = param_manager.load_param('show_summary', show_summary)
        self.data_dir = param_manager.load_param('data_dir', data_dir)
        self.inspect_arguments = param_manager.load_param('inspect_arguments', inspect_arguments)
        self.ignore_no_config = param_manager.load_param('ignore_no_config')
        if self.inspect_arguments and sys.version_info[:2] <= (3, 8):
            raise LogfireConfigError(
                'Inspecting arguments is only supported in Python 3.9+ and only recommended in Python 3.11+.'
            )

        # We save `scrubbing` just so that it can be serialized and deserialized.
        if isinstance(scrubbing, dict):
            # This is particularly for deserializing from a dict as in executors.py
            scrubbing = ScrubbingOptions(**scrubbing)  # type: ignore
        if scrubbing is None:
            scrubbing = ScrubbingOptions()
        self.scrubbing: ScrubbingOptions | Literal[False] = scrubbing
        self.scrubber: BaseScrubber = (
            Scrubber(scrubbing.extra_patterns, scrubbing.callback) if scrubbing else NOOP_SCRUBBER
        )

        if isinstance(console, dict):
            # This is particularly for deserializing from a dict as in executors.py
            console = ConsoleOptions(**console)  # type: ignore
        if console is not None:
            self.console = console
        elif param_manager.load_param('console') is False:
            self.console = False
        else:
            self.console = ConsoleOptions(
                colors=param_manager.load_param('console_colors'),
                span_style=param_manager.load_param('console_span_style'),
                include_timestamps=param_manager.load_param('console_include_timestamp'),
                verbose=param_manager.load_param('console_verbose'),
                min_log_level=param_manager.load_param('console_min_log_level'),
            )

        if isinstance(pydantic_plugin, dict):
            # This is particularly for deserializing from a dict as in executors.py
            pydantic_plugin = PydanticPlugin(**pydantic_plugin)  # type: ignore
        self.pydantic_plugin = pydantic_plugin or param_manager.pydantic_plugin
        if self.pydantic_plugin.record != 'off':
            import pydantic

            if get_version(pydantic.__version__) < get_version('2.5.0'):  # pragma: no cover
                raise RuntimeError('The Pydantic plugin requires Pydantic 2.5.0 or newer.')

        if isinstance(tail_sampling, dict):
            # This is particularly for deserializing from a dict as in executors.py
            tail_sampling = TailSamplingOptions(**tail_sampling)  # type: ignore
        self.tail_sampling = tail_sampling

        self.fast_shutdown = fast_shutdown

        self.id_generator = id_generator or RandomIdGenerator()
        self.ns_timestamp_generator = ns_timestamp_generator or time.time_ns
        self.additional_span_processors = additional_span_processors
        self.additional_metric_readers = additional_metric_readers
        if self.service_version is None:
            try:
                self.service_version = get_git_revision_hash()
            except Exception:
                # many things could go wrong here, e.g. git is not installed, etc.
                # ignore them
                pass


class LogfireConfig(_LogfireConfigData):
    def __init__(
        self,
        base_url: str | None = None,
        send_to_logfire: bool | None = None,
        token: str | None = None,
        project_name: str | None = None,
        service_name: str | None = None,
        service_version: str | None = None,
        trace_sample_rate: float | None = None,
        console: ConsoleOptions | Literal[False] | None = None,
        show_summary: bool | None = None,
        config_dir: Path | None = None,
        data_dir: Path | None = None,
        id_generator: IdGenerator | None = None,
        ns_timestamp_generator: Callable[[], int] | None = None,
        additional_span_processors: Sequence[SpanProcessor] | None = None,
        additional_metric_readers: Sequence[MetricReader] | None = None,
        pydantic_plugin: PydanticPlugin | None = None,
        fast_shutdown: bool = False,
        scrubbing: ScrubbingOptions | Literal[False] | None = None,
        inspect_arguments: bool | None = None,
        tail_sampling: TailSamplingOptions | None = None,
    ) -> None:
        """Create a new LogfireConfig.

        Users should never need to call this directly, instead use `logfire.configure`.

        See `_LogfireConfigData` for parameter documentation.
        """
        # The `load_configuration` is it's own method so that it can be called on an existing config object
        # in particular the global config object.
        self._load_configuration(
            base_url=base_url,
            send_to_logfire=send_to_logfire,
            token=token,
            project_name=project_name,
            service_name=service_name,
            service_version=service_version,
            trace_sample_rate=trace_sample_rate,
            console=console,
            show_summary=show_summary,
            config_dir=config_dir,
            data_dir=data_dir,
            id_generator=id_generator,
            ns_timestamp_generator=ns_timestamp_generator,
            additional_span_processors=additional_span_processors,
            additional_metric_readers=additional_metric_readers,
            pydantic_plugin=pydantic_plugin,
            fast_shutdown=fast_shutdown,
            scrubbing=scrubbing,
            inspect_arguments=inspect_arguments,
            tail_sampling=tail_sampling,
        )
        # initialize with no-ops so that we don't impact OTEL's global config just because logfire is installed
        # that is, we defer setting logfire as the otel global config until `configure` is called
        self._tracer_provider = ProxyTracerProvider(trace.NoOpTracerProvider(), self)
        # note: this reference is important because the MeterProvider runs things in background threads
        # thus it "shuts down" when it's gc'ed
        self._meter_provider = ProxyMeterProvider(metrics.NoOpMeterProvider())
        # This ensures that we only call OTEL's global set_tracer_provider once to avoid warnings.
        self._has_set_providers = False
        self._initialized = False
        self._lock = RLock()

    def configure(
        self,
        base_url: str | None,
        send_to_logfire: bool | Literal['if-token-present'] | None,
        token: str | None,
        project_name: str | None,
        service_name: str | None,
        service_version: str | None,
        trace_sample_rate: float | None,
        console: ConsoleOptions | Literal[False] | None,
        show_summary: bool | None,
        config_dir: Path | None,
        data_dir: Path | None,
        id_generator: IdGenerator | None,
        ns_timestamp_generator: Callable[[], int] | None,
        additional_span_processors: Sequence[SpanProcessor] | None,
        additional_metric_readers: Sequence[MetricReader] | None,
        pydantic_plugin: PydanticPlugin | None,
        fast_shutdown: bool,
        scrubbing: ScrubbingOptions | Literal[False] | None,
        inspect_arguments: bool | None,
        tail_sampling: TailSamplingOptions | None,
    ) -> None:
        with self._lock:
            self._initialized = False
            self._load_configuration(
                base_url,
                send_to_logfire,
                token,
                project_name,
                service_name,
                service_version,
                trace_sample_rate,
                console,
                show_summary,
                config_dir,
                data_dir,
                id_generator,
                ns_timestamp_generator,
                additional_span_processors,
                additional_metric_readers,
                pydantic_plugin,
                fast_shutdown,
                scrubbing,
                inspect_arguments,
                tail_sampling,
            )
            self.initialize()

    def initialize(self) -> ProxyTracerProvider:
        """Configure internals to start exporting traces and metrics."""
        with self._lock:
            return self._initialize()

    def _initialize(self) -> ProxyTracerProvider:
        if self._initialized:  # pragma: no cover
            return self._tracer_provider

        with suppress_instrumentation():
            otel_resource_attributes: dict[str, Any] = {
                ResourceAttributes.SERVICE_NAME: self.service_name,
                ResourceAttributes.PROCESS_PID: os.getpid(),
                # Having this giant blob of data associated with every span/metric causes various problems so it's
                # disabled for now, but we may want to re-enable something like it in the future
                # RESOURCE_ATTRIBUTES_PACKAGE_VERSIONS: json.dumps(collect_package_info(), separators=(',', ':')),
            }
            if self.service_version:
                otel_resource_attributes[ResourceAttributes.SERVICE_VERSION] = self.service_version
            otel_resource_attributes_from_env = os.getenv(OTEL_RESOURCE_ATTRIBUTES)
            if otel_resource_attributes_from_env:
                for _field in otel_resource_attributes_from_env.split(','):
                    key, value = _field.split('=', maxsplit=1)
                    otel_resource_attributes[key.strip()] = value.strip()

            resource = Resource.create(otel_resource_attributes)

            # Set service instance ID to a random UUID if it hasn't been set already.
            # Setting it above would have also mostly worked and allowed overriding via OTEL_RESOURCE_ATTRIBUTES,
            # but doing it here means that resource detectors (checked in Resource.create) get priority.
            # This attribute is currently experimental. The latest released docs about it are here:
            # https://opentelemetry.io/docs/specs/semconv/resource/#service-experimental
            # Currently there's a newer version with some differences here:
            # https://github.com/open-telemetry/semantic-conventions/blob/e44693245eef815071402b88c3a44a8f7f8f24c8/docs/resource/README.md#service-experimental
            # Both recommend generating a UUID.
            resource = Resource({ResourceAttributes.SERVICE_INSTANCE_ID: uuid4().hex}).merge(resource)

            # Avoid using the usual sampler if we're using tail-based sampling.
            # The TailSamplingProcessor will handle the random sampling part as well.
            sampler = (
                ParentBasedTraceIdRatio(self.trace_sample_rate)
                if self.trace_sample_rate < 1 and self.tail_sampling is None
                else None
            )
            tracer_provider = SDKTracerProvider(
                sampler=sampler,
                resource=resource,
                id_generator=self.id_generator,
            )

            self._tracer_provider.shutdown()
            self._tracer_provider.set_provider(tracer_provider)  # do we need to shut down the existing one???

            processors_with_pending_spans: list[SpanProcessor] = []

            def add_span_processor(span_processor: SpanProcessor) -> None:
                # Some span processors added to the tracer provider should also be recorded in
                # `processors_with_pending_spans` so that they can be used by the final pending span processor.
                # This means that `tracer_provider.add_span_processor` should only appear in two places.
                has_pending = isinstance(
                    getattr(span_processor, 'span_exporter', None),
                    (TestExporter, RemovePendingSpansExporter, SimpleConsoleSpanExporter),
                )

                if self.tail_sampling:
                    span_processor = TailSamplingProcessor(
                        span_processor,
                        self.tail_sampling,
                        # If self.trace_sample_rate < 1 then that ratio of spans should be included randomly by this.
                        # In that case the tracer provider doesn't need to do any sampling, see above.
                        # Otherwise we're not using any random sampling, so 0% of spans should be included 'randomly'.
                        self.trace_sample_rate if self.trace_sample_rate < 1 else 0,
                    )
                span_processor = MainSpanProcessorWrapper(span_processor, self.scrubber)
                tracer_provider.add_span_processor(span_processor)
                if has_pending:
                    processors_with_pending_spans.append(span_processor)

            if self.additional_span_processors is not None:
                for processor in self.additional_span_processors:
                    add_span_processor(processor)

            if self.console:
                if self.console.span_style == 'simple':  # pragma: no cover
                    exporter_cls = SimpleConsoleSpanExporter
                elif self.console.span_style == 'indented':  # pragma: no cover
                    exporter_cls = IndentedConsoleSpanExporter
                else:
                    assert self.console.span_style == 'show-parents'
                    exporter_cls = ShowParentsConsoleSpanExporter
                add_span_processor(
                    SimpleSpanProcessor(
                        exporter_cls(
                            colors=self.console.colors,
                            include_timestamp=self.console.include_timestamps,
                            verbose=self.console.verbose,
                            min_log_level=self.console.min_log_level,
                        ),
                    )
                )

            metric_readers = list(self.additional_metric_readers or [])

            if (self.send_to_logfire == 'if-token-present' and self.token is not None) or self.send_to_logfire is True:
                if self.token is None:
                    if (credentials := LogfireCredentials.load_creds_file(self.data_dir)) is None:  # pragma: no branch
                        credentials = LogfireCredentials.initialize_project(
                            logfire_api_url=self.base_url,
                            project_name=self.project_name,
                            session=requests.Session(),
                        )
                        credentials.write_creds_file(self.data_dir)
                    self.token = credentials.token
                    self.base_url = self.base_url or credentials.logfire_api_url
                    if self.show_summary:  # pragma: no branch
                        credentials.print_token_summary()
                else:

                    def check_token():
                        assert self.token is not None
                        creds = self._initialize_credentials_from_token(self.token)
                        if self.show_summary and creds is not None:  # pragma: no branch
                            creds.print_token_summary()

                    thread = Thread(target=check_token, name='check_logfire_token')
                    thread.start()

                headers = {'User-Agent': f'logfire/{VERSION}', 'Authorization': self.token}
                session = OTLPExporterHttpSession(max_body_size=OTLP_MAX_BODY_SIZE)
                session.headers.update(headers)
                span_exporter = OTLPSpanExporter(endpoint=urljoin(self.base_url, '/v1/traces'), session=session)
                span_exporter = RetryFewerSpansSpanExporter(span_exporter)
                span_exporter = FallbackSpanExporter(
                    span_exporter, FileSpanExporter(self.data_dir / DEFAULT_FALLBACK_FILE_NAME, warn=True)
                )
                span_exporter = RemovePendingSpansExporter(span_exporter)
                schedule_delay_millis = _get_int_from_env(OTEL_BSP_SCHEDULE_DELAY) or 500
                add_span_processor(BatchSpanProcessor(span_exporter, schedule_delay_millis=schedule_delay_millis))

                metric_readers += [
                    PeriodicExportingMetricReader(
                        QuietMetricExporter(
                            OTLPMetricExporter(
                                endpoint=urljoin(self.base_url, '/v1/metrics'),
                                headers=headers,
                                session=session,
                                # I'm pretty sure that this line here is redundant,
                                # and that passing it to the QuietMetricExporter is what matters
                                # because the PeriodicExportingMetricReader will read it from there.
                                preferred_temporality=METRICS_PREFERRED_TEMPORALITY,
                            ),
                            preferred_temporality=METRICS_PREFERRED_TEMPORALITY,
                        )
                    )
                ]

            if processors_with_pending_spans:
                tracer_provider.add_span_processor(
                    PendingSpanProcessor(self.id_generator, tuple(processors_with_pending_spans))
                )

            otlp_endpoint = os.getenv(OTEL_EXPORTER_OTLP_ENDPOINT)
            otlp_traces_endpoint = os.getenv(OTEL_EXPORTER_OTLP_TRACES_ENDPOINT)
            otlp_metrics_endpoint = os.getenv(OTEL_EXPORTER_OTLP_METRICS_ENDPOINT)
            otlp_traces_exporter = os.getenv(OTEL_TRACES_EXPORTER, '').lower()
            otlp_metrics_exporter = os.getenv(OTEL_METRICS_EXPORTER, '').lower()

            if (otlp_endpoint or otlp_traces_endpoint) and otlp_traces_exporter in ('otlp', ''):
                add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

            if (otlp_endpoint or otlp_metrics_endpoint) and otlp_metrics_exporter in ('otlp', ''):
                metric_readers += [PeriodicExportingMetricReader(OTLPMetricExporter())]

            meter_provider = MeterProvider(
                metric_readers=metric_readers,
                resource=resource,
                views=[
                    View(
                        instrument_type=Histogram,
                        aggregation=ExponentialBucketHistogramAggregation(),
                    )
                ],
            )

            # we need to shut down any existing providers to avoid leaking resources (like threads)
            # but if this takes longer than 100ms you should call `logfire.shutdown` before reconfiguring
            self._meter_provider.shutdown(
                timeout_millis=200
            )  # note: this may raise an Exception if it times out, call `logfire.shutdown` first
            self._meter_provider.set_meter_provider(meter_provider)

            if self is GLOBAL_CONFIG and not self._has_set_providers:
                self._has_set_providers = True
                trace.set_tracer_provider(self._tracer_provider)
                metrics.set_meter_provider(self._meter_provider)

            @atexit.register
            def _exit_open_spans():  # type: ignore[reportUnusedFunction]  # pragma: no cover
                # Ensure that all open spans are closed when the program exits.
                # OTEL registers its own atexit callback in the tracer/meter providers to shut them down.
                # Registering this callback here after the OTEL one means that this runs first.
                # Otherwise OTEL would log an error "Already shutdown, dropping span."
                for span in list(OPEN_SPANS):
                    span.__exit__(None, None, None)

            self._initialized = True

            # set up context propagation for ThreadPoolExecutor and ProcessPoolExecutor
            instrument_executors()

            self._ensure_flush_after_aws_lambda()

            return self._tracer_provider

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Force flush all spans and metrics.

        Args:
            timeout_millis: The timeout in milliseconds.

        Returns:
            Whether the flush of spans was successful.
        """
        self._meter_provider.force_flush(timeout_millis)
        return self._tracer_provider.force_flush(timeout_millis)

    def get_tracer_provider(self) -> ProxyTracerProvider:
        """Get a tracer provider from this `LogfireConfig`.

        This is used internally and should not be called by users of the SDK.

        Returns:
            The tracer provider.
        """
        self.warn_if_not_initialized('No logs or spans will be created')
        return self._tracer_provider

    def get_meter_provider(self) -> ProxyMeterProvider:
        """Get a meter provider from this `LogfireConfig`.

        This is used internally and should not be called by users of the SDK.

        Returns:
            The meter provider.
        """
        self.warn_if_not_initialized('No metrics will be created')
        return self._meter_provider

    def warn_if_not_initialized(self, message: str):
        if not self._initialized and not self.ignore_no_config:
            warn_at_user_stacklevel(
                f'{message} until `logfire.configure()` has been called. '
                f'Set the environment variable LOGFIRE_IGNORE_NO_CONFIG=1 or add ignore_no_config=true in pyproject.toml to suppress this warning.',
                category=LogfireNotConfiguredWarning,
            )

    @cached_property
    def meter(self) -> metrics.Meter:
        """Get a meter from this `LogfireConfig`.

        This is used internally and should not be called by users of the SDK.

        Returns:
            The meter.
        """
        return self.get_meter_provider().get_meter('logfire', VERSION)

    def _initialize_credentials_from_token(self, token: str) -> LogfireCredentials | None:
        return LogfireCredentials.from_token(token, requests.Session(), self.base_url)

    def _ensure_flush_after_aws_lambda(self):
        """Ensure that `force_flush` is called after an AWS Lambda invocation.

        This way Logfire will just work in Lambda without the user needing to know anything.
        Without the `force_flush`, spans may just remain in the queue when the Lambda runtime is frozen.
        """

        def wrap_client_post_invocation_method(client_method: Any):  # pragma: no cover
            @functools.wraps(client_method)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    self.force_flush(timeout_millis=3000)
                except Exception:
                    import traceback

                    traceback.print_exc()

                return client_method(*args, **kwargs)

            return wrapper

        # This suggests that the lambda runtime module moves around a lot:
        # https://github.com/getsentry/sentry-python/blob/eab218c91ae2b894df18751e347fd94972a4fe06/sentry_sdk/integrations/aws_lambda.py#L280-L314
        # So we just look for the client class in all modules.
        # This feels inefficient but it appears be a tiny fraction of the time `configure` takes anyway.
        # We convert the modules to a list in case something gets imported during the loop and the dict gets modified.
        for mod in list(sys.modules.values()):
            try:
                client = getattr(mod, 'LambdaRuntimeClient', None)
            except Exception:  # pragma: no cover
                continue
            if not client:
                continue
            try:  # pragma: no cover
                client.post_invocation_error = wrap_client_post_invocation_method(client.post_invocation_error)
                client.post_invocation_result = wrap_client_post_invocation_method(client.post_invocation_result)
            except Exception as e:  # pragma: no cover
                with suppress(Exception):
                    # client is likely some random object from a dynamic module unrelated to AWS lambda.
                    # If it doesn't look like the LambdaRuntimeClient class, ignore this error.
                    # We don't check this beforehand so that if the lambda runtime library changes
                    # LambdaRuntimeClient to some object other than a class,
                    # or something else patches it with some kind of wrapper,
                    # our patching still has some chance of working.
                    # But we also don't want to log spurious noisy tracebacks.
                    if not (isinstance(client, type) and client.__name__ == 'LambdaRuntimeClient'):
                        continue

                import traceback

                traceback.print_exception(e)


# The global config is the single global object in logfire
# It also does not initialize anything when it's created (right now)
# but when `logfire.configure` aka `GLOBAL_CONFIG.configure` is called
# it will initialize the tracer and metrics
GLOBAL_CONFIG = LogfireConfig()


@dataclasses.dataclass
class LogfireCredentials:
    """Credentials for logfire.dev."""

    token: str
    """The Logfire API token to use."""
    project_name: str
    """The name of the project."""
    project_url: str
    """The URL for the project."""
    logfire_api_url: str
    """The Logfire API base URL."""

    @classmethod
    def load_creds_file(cls, creds_dir: Path) -> Self | None:
        """Check if a credentials file exists and if so load it.

        Args:
            creds_dir: Path to the credentials directory.

        Returns:
            The loaded credentials or `None` if the file does not exist.

        Raises:
            LogfireConfigError: If the credentials file exists but is invalid.
        """
        path = _get_creds_file(creds_dir)
        if path.exists():
            try:
                with path.open('rb') as f:
                    data = json.load(f)
            except (ValueError, OSError) as e:
                raise LogfireConfigError(f'Invalid credentials file: {path}') from e

            try:
                # Handle legacy key
                dashboard_url = data.pop('dashboard_url', None)
                if dashboard_url is not None:
                    data.setdefault('project_url', dashboard_url)
                return cls(**data)
            except TypeError as e:
                raise LogfireConfigError(f'Invalid credentials file: {path} - {e}') from e

    @classmethod
    def from_token(cls, token: str, session: requests.Session, base_url: str) -> Self | None:
        """Check that the token is valid.

        Issue a warning if the Logfire API is unreachable, or we get a response other than 200 or 401.

        We continue unless we get a 401. If something is wrong, we'll later store data locally for back-fill.

        Raises:
            LogfireConfigError: If the token is invalid.
        """
        try:
            response = session.get(
                urljoin(base_url, '/v1/info'),
                timeout=10,
                headers={**COMMON_REQUEST_HEADERS, 'Authorization': token},
            )
        except requests.RequestException as e:
            warnings.warn(f'Logfire API is unreachable, you may have trouble sending data. Error: {e}')
            return None

        if response.status_code == 401:
            warnings.warn('Invalid Logfire token.')
            return None
        elif response.status_code != 200:
            # any other status code is considered unhealthy
            warnings.warn(
                f'Logfire API is unhealthy, you may have trouble sending data. Status code: {response.status_code}'
            )
            return None

        data = response.json()
        return cls(
            token=token,
            project_name=data['project_name'],
            project_url=data['project_url'],
            logfire_api_url=base_url,
        )

    @classmethod
    def _get_user_token(cls, logfire_api_url: str) -> str:
        if DEFAULT_FILE.is_file():  # pragma: no branch
            data = cast(DefaultFile, read_toml_file(DEFAULT_FILE))
            if is_logged_in(data, logfire_api_url):  # pragma: no branch
                return data['tokens'][logfire_api_url]['token']
        raise LogfireConfigError(
            """You are not authenticated. Please run `logfire auth` to authenticate.

If you are running in production, you can set the `LOGFIRE_TOKEN` environment variable.
To create a write token, refer to https://docs.pydantic.dev/logfire/guides/advanced/creating_write_tokens/
"""
        )

    @classmethod
    def get_current_user(cls, session: requests.Session, logfire_api_url: str) -> dict[str, Any] | None:
        try:
            user_token = cls._get_user_token(logfire_api_url=logfire_api_url)
        except LogfireConfigError:
            return None
        return cls._get_user_for_token(user_token, session, logfire_api_url)

    @classmethod
    def _get_user_for_token(cls, user_token: str, session: requests.Session, logfire_api_url: str) -> dict[str, Any]:
        headers = {**COMMON_REQUEST_HEADERS, 'Authorization': user_token}
        account_info_url = urljoin(logfire_api_url, '/v1/account/me')
        try:
            response = session.get(account_info_url, headers=headers)
            UnexpectedResponse.raise_for_status(response)
        except requests.RequestException as e:
            raise LogfireConfigError('Error retrieving user information.') from e
        return response.json()

    @classmethod
    def get_user_projects(cls, session: requests.Session, logfire_api_url: str) -> list[dict[str, Any]]:
        """Get list of projects that user has access to them.

        Args:
            session: HTTP client session used to communicate with the Logfire API.
            logfire_api_url: The Logfire API base URL.

        Returns:
            List of user projects.

        Raises:
            LogfireConfigError: If there was an error retrieving user projects.
        """
        user_token = cls._get_user_token(logfire_api_url=logfire_api_url)
        headers = {**COMMON_REQUEST_HEADERS, 'Authorization': user_token}
        projects_url = urljoin(logfire_api_url, '/v1/projects/')
        try:
            response = session.get(projects_url, headers=headers)
            UnexpectedResponse.raise_for_status(response)
        except requests.RequestException as e:  # pragma: no cover
            raise LogfireConfigError('Error retrieving list of projects.') from e
        return response.json()

    @classmethod
    def use_existing_project(
        cls,
        *,
        session: requests.Session,
        logfire_api_url: str,
        projects: list[dict[str, Any]],
        organization: str | None = None,
        project_name: str | None = None,
    ) -> dict[str, Any] | None:
        """Configure one of the user projects to be used by Logfire.

        It configures the project if organization/project_name is a valid project that
        the user has access to it. Otherwise, it asks the user to select a project interactively.

        Args:
            session: HTTP client session used to communicate with the Logfire API.
            logfire_api_url: The Logfire API base URL.
            projects: List of user projects.
            organization: Project organization.
            project_name: Name of project that has to be used.

        Returns:
            The configured project information.

        Raises:
            LogfireConfigError: If there was an error configuring the project.
        """
        user_token = cls._get_user_token(logfire_api_url=logfire_api_url)
        headers = {**COMMON_REQUEST_HEADERS, 'Authorization': user_token}

        org_message = ''
        org_flag = ''
        project_message = 'projects'
        filtered_projects = projects

        console = Console(file=sys.stderr)

        if organization is not None:
            filtered_projects = [p for p in projects if p['organization_name'] == organization]
            org_message = f' in organization `{organization}`'
            org_flag = f' --org {organization}'

        if project_name is not None:
            project_message = f'projects with name `{project_name}`'
            filtered_projects = [p for p in filtered_projects if p['project_name'] == project_name]

        if project_name is not None and len(filtered_projects) == 1:
            # exact match to requested project
            organization = filtered_projects[0]['organization_name']
            project_name = filtered_projects[0]['project_name']
        elif not filtered_projects:
            if not projects:
                console.print(
                    'No projects found for the current user. You can create a new project with `logfire projects new`'
                )
                return None
            elif (
                Prompt.ask(
                    f'No {project_message} found for the current user{org_message}. Choose from all projects?',
                    choices=['y', 'n'],
                    default='y',
                )
                == 'n'
            ):
                # user didn't want to expand search, print a hint and quit
                console.print(f'You can create a new project{org_message} with `logfire projects new{org_flag}`')
                return None
            # try all projects
            filtered_projects = projects
            organization = None
            project_name = None
        else:
            # multiple matches
            if project_name is not None and organization is None:
                # only bother printing if the user asked for a specific project
                # but didn't specify an organization
                console.print(f'Found multiple {project_message}.')
            organization = None
            project_name = None

        if organization is None or project_name is None:
            project_choices = {
                str(index + 1): (item['organization_name'], item['project_name'])
                for index, item in enumerate(filtered_projects)
            }
            project_choices_str = '\n'.join(
                [f'{index}. {item[0]}/{item[1]}' for index, item in project_choices.items()]
            )
            selected_project_key = Prompt.ask(
                f'Please select one of the following projects by number:\n' f'{project_choices_str}\n',
                choices=list(project_choices.keys()),
                default='1',
            )
            project_info_tuple = project_choices[selected_project_key]
            organization = project_info_tuple[0]
            project_name = project_info_tuple[1]

        project_write_token_url = urljoin(
            logfire_api_url,
            f'/v1/organizations/{organization}/projects/{project_name}/write-tokens/',
        )
        try:
            response = session.post(project_write_token_url, headers=headers)
            UnexpectedResponse.raise_for_status(response)
        except requests.RequestException as e:
            raise LogfireConfigError('Error creating project write token.') from e

        return response.json()

    @classmethod
    def create_new_project(
        cls,
        *,
        session: requests.Session,
        logfire_api_url: str,
        organization: str | None = None,
        default_organization: bool = False,
        project_name: str | None = None,
        force_project_name_prompt: bool = False,
    ) -> dict[str, Any]:
        """Create a new project and configure it to be used by Logfire.

        It creates the project under the organization if both project and organization are valid.
        Otherwise, it asks the user to select organization and enter a valid project name interactively.

        Args:
            session: HTTP client session used to communicate with the Logfire API.
            logfire_api_url: The Logfire API base URL.
            organization: The organization name of the new project.
            default_organization: Whether to create the project under the user default organization.
            project_name: The default name of the project.
            force_project_name_prompt: Whether to force a prompt for the project name.
            service_name: Name of the service.

        Returns:
            The created project informations.

        Raises:
            LogfireConfigError: If there was an error creating projects.
        """
        user_token = cls._get_user_token(logfire_api_url=logfire_api_url)
        headers = {**COMMON_REQUEST_HEADERS, 'Authorization': user_token}

        # Get user organizations
        organizations_url = urljoin(logfire_api_url, '/v1/organizations/')
        try:
            response = session.get(organizations_url, headers=headers)
            UnexpectedResponse.raise_for_status(response)
        except requests.RequestException as e:
            raise LogfireConfigError('Error retrieving list of organizations.') from e
        organizations = [item['organization_name'] for item in response.json()]

        if organization not in organizations:
            if len(organizations) > 1:
                # Get user default organization
                user_details = cls._get_user_for_token(user_token, session, logfire_api_url)
                assert user_details is not None
                user_default_organization_name = user_details.get('default_organization', {}).get('organization_name')

                if default_organization and user_default_organization_name:
                    organization = user_default_organization_name
                else:
                    organization = Prompt.ask(
                        '\nTo create and use a new project, please provide the following information:\n'
                        'Select the organization to create the project in',
                        choices=organizations,
                        default=user_default_organization_name if user_default_organization_name else organizations[0],
                    )
            else:
                organization = organizations[0]
                if not default_organization:
                    confirm = Confirm.ask(
                        f'The project will be created in the organization "{organization}". Continue?', default=True
                    )
                    if not confirm:
                        sys.exit(1)

        project_name_default: str | None = project_name or default_project_name()
        project_name_prompt = 'Enter the project name'
        while True:
            if force_project_name_prompt or not project_name:
                project_name = Prompt.ask(project_name_prompt, default=project_name_default)
            while project_name and not re.match(PROJECT_NAME_PATTERN, project_name):
                project_name = Prompt.ask(
                    "\nThe project name you've entered is invalid. Valid project names:\n"
                    '  * may contain lowercase alphanumeric characters\n'
                    '  * may contain single hyphens\n'
                    '  * may not start or end with a hyphen\n\n'
                    'Enter the project name you want to use:',
                    default=project_name_default,
                )

            url = urljoin(logfire_api_url, f'/v1/projects/{organization}')
            try:
                response = session.post(url, headers=headers, json={'project_name': project_name})
                if response.status_code == 409:
                    project_name_default = ...  # type: ignore  # this means the value is required
                    project_name_prompt = (
                        f"\nA project with the name '{project_name}' already exists."
                        f' Please enter a different project name'
                    )
                    project_name = None
                    continue
                if response.status_code == 422:
                    error = response.json()['detail'][0]
                    if error['loc'] == ['body', 'project_name']:  # pragma: no branch
                        project_name_default = ...  # type: ignore  # this means the value is required
                        project_name_prompt = (
                            f'\nThe project name you entered is invalid:\n'
                            f'{error["msg"]}\n'
                            f'Please enter a different project name'
                        )
                        project_name = None
                        continue
                UnexpectedResponse.raise_for_status(response)
            except requests.RequestException as e:
                raise LogfireConfigError('Error creating new project.') from e
            else:
                return response.json()

    @classmethod
    def initialize_project(
        cls,
        *,
        logfire_api_url: str,
        project_name: str | None,
        session: requests.Session,
    ) -> Self:
        """Create a new project or use an existing project on logfire.dev requesting the given project name.

        Args:
            logfire_api_url: The Logfire API base URL.
            project_name: Name for the project.
            user_token: The user's token to use to create the new project.
            session: HTTP client session used to communicate with the Logfire API.

        Returns:
            The new credentials.

        Raises:
            LogfireConfigError: If there was an error on creating/configuring the project.
        """
        credentials: dict[str, Any] | None = None

        print(
            'No Logfire project credentials found.\n'  # TODO: Add a link to the docs about where we look
            'All data sent to Logfire must be associated with a project.\n'
        )

        projects = cls.get_user_projects(session=session, logfire_api_url=logfire_api_url)
        if projects:
            use_existing_projects = Confirm.ask('Do you want to use one of your existing projects? ', default=True)
            if use_existing_projects:  # pragma: no branch
                credentials = cls.use_existing_project(
                    session=session, logfire_api_url=logfire_api_url, projects=projects
                )

        if not credentials:
            credentials = cls.create_new_project(
                session=session,
                logfire_api_url=logfire_api_url,
                project_name=project_name,
                force_project_name_prompt=True,
            )

        try:
            result = cls(**credentials, logfire_api_url=logfire_api_url)
            Prompt.ask(
                f'Project initialized successfully. You will be able to view it at: {result.project_url}\n'
                'Press Enter to continue'
            )
            return result
        except TypeError as e:  # pragma: no cover
            raise LogfireConfigError(f'Invalid credentials, when initializing project: {e}') from e

    def write_creds_file(self, creds_dir: Path) -> None:
        """Write a credentials file to the given path."""
        ensure_data_dir_exists(creds_dir)
        data = dataclasses.asdict(self)
        path = _get_creds_file(creds_dir)
        path.write_text(json.dumps(data, indent=2) + '\n')

    def print_token_summary(self) -> None:
        """Print a summary of the existing project."""
        if self.project_url:  # pragma: no branch
            _print_summary(
                f'[bold]Logfire[/bold] project URL: [link={self.project_url} cyan]{self.project_url}[/link]',
                min_content_width=len(self.project_url),
            )


def _print_summary(message: str, min_content_width: int) -> None:
    from rich.console import Console
    from rich.style import Style
    from rich.theme import Theme

    # customise the link color since the default `blue` is too dark for me to read.
    custom_theme = Theme({'markdown.link_url': Style(color='cyan')})
    console = Console(stderr=True, theme=custom_theme)
    if console.width < min_content_width + 4:  # pragma: no cover
        console.width = min_content_width + 4
    console.print(message)


def _get_creds_file(creds_dir: Path) -> Path:
    """Get the path to the credentials file."""
    return creds_dir / CREDENTIALS_FILENAME


def get_git_revision_hash() -> str:
    """Get the current git commit hash."""
    import subprocess

    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], stderr=subprocess.STDOUT).decode('ascii').strip()


def sanitize_project_name(name: str) -> str:
    """Convert `name` to a string suitable for the `requested_project_name` API parameter."""
    # Project names are limited to 50 characters, but the backend may also add 9 characters
    # if the project name already exists, so we limit it to 41 characters.
    return re.sub(r'[^a-zA-Z0-9]', '', name).lower()[:41] or 'untitled'


def default_project_name():
    return sanitize_project_name(os.path.basename(os.getcwd()))


class LogfireNotConfiguredWarning(UserWarning):
    pass
