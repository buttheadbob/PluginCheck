"""A server that has stopped logging ends as a timeout after 10 minutes, not 20.
Run with: python -I -m unittest discover -s tests"""
import ast, itertools, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import replay
from test_free_slot import LiftedServe

SOURCE = os.path.join(replay.STAGES, 'behaviour.py')


def stall_limit(options):
    tree = ast.parse(open(SOURCE, encoding='utf-8').read())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'stall_limit']
    nodes += [n for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, 'id', '') == 'STALL_SECONDS' for t in n.targets)]
    namespace = {'options': options}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), SOURCE, 'exec'), namespace)
    return namespace['stall_limit']


class LimitTests(unittest.TestCase):
    def test_ten_minutes(self):
        self.assertEqual(stall_limit({})([{'name': 'a', 'command': 'x'}]), 600)
        self.assertEqual(stall_limit({})([{'name': 'idle', 'min': 120}]), 600)
        self.assertEqual(stall_limit({'settleSeconds': 70})([{'name': 'a', 'settle': 170}]), 600)

    def test_a_scenario_that_waits_longer_in_one_go_gets_that_and_a_minute(self):
        self.assertEqual(stall_limit({})([{'name': 'a'}, {'name': 'b', 'min': 900}]), 960)
        self.assertEqual(stall_limit({'settleSeconds': 700})([{'name': 'a'}]), 760)


class StallTests(LiftedServe):
    """serve() lifted, with the stall limit at 2 s and Docker checked every second."""

    def test_a_server_that_stops_logging_ends_as_a_timeout(self):
        records, calls, freed, timing = self.serve('torch', lambda: 'waiting\n1200 total\n[INFO] loading world', stall=2)
        self.assertEqual(records[0]['state'], 'timeout')
        self.assertIn('wrote no log line for its last', records[0]['failed'])
        self.assertLess(timing['stopped'] - timing['up'], 6)
        self.assertTrue(any(c[0] == 'kill' for c in calls))

    def test_a_server_that_keeps_logging_is_never_cut(self):
        sizes = itertools.count(100)
        records, _, _, _ = self.serve('magnetar', lambda: f'waiting\n{next(sizes)} total\nstill going', marker_after=3.5, stall=2)
        self.assertEqual(records[0]['state'], 'recorded')

    def test_a_server_that_has_not_logged_yet_is_left_to_the_start_limit(self):
        """Nothing in its log folders yet, such as a first start still setting up: the stall clock has not started."""
        records, _, _, timing = self.serve('torch', lambda: 'waiting', stall=1, start_timeout=4)
        self.assertEqual(records[0]['state'], 'timeout')
        self.assertNotIn('wrote no log line', records[0]['failed'])
        self.assertGreaterEqual(timing['stopped'] - timing['up'], 4)

    def test_the_clock_starts_at_the_first_log_line(self):
        answers = itertools.chain(['waiting'] * 3, itertools.repeat('waiting\n800 total\n[INFO] loading world'))
        records, _, _, timing = self.serve('torch', lambda: next(answers), stall=2, start_timeout=20)
        self.assertIn('wrote no log line for its last', records[0]['failed'])
        self.assertGreaterEqual(timing['stopped'] - timing['up'], 4.5)

    def test_a_server_compiling_is_never_cut(self):
        records, _, _, timing = self.serve('magnetar', lambda: 'waiting\n900 total\nCompiling files for Demo', stall=1, start_timeout=4)
        self.assertEqual(records[0]['state'], 'timeout')
        self.assertNotIn('wrote no log line', records[0]['failed'])
        self.assertGreaterEqual(timing['stopped'] - timing['up'], 4)

    def test_the_check_reports_log_size_and_the_newest_line(self):
        _, calls, _, _ = self.serve('legacy', ['recorded'])
        script = calls[0][-1]
        self.assertIn('find /data/Magnetar /data/Instance -name "*.log"', script)
        self.assertIn('wc -c $logs | tail -n 1', script)
        self.assertTrue(script.endswith('; true'))


class SourceTests(unittest.TestCase):
    def test_agent_md_states_the_exception(self):
        agent = open(os.path.join(HERE, '..', 'Agent.md'), encoding='utf-8').read()
        rule = agent[agent.index('- Never cap a run.'):]
        rule = rule[:rule.index('\n- ')]
        self.assertIn('The one exception', rule)
        self.assertIn('10 minutes', rule)


if __name__ == '__main__':
    unittest.main()
