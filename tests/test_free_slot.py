"""The slot goes back at kill, and the wait asks Docker less. Run with: python -I -m unittest discover -s tests"""
import ast, itertools, json, os, re, shutil, sys, tempfile, threading, time, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import replay
import startlog

SOURCE = os.path.join(replay.STAGES, 'behaviour.py')


class PoolTests(unittest.TestCase):
    def test_spare_threads_start_the_next_server_and_the_slots_still_limit(self):
        """A thread whose server was killed is still cleaning up, so the main and alone pools have spare threads. They wait in run_server
        for a slot (slots.get), so the slots still decide how many servers run."""
        source = open(SOURCE, encoding='utf-8').read()
        self.assertIn('pool_threads = workers + max(2, workers // 4)', source)
        self.assertEqual(source.count('ThreadPoolExecutor(pool_threads)'), 2)
        run_server = source[source.index('def run_server('):source.index('def serve(')]
        self.assertLess(run_server.index('slots.get()'), run_server.index('serve(build, batch, save, probe_zip, cores, timing, free)'))


class ReplayTests(unittest.TestCase):
    def test_a_server_starts_while_another_is_cleaned_up(self):
        import test_replay
        case = test_replay.synthetic_case(workers=2, cleanup=0.05, job_scale=0.0005)
        r = replay.Replay(SOURCE, case, replay.folder()).run()
        self.assertTrue(r.overlapped)
        self.assertLessEqual(r.most_running, 2)
        self.assertEqual(r.result()['status'], 'review')


class RunningTests(unittest.TestCase):
    """run_server lifted: a server counts as running through its clean-up, so an exclusive lease waits for it."""

    def test_running_until_cleaned_up(self):
        import queue
        tree = ast.parse(open(SOURCE, encoding='utf-8').read())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_server']
        seen = {}
        slots = queue.Queue()
        slots.put('0-1')

        def serve(build, batch, save, probe_zip, cores, timing, free):
            free()
            seen['after free'] = running[0]
            seen['slot back'] = slots.qsize()
            return [{'scenario': 's'}]
        running = [0]
        lending = types.SimpleNamespace(may_start=lambda: True, free=lambda cores: None, note=lambda *a: None)
        namespace = {'time': time, 'lending': lending, 'slots': slots, 'running': running, 'running_lock': threading.Lock(), 'status': lambda **f: None,
                     'serve': serve, 'server_timing': types.SimpleNamespace(summary=lambda t, p: (None, {}))}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), SOURCE, 'exec'), namespace)
        namespace['run_server']({'name': 'b'}, [{'name': 's'}], None, None)
        self.assertEqual(seen, {'after free': 1, 'slot back': 1})
        self.assertEqual(running[0], 0)


class WaitTests(unittest.TestCase):
    """serve() lifted out of behaviour.py, with Docker and the server start replaced."""

    def serve(self, host, answers, marker_after=None, kill_fails=False):
        tree = ast.parse(open(SOURCE, encoding='utf-8').read())
        wanted = {'serve', 'write', 'read_text', 'container_log'}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
        work = tempfile.mkdtemp(prefix='pc-wait-')
        calls, ended = [], []

        def docker(*args):
            calls.append(args)
            out = types.SimpleNamespace(returncode=0, stdout='', stderr='')
            if args[0] == 'exec':
                said = answers.pop(0) if answers else ''
                if said == 'ENDED':
                    ended.append(True)
                    said = ''
                out.stdout, out.returncode = said, 0 if said else 1
            elif args[0] == 'inspect':
                out.stdout = 'false' if ended else 'true'
            elif args[0] == 'kill' and kill_fails:
                out.returncode = 1
            elif args[0] == 'cp' and args[1].endswith('/probe.json'):
                with open(args[2], 'w', encoding='utf-8') as f:
                    json.dump({'after': {'grids': {}}}, f)
            return out

        def start(*args):
            folder = args[2] if host != 'legacy' else args[1]
            if marker_after is not None:
                def later():
                    time.sleep(marker_after)
                    with open(os.path.join(folder, 'marker', 'probe.json'), 'w', encoding='utf-8') as f:
                        f.write('{}')
                threading.Thread(target=later, daemon=True).start()

        namespace = {'os': os, 'shutil': shutil, 'json': json, 're': re, 'glob': __import__('glob'), 'time': time, 'docker': docker,
                     'counter': itertools.count(1), 'work': work, 'scenario_file': lambda build, batch: 'mode=run\n', 'START_TIMEOUT': 20,
                     'CHECK_SECONDS': 1, 'startlog': startlog, 'remember_magnetar_cache': lambda folder: None,
                     'seed_magnetar': lambda *a: None, 'seed_torch': lambda *a: None, 'start_magnetar': start, 'start_legacy': start,
                     'start_torch': start}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), SOURCE, 'exec'), namespace)
        save = os.path.join(work, 'save')
        os.makedirs(save)
        freed = []
        timing = {}
        build = {'name': f'working-{host}', 'host': host, 'plugin': 'Demo'}
        records = namespace['serve'](build, [{'name': 's', 'command': '!demo'}], save, None, '0-1', timing, lambda: freed.append(len(calls)))
        return records, calls, freed, timing

    def test_the_marker_is_seen_without_asking_docker_again(self):
        records, calls, freed, timing = self.serve('magnetar', [], marker_after=0.3)
        self.assertEqual(records[0]['state'], 'recorded')
        self.assertLessEqual(sum(1 for c in calls if c[0] in ('exec', 'inspect')), 2)
        self.assertLess(timing['stopped'] - timing['up'], 0.95)

    def test_the_slot_goes_back_right_after_the_kill(self):
        records, calls, freed, timing = self.serve('magnetar', [], marker_after=0.1)
        kill = next(i for i, c in enumerate(calls) if c[0] == 'kill')
        self.assertEqual(freed, [kill + 1])
        self.assertTrue(any(c[0] == 'cp' for c in calls[kill + 1:]))
        self.assertLess(timing['freed'] if 'freed' in timing else timing['killed'], timing['cleaned'])

    def test_a_container_still_running_after_its_kill_keeps_its_cores(self):
        records, calls, freed, timing = self.serve('magnetar', [], marker_after=0.1, kill_fails=True)
        self.assertEqual(freed, [], 'the cores went back while the container still ran')
        self.assertEqual(records[0]['state'], 'recorded')

    def test_a_kill_of_a_container_that_already_ended_still_frees_at_once(self):
        records, calls, freed, timing = self.serve('legacy', ['', 'ENDED'], kill_fails=True)
        kill = next(i for i, c in enumerate(calls) if c[0] == 'kill')
        self.assertEqual(freed, [kill + 2])

    def test_docker_finds_the_record_when_the_copy_does_not_come(self):
        records, calls, _, _ = self.serve('magnetar', ['', 'recorded'])
        self.assertEqual(records[0]['state'], 'recorded')
        self.assertEqual(sum(1 for c in calls if c[0] == 'exec'), 2)
        self.assertIn('test -f /data/Instance/probe.json', calls[0][-1])

    def test_a_torch_fatal_error(self):
        records, calls, _, _ = self.serve('torch', ['fatal'])
        self.assertEqual(records[0]['state'], 'fatal')
        self.assertIn('grep -qF "[FATAL]" /run-data/Logs/Torch-*.log', calls[0][-1])
        self.assertIn('test -f /run-data/Instance/probe.json', calls[0][-1])

    def test_a_server_that_ended(self):
        records, calls, _, _ = self.serve('legacy', ['', 'ENDED'])
        self.assertEqual(records[0]['state'], 'exited')
        self.assertEqual([c[0] for c in calls[:4]], ['exec', 'inspect', 'exec', 'inspect'])

    def test_the_marker_folder_is_mounted_where_the_probe_looks(self):
        source = open(SOURCE, encoding='utf-8').read()
        # Beside the instance folder, never inside it: a mount there makes Docker create the instance folder as root, and Torch and Legacy
        # run as a user, so their servers could not write their own instance.
        for function, target in (('start_magnetar', '/data/marker'), ('start_legacy', '/data/marker'), ('start_torch', '/run-data/marker')):
            body = source[source.index(f'def {function}('):]
            body = body[:body.index('\ndef ', 1)]
            self.assertIn(f'{{os.path.join(folder, "marker")}}:{target}\'', body, function)
            self.assertNotIn('Instance/marker', body, function)
        probe = open(os.path.join(replay.STAGES, 'behaviour', 'probe', 'src', 'Probe.cs'), encoding='utf-8').read()
        finish = probe[probe.index('private void Finish(string json)'):]
        finish = finish[:finish.index('MyLog.Default.WriteLineAndConsole("PROBE done")')]
        self.assertLess(finish.index('"probe.json"), json);'), finish.index('Path.GetDirectoryName(MyFileSystem.UserDataPath'))
        self.assertIn('"marker");', finish)
        self.assertIn('catch (Exception e)', finish)


if __name__ == '__main__':
    unittest.main()
