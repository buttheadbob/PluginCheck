"""Per-server timing in behaviour. Run with: python -I -m unittest discover -s tests"""
import ast, copy, json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = os.path.join(HERE, '..', 'stages')
sys.path.insert(0, os.path.join(STAGES, 'behaviour'))
import timing


def comparison_functions():
    """behaviour.py runs a whole stage when imported, so its comparison functions are lifted out of the source and run alone."""
    source = open(os.path.join(STAGES, 'behaviour.py'), encoding='utf-8').read()
    tree = ast.parse(source)
    wanted = {'differences', 'replies_of', 'touched', 'changes', 'disagreements'}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    nodes += [n for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, 'id', '') == 'COMPARED' for t in n.targets)]
    namespace = {'re': __import__('re')}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'behaviour.py', 'exec'), namespace)
    return namespace


class SummaryTests(unittest.TestCase):
    def test_a_full_server(self):
        t = {'server': '0012-Monitor', 'cores': '4-5', 'queued': 100.0, 'slot': 103.0, 'up': 112.0, 'stopped': 233.0, 'state': 'recorded',
             'killed': 234.0, 'cleaned': 245.0, 'freed': 245.5}
        line, out = timing.summary(t, {'readySeconds': 85.0, 'ticks': 5000, 'simSpeed': 0.98})
        self.assertEqual(out, {'waitedSeconds': 3.0, 'upSeconds': 9.0, 'stoppedSeconds': 130.0, 'killedSeconds': 131.0, 'cleanedSeconds': 142.0,
                               'freedSeconds': 142.5, 'readySeconds': 45.0, 'simSpeed': 0.98, 'state': 'recorded'})
        self.assertEqual(line, '  timing 0012-Monitor on 4-5: waited 3s for a slot, up 9s, game ready 45s, recorded 130s, killed 131s, '
                               'cleaned up 142s, slot freed 142.5s, sim speed 0.98')

    def test_a_server_that_timed_out_has_no_probe_timing(self):
        t = {'server': '0003-x', 'cores': '0-1', 'queued': 0.0, 'slot': 0.0, 'up': 5.0, 'stopped': 1205.0, 'state': 'timeout', 'killed': 1206.0, 'freed': 1210.0}
        line, out = timing.summary(t, None)
        self.assertNotIn('readySeconds', out)
        self.assertNotIn('simSpeed', out)
        self.assertIn('timeout 1205s', line)

    def test_a_server_that_failed_before_its_container_started(self):
        line, out = timing.summary({'queued': 1.0, 'slot': 2.0, 'cores': '0-1', 'freed': 2.5}, None)
        self.assertEqual(out['freedSeconds'], 0.5)
        self.assertIn('slot freed 0.5s', line)

    def test_nothing_without_a_slot(self):
        self.assertEqual(timing.summary({'queued': 1.0}, None), (None, {}))

    def test_odd_probe_values_are_left_out(self):
        _, out = timing.summary({'queued': 0, 'slot': 0, 'stopped': 10}, {'readySeconds': 'x', 'simSpeed': None})
        self.assertNotIn('readySeconds', out)
        self.assertNotIn('simSpeed', out)


class CacheTests(unittest.TestCase):
    """A kept result is reused by later runs, so it must not carry the run's own timing."""

    def test_without_drops_only_the_timing_fields_and_leaves_the_records_alone(self):
        records = [{'state': 'recorded', 'after': {'grids': {}}, 'timing': {'upSeconds': 9}, 'probeTiming': {'ticks': 1}}, {'state': 'timeout'}]
        kept = timing.without(records)
        self.assertEqual(kept, [{'state': 'recorded', 'after': {'grids': {}}}, {'state': 'timeout'}])
        self.assertIn('timing', records[0])
        self.assertIn('probeTiming', records[0])

    def remember_functions(self, folder, results):
        """run_and_remember and run_and_remember_discovery lifted out of behaviour.py, with a server that returns these results."""
        tree = ast.parse(open(os.path.join(STAGES, 'behaviour.py'), encoding='utf-8').read())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('run_and_remember', 'run_and_remember_discovery')]
        namespace = {'os': os, 'json': json, 'server_timing': timing, 'cache': folder, 'save': None, 'probe_zip': None,
                     'job_key': lambda build, batch, repeat: f"{build['name']}-{batch[0].get('mode', 'run')}-{repeat}",
                     'run_with_retry': lambda *args: copy.deepcopy(results)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'behaviour.py', 'exec'), namespace)
        return namespace

    def test_results_and_discoveries_are_kept_without_timing(self):
        measured = {'timing': {'upSeconds': 9, 'state': 'recorded'}, 'probeTiming': {'readySeconds': 80, 'ticks': 4800, 'simSpeed': 1.0}}
        with tempfile.TemporaryDirectory() as folder:
            f = self.remember_functions(folder, [{'state': 'recorded', 'after': {'grids': {}}, 'commands': [], 'scenario': 's', **measured}])
            returned = f['run_and_remember']({'name': 'b'}, [{'name': 's'}], 1)
            self.assertEqual(returned[0]['timing'], measured['timing'], 'this run still reports its own timing')
            discovered = f['run_and_remember_discovery']({'name': 'b'})
            self.assertIn('probeTiming', discovered[0])
            for name in ('b-run-1.json', 'b-discover-0.json'):
                kept = json.load(open(os.path.join(folder, 'results', name), encoding='utf-8'))
                self.assertEqual(kept, [{'state': 'recorded', 'after': {'grids': {}}, 'commands': [], 'scenario': 's'}], name)


class ComparisonUnchangedTests(unittest.TestCase):
    """Replay: the same pairs of records, with and without the new fields, give the same differences, replies and changes."""

    def record(self, blocks, reply):
        return {'state': 'recorded', 'replies': [reply],
                'before': {'grids': {'g1': {'blocks': 10, 'owners': ['a']}}},
                'after': {'grids': {'g1': {'blocks': blocks, 'owners': ['a']}, 'g2': {'blocks': 3}}}}

    def with_timing(self, record, seed):
        out = copy.deepcopy(record)
        out['timing'] = {'waitedSeconds': seed, 'upSeconds': seed * 2, 'simSpeed': 1 / (seed + 1)}
        out['probeTiming'] = {'readySeconds': 80 + seed, 'ticks': 4000 + seed, 'simSpeed': 0.5 + seed / 10}
        return out

    def test_replayed_records_compare_the_same(self):
        f = comparison_functions()
        pairs = [(self.record(10, 'ok 12 ms'), self.record(9, 'ok 15 ms')), (self.record(7, 'done at 10:01'), self.record(7, 'done at 11:02')),
                 (self.record(10, 'same'), self.record(10, 'same'))]
        for a, b in pairs:
            ta, tb = self.with_timing(a, 1), self.with_timing(b, 7)
            self.assertEqual(f['disagreements'](a, b), f['disagreements'](ta, tb))
            self.assertEqual(f['differences'](a, b), f['differences'](ta, tb))
            self.assertEqual(f['replies_of'](a), f['replies_of'](ta))
            self.assertEqual(f['changes'](a), f['changes'](ta))
            self.assertEqual(f['touched'](b), f['touched'](tb))

    def test_timing_never_differs_two_runs_into_a_finding(self):
        f = comparison_functions()
        a = self.with_timing(self.record(10, 'x'), 1)
        b = self.with_timing(self.record(10, 'x'), 9)
        self.assertEqual(f['disagreements'](a, b), {})
        self.assertEqual(f['replies_of'](a), f['replies_of'](b))


class ProbeSourceTests(unittest.TestCase):
    def test_the_probe_adds_probe_timing_to_every_record(self):
        source = open(os.path.join(STAGES, 'behaviour', 'probe', 'src', 'Probe.cs'), encoding='utf-8').read()
        finish = source[source.index('private void Finish(string json)'):]
        finish = finish[:finish.index('MyLog.Default.WriteLineAndConsole("PROBE done")')]
        self.assertIn('"\\"probeTiming\\":{\\"readySeconds\\":"', finish)
        self.assertLess(finish.index('json = json.Length > 2'), finish.index('File.WriteAllText'))

    def test_the_probe_trims_the_record_before_splicing_into_it(self):
        # The splice drops the last character, so it must be the closing brace.
        source = open(os.path.join(STAGES, 'behaviour', 'probe', 'src', 'Probe.cs'), encoding='utf-8').read()
        finish = source[source.index('private void Finish(string json)'):]
        self.assertLess(finish.index('json = json.TrimEnd();'), finish.index('json = json.Length > 2'))


if __name__ == '__main__':
    unittest.main()
