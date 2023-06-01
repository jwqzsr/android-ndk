#!/usr/bin/env python3
#
# Copyright (C) 2017 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""debug_unwind_reporter.py: report failed dwarf unwinding cases generated by debug-unwind cmd.

Below is an example using debug_unwind_reporter.py:
1. Record with "-g --keep-failed-unwinding-debug-info" option on device.
    $ simpleperf record -g --keep-failed-unwinding-debug-info --app com.google.sample.tunnel \\
        --duration 10
    The generated perf.data can be used for normal reporting. But it also contains stack data
    and binaries for debugging failed unwinding cases.

2. Generate report with debug-unwind cmd.
    $ simpleperf debug-unwind -i perf.data --generate-report -o report.txt
    The report contains details for each failed unwinding case. It is usually too long to
    parse manually. That's why we need debug_unwind_reporter.py.

3. Use debug_unwind_reporter.py to parse the report.
    $ simpleperf debug-unwind -i report.txt --summary
    $ simpleperf debug-unwind -i report.txt --include-error-code 1
    ...
"""

import argparse
from collections import Counter, defaultdict
from simpleperf_utils import BaseArgumentParser
from texttable import Texttable
from typing import Dict, Iterator, List


class CallChainNode:
    def __init__(self):
        self.dso = ''
        self.symbol = ''


class Sample:
    """ A failed unwinding case """

    def __init__(self, raw_lines: List[str]):
        self.raw_lines = raw_lines
        self.sample_time = 0
        self.error_code = 0
        self.callchain: List[CallChainNode] = []
        self.parse()

    def parse(self):
        for line in self.raw_lines:
            key, value = line.split(': ', 1)
            if key == 'sample_time':
                self.sample_time = int(value)
            elif key == 'unwinding_error_code':
                self.error_code = int(value)
            elif key.startswith('dso'):
                callchain_id = int(key.rsplit('_', 1)[1])
                self._get_callchain_node(callchain_id).dso = value
            elif key.startswith('symbol'):
                callchain_id = int(key.rsplit('_', 1)[1])
                self._get_callchain_node(callchain_id).symbol = value

    def _get_callchain_node(self, callchain_id: int) -> CallChainNode:
        callchain_id -= 1
        if callchain_id == len(self.callchain):
            self.callchain.append(CallChainNode())
        return self.callchain[callchain_id]


class SampleFilter:
    def match(self, sample: Sample) -> bool:
        raise Exception('unimplemented')


class CompleteCallChainFilter(SampleFilter):
    def match(self, sample: Sample) -> bool:
        for node in sample.callchain:
            if node.dso.endswith('libc.so') and (node.symbol in ('__libc_init', '__start_thread')):
                return True
        return False


class ErrorCodeFilter(SampleFilter):
    def __init__(self, error_code: List[int]):
        self.error_code = set(error_code)

    def match(self, sample: Sample) -> bool:
        return sample.error_code in self.error_code


class EndDsoFilter(SampleFilter):
    def __init__(self, end_dso: List[str]):
        self.end_dso = set(end_dso)

    def match(self, sample: Sample) -> bool:
        return sample.callchain[-1].dso in self.end_dso


class EndSymbolFilter(SampleFilter):
    def __init__(self, end_symbol: List[str]):
        self.end_symbol = set(end_symbol)

    def match(self, sample: Sample) -> bool:
        return sample.callchain[-1].symbol in self.end_symbol


class SampleTimeFilter(SampleFilter):
    def __init__(self, sample_time: List[int]):
        self.sample_time = set(sample_time)

    def match(self, sample: Sample) -> bool:
        return sample.sample_time in self.sample_time


class ReportInput:
    def __init__(self):
        self.exclude_filters: List[SampleFilter] = []
        self.include_filters: List[SampleFilter] = []

    def set_filters(self, args: argparse.Namespace):
        if not args.show_callchain_fixed_by_joiner:
            self.exclude_filters.append(CompleteCallChainFilter())
        if args.exclude_error_code:
            self.exclude_filters.append(ErrorCodeFilter(args.exclude_error_code))
        if args.exclude_end_dso:
            self.exclude_filters.append(EndDsoFilter(args.exclude_end_dso))
        if args.exclude_end_symbol:
            self.exclude_filters.append(EndSymbolFilter(args.exclude_end_symbol))
        if args.exclude_sample_time:
            self.exclude_filters.append(SampleTimeFilter(args.exclude_sample_time))

        if args.include_error_code:
            self.include_filters.append(ErrorCodeFilter(args.include_error_code))
        if args.include_end_dso:
            self.include_filters.append(EndDsoFilter(args.include_end_dso))
        if args.include_end_symbol:
            self.include_filters.append(EndSymbolFilter(args.include_end_symbol))
        if args.include_sample_time:
            self.include_filters.append(SampleTimeFilter(args.include_sample_time))

    def get_samples(self, input_file: str) -> Iterator[Sample]:
        sample_lines: List[str] = []
        in_sample = False
        with open(input_file, 'r') as fh:
            for line in fh.readlines():
                line = line.rstrip()
                if line.startswith('sample_time:'):
                    in_sample = True
                elif not line:
                    if in_sample:
                        in_sample = False
                        sample = Sample(sample_lines)
                        sample_lines = []
                        if self.filter_sample(sample):
                            yield sample
                if in_sample:
                    sample_lines.append(line)

    def filter_sample(self, sample: Sample) -> bool:
        """ Return true if the input sample passes filters. """
        for exclude_filter in self.exclude_filters:
            if exclude_filter.match(sample):
                return False
        for include_filter in self.include_filters:
            if not include_filter.match(sample):
                return False
        return True


class ReportOutput:
    def report(self, sample: Sample):
        pass

    def end_report(self):
        pass


class ReportOutputDetails(ReportOutput):
    def report(self, sample: Sample):
        for line in sample.raw_lines:
            print(line)
        print()


class ReportOutputSummary(ReportOutput):
    def __init__(self):
        self.error_code_counter = Counter()
        self.symbol_counters: Dict[int, Counter] = defaultdict(Counter)

    def report(self, sample: Sample):
        symbol_key = (sample.callchain[-1].dso, sample.callchain[-1].symbol)
        self.symbol_counters[sample.error_code][symbol_key] += 1
        self.error_code_counter[sample.error_code] += 1

    def end_report(self):
        self.draw_error_code_table()
        self.draw_symbol_table()

    def draw_error_code_table(self):
        table = Texttable()
        table.set_cols_align(['l', 'c'])
        table.add_row(['Count', 'Error Code'])
        for error_code, count in self.error_code_counter.most_common():
            table.add_row([count, error_code])
        print(table.draw())

    def draw_symbol_table(self):
        table = Texttable()
        table.set_cols_align(['l', 'c', 'l', 'l'])
        table.add_row(['Count', 'Error Code', 'Dso', 'Symbol'])
        for error_code, _ in self.error_code_counter.most_common():
            symbol_counter = self.symbol_counters[error_code]
            for symbol_key, count in symbol_counter.most_common():
                dso, symbol = symbol_key
                table.add_row([count, error_code, dso, symbol])
        print(table.draw())


def get_args() -> argparse.Namespace:
    parser = BaseArgumentParser(description=__doc__)
    parser.add_argument('-i', '--input-file', required=True,
                        help='report file generated by debug-unwind cmd')
    parser.add_argument(
        '--show-callchain-fixed-by-joiner', action='store_true',
        help="""By default, we don't show failed unwinding cases fixed by callchain joiner.
                Use this option to show them.""")
    parser.add_argument('--summary', action='store_true',
                        help='show summary instead of case details')
    parser.add_argument('--exclude-error-code', metavar='error_code', type=int, nargs='+',
                        help='exclude cases with selected error code')
    parser.add_argument('--exclude-end-dso', metavar='dso', nargs='+',
                        help='exclude cases ending at selected binary')
    parser.add_argument('--exclude-end-symbol', metavar='symbol', nargs='+',
                        help='exclude cases ending at selected symbol')
    parser.add_argument('--exclude-sample-time', metavar='time', type=int,
                        nargs='+', help='exclude cases with selected sample time')
    parser.add_argument('--include-error-code', metavar='error_code', type=int,
                        nargs='+', help='include cases with selected error code')
    parser.add_argument('--include-end-dso', metavar='dso', nargs='+',
                        help='include cases ending at selected binary')
    parser.add_argument('--include-end-symbol', metavar='symbol', nargs='+',
                        help='include cases ending at selected symbol')
    parser.add_argument('--include-sample-time', metavar='time', type=int,
                        nargs='+', help='include cases with selected sample time')
    return parser.parse_args()


def main():
    args = get_args()
    report_input = ReportInput()
    report_input.set_filters(args)
    report_output = ReportOutputSummary() if args.summary else ReportOutputDetails()
    for sample in report_input.get_samples(args.input_file):
        report_output.report(sample)
    report_output.end_report()


if __name__ == '__main__':
    main()
