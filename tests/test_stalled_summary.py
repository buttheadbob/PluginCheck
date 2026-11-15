"""A behaviour stage stopped by its stall watchdog shows as a failure, never as a quiet "not run", and check.py exits 1 for it.
Run with: python -I -m unittest discover -s tests"""
import ast, contextlib, io, os, sys, threading, types, unittest
from unittest import mock
import scratchmachine  # noqa: F401  (before anything of PluginCheck's: keeps every test off the real lease folders)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'stages'))
CHECK = open(os.path.join(ROOT, 'check.py'), encoding='utf-8').read()
REASON = 'stopped because Docker stalled: plugincheck-behaviour-1-1 has run for 1600s, past the 1500s any server is given'
STAGES = {'manifest': {'status': 'pass'}, 'behaviour': {'status': 'not run', 'reason': REASON, 'stalled': True}, 'logcheck': {'status': 'review'}}


def display_module():
    import display
    return display


class SummaryTests(unittest.TestCase):
    def summary(self, stages):
        display = display_module()
        with mock.patch.dict(os.environ, {'PLUGINCHECK_PLAIN': '1'}), contextlib.redirect_stdout(io.StringIO()) as out:
            shown = display.Display('Demo', list(stages), {}, lambda: 0, None)
            shown.summary(stages, 'results.json')
        return out.getvalue().splitlines()

    def test_a_stalled_stage_is_listed_as_stopped_and_called_out_as_failed(self):
        lines = self.summary(STAGES)
        self.assertIn(f'behaviour  stopped: Docker stalled - {REASON}', lines)
        self.assertIn('FAILED  behaviour stopped: Docker stalled: this run is not a pass. Run it again once Docker answers.', lines)
        self.assertNotIn('behaviour  not run', '\n'.join(lines))

    def test_a_plain_not_run_is_unchanged(self):
        lines = self.summary({'behaviour': {'status': 'not run', 'reason': 'no build to run'}})
        self.assertEqual(lines, ['behaviour  not run - no build to run', 'results.json'])

    def test_stalled_lists_only_stalled_stages(self):
        self.assertEqual(display_module().stalled(STAGES), ['behaviour'])
        self.assertEqual(display_module().stalled({'behaviour': {'status': 'not run'}}), [])


class CheckTests(unittest.TestCase):
    def exit_code(self, stages, stopping=False):
        """check.py's last statement, sys.exit(...), evaluated on these stage outcomes."""
        last = ast.parse(CHECK).body[-1]
        self.assertEqual(ast.unparse(last.value.func), 'sys.exit')
        event = threading.Event()
        if stopping:
            event.set()
        return eval(compile(ast.Expression(last.value.args[0]), 'check.py', 'eval'),
                    {'stopping': event, 'stalled': getattr(display_module(), 'stalled', lambda stages: []), 'results': {'stages': stages}})

    def test_a_stalled_stage_makes_the_exit_code_non_zero(self):
        self.assertEqual(self.exit_code(STAGES), 1)

    def test_a_run_with_no_failure_still_exits_0(self):
        self.assertEqual(self.exit_code({'manifest': {'status': 'pass'}, 'behaviour': {'status': 'not run', 'reason': 'no build'}}), 0)
        self.assertEqual(self.exit_code({'manifest': {'status': 'fail'}}), 1)

    def test_the_stage_ends_as_a_failure_line_on_the_panel(self):
        """run_stage's last statement, with the display recorded: a stalled outcome is finished as 'fail' with a FAILED line."""
        run_stage = next(n for n in ast.parse(CHECK).body if isinstance(n, ast.FunctionDef) and n.name == 'run_stage')
        last = run_stage.body[-1]
        finished = []
        space = {'display': types.SimpleNamespace(stage_finished=lambda *a: finished.append(a)), 'clock': lambda s: '1m00s', 'left_text': lambda: 'no time',
                 'STALLED': 'stopped: Docker stalled', 'stage': 'behaviour', 'seconds': 60}
        for outcome, expected in ((STAGES['behaviour'], ('behaviour', 'fail', f'FAILED  behaviour stopped: Docker stalled after 1m00s: {REASON}')),
                                  ({'status': 'review'}, ('behaviour', 'review', 'behaviour: review in 1m00s. About no time.'))):
            finished.clear()
            exec(compile(ast.Module(body=[last], type_ignores=[]), 'check.py', 'exec'), {**space, 'outcome': outcome})
            self.assertEqual(finished, [expected])

    def test_the_failure_line_is_coloured_and_counted_as_failed(self):
        display = display_module()
        self.assertEqual(display.STALLED, 'stopped: Docker stalled')
        line = f'FAILED  behaviour {display.STALLED} after 1m00s: {REASON}'
        self.assertTrue(any(pattern.match(line) for name, pattern, _ in display.COUNTED if name == 'FAILED'))


if __name__ == '__main__':
    unittest.main()
