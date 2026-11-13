"""Replays a behaviour run without Docker: behaviour.py runs as it is, with only the parts that reach outside replaced.

Replaced: docker, capacity, the world and probe builds, the builds and their hashes, the server itself (serve), finish, slot lending and
GitHub. Everything that decides what runs and what is compared is behaviour.py's own: discovery, generate, the batches, the results cache,
run_server, the retries, take, compare and the alone reruns. A server's records come from a case: recorded records where it has them, made
up ones where it does not, always the same for the same build, scenario, repeat and batch. So two versions of behaviour.py fed the same case
can be compared result for result.
"""
import ast, atexit, contextlib, hashlib, io, json, os, queue, random, shutil, sys, tempfile, threading, time, types

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = os.path.normpath(os.path.join(HERE, '..', 'stages'))
for path in (os.path.join(STAGES, 'behaviour'), STAGES):
    if path not in sys.path:
        sys.path.insert(0, path)

REPLACED = ('docker', 'capacity', 'copy_world_files', 'working_builds', 'published_builds', 'build_torch_probe', 'game_hash', 'tool_hash',
            'build_hash', 'serve', 'finish')


_root = []


def folder():
    """A new empty folder for one replay, under one folder per test process that is removed when the process ends."""
    if not _root:
        _root.append(tempfile.mkdtemp(prefix='pc-replay-'))
        atexit.register(shutil.rmtree, _root[0], True)
    return tempfile.mkdtemp(dir=_root[0])


class Finished(Exception):
    def __init__(self, status, fields):
        super().__init__(status)
        self.status, self.fields = status, fields


def stable(*parts):
    return int(hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:8], 16)


def fake_leases():
    module = types.ModuleType('leases')

    class Lending:
        lent_at_start = []

        def __init__(self, *args, **kwargs):
            pass

        def may_start(self):
            return True

        def free(self, cores):
            return None

        def beat(self, running):
            return []

        def note(self, *args):
            pass

        def stop(self):
            pass

    module.Lending = Lending
    module.Owner = lambda *args: None
    return module


def fake_classification(reading):
    import classification as real
    module = types.ModuleType('classification')
    module.__dict__.update({k: v for k, v in real.__dict__.items() if not k.startswith('__')})
    commands = (reading or {}).get('commands', {})
    module.load = lambda name, repo: None if reading is None else {line.lower(): (set(e.get('reads', [])), set(e.get('writes', ['*']))) for line, e in commands.items()}
    module.settings_read = lambda name, repo: {line.lower(): set(e['settings']) for line, e in commands.items() if 'settings' in e}
    module.examples = lambda name, repo: {c.lower(): [{'line': x} if isinstance(x, str) else x for x in e.get('examples', [])] for c, e in commands.items() if e.get('examples')}
    return module


class Replay:
    """One run of one behaviour.py source over one case."""

    def __init__(self, source, case, folder):
        self.source, self.case, self.folder = source, case, folder
        self.calls, self.lock = [], threading.Lock()
        self.discovered_at, self.server_started = {}, []
        self.random = random.Random(case.get('seed', 0))
        self.namespace = {}

    # ---- what replaces the outside world ----

    def docker(self, *args):
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')

    def capacity(self):
        workers = self.case.get('workers', 16)
        slots = queue.Queue()
        for i in range(workers):
            slots.put(f'{2 * i}-{2 * i + 1}')
        return workers, slots

    def record_for(self, build, plan, repeat, alone):
        name = plan.get('name', 'discover')
        recorded = self.case['records'].get((build['name'], name)) or self.case['records'].get((build['name'].split('~')[0], name))
        if recorded:
            record = json.loads(json.dumps(recorded[repeat % len(recorded)]))
        else:
            h = stable(build['name'].split('~')[0] if not build['published'] else build['name'], name, repeat)
            before = {'grids': {'g1': {'blocks': 10, 'owners': ['a']}, 'g2': {'blocks': 4}}}
            after = json.loads(json.dumps(before))
            if h % 4 == 0:
                after['grids']['g1']['blocks'] = 9
            if h % 7 == 0:
                after['grids']['g3'] = {'blocks': 1}
            record = {'state': 'recorded', 'seconds': 60 + h % 120, 'before': before, 'after': after, 'replies': [f'reply {h % 3}']}
        for key in ('timing', 'probeTiming', 'reused', 'sharedAfter', 'wrongArguments'):
            record.pop(key, None)
        if alone and stable('alone', build['name'], name, repeat) % 3 == 0 and 'before' in record:
            record['after'] = json.loads(json.dumps(record['before']))
        record['build'] = build['name']
        return record

    def serve(self, build, batch, save, probe_zip, cores, timing=None):
        # A behaviour.py from before its timing points passes no timing dict.
        timing = {} if timing is None else timing
        timing['server'] = f"{build['name']}"
        repeat = self.case['_repeat'].value if hasattr(self.case['_repeat'], 'value') else 0
        discovery = batch[0].get('mode') == 'discover'
        if discovery:
            time.sleep(self.case.get('discover_delay', {}).get(build['name'], 0.01))
            found = json.loads(json.dumps(self.case['discoveries'][build['name']]))
            found.update({'build': build['name'], 'state': 'recorded', 'seconds': 30, 'blocked': None, 'log': [], 'scenario': 'discover'})
            reason = self.case.get('fail_discovery', {}).get(build['name'])
            if reason:
                found['failed'] = reason
            with self.lock:
                self.discovered_at[build['name']] = time.monotonic()
            return [found]
        with self.lock:
            self.server_started.append((time.monotonic(), build['name']))
        alone = len(batch) == 1 and batch[0].get('command') and 'readonly' not in batch[0]
        records = [self.record_for(build, plan, repeat, alone) for plan in batch]
        time.sleep(self.case.get('job_scale', 0.0001) * max(r.get('seconds', 60) for r in records) * (0.5 + self.random.random()))
        timing['stopped'], timing['state'] = time.time(), 'recorded'
        return [{**r, 'scenario': plan['name'], 'sharedAfter': position} for position, (plan, r) in enumerate(zip(batch, records))]

    def finish(self, status, **fields):
        raise Finished(status, fields)

    # ---- running it ----

    def run(self):
        os.makedirs(os.path.join(self.folder, 'stages'), exist_ok=True)
        run_dir = os.path.join(self.folder, 'runs', self.case['target']['name'], '20990101-000000')
        os.makedirs(run_dir, exist_ok=True)
        target_path = os.path.join(self.folder, 'target.json')
        with open(target_path, 'w', encoding='utf-8') as f:
            json.dump(self.case['target'], f)
        self.case['_repeat'] = threading.local()

        tree = ast.parse(open(self.source, encoding='utf-8').read())
        body = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in REPLACED:
                body.append(ast.parse(f"{node.name} = __replay__.{node.name}").body[0])
            elif isinstance(node, ast.FunctionDef) and node.name == 'run_and_remember':
                node.name = '_run_and_remember'
                body.append(node)
                body.append(ast.parse('def run_and_remember(build, batch, repeat, queued=None):\n'
                                      '    return __replay__.remember(_run_and_remember, build, batch, repeat, queued)').body[0])
            else:
                body.append(node)
        code = compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), self.source, 'exec')

        builds = self.case['builds']
        stubs = {'docker': self.docker, 'capacity': self.capacity, 'copy_world_files': lambda: os.path.join(self.folder, 'world'),
                 'working_builds': lambda: [dict(b) for b in builds], 'published_builds': lambda: [],
                 'build_torch_probe': lambda: ('probe.zip', None), 'game_hash': lambda host: 'game-' + host, 'tool_hash': lambda: 'tools',
                 'build_hash': lambda build: 'hash-' + build['name'], 'serve': self.serve, 'finish': self.finish}
        replay = types.SimpleNamespace(**stubs, remember=self.remember)
        namespace = self.namespace
        namespace.update({'__name__': 'behaviour_replay', '__file__': os.path.join(self.folder, 'stages', 'behaviour.py'), '__replay__': replay})
        saved_modules = {k: sys.modules.get(k) for k in ('leases', 'classification', 'githubtoken')}
        saved_argv, saved_path = sys.argv, list(sys.path)
        sys.modules['leases'] = fake_leases()
        sys.modules['classification'] = fake_classification(self.case.get('classification'))
        sys.modules['githubtoken'] = types.SimpleNamespace(headers=lambda url: {})
        sys.argv = ['behaviour.py', target_path, run_dir, os.path.join(run_dir, 'behaviour.json')]
        log = open(os.path.join(run_dir, 'behaviour.log'), 'w', encoding='utf-8')
        self.outcome = None
        try:
            with contextlib.redirect_stdout(log):
                try:
                    exec(code, namespace)
                except Finished as done:
                    self.outcome = done
        finally:
            log.close()
            sys.argv, sys.path[:] = saved_argv, saved_path
            for name, module in saved_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
        with open(os.path.join(run_dir, 'behaviour.log'), encoding='utf-8') as f:
            self.log = f.read()
        records = os.path.join(run_dir, 'behaviour-records.json')
        self.records = None
        if os.path.exists(records):
            with open(records, encoding='utf-8') as f:
                self.records = json.load(f)
        replies = os.path.join(run_dir, 'behaviour-replies.txt')
        self.replies = None
        if os.path.exists(replies):
            with open(replies, encoding='utf-8') as f:
                self.replies = f.read()
        return self

    def remember(self, real, build, batch, repeat, queued):
        with self.lock:
            self.calls.append((build['name'], json.dumps(batch, sort_keys=True), repeat))
        self.case['_repeat'].value = repeat
        # A behaviour.py from before its timing points has no queued time.
        return real(build, batch, repeat) if queued is None else real(build, batch, repeat, queued)

    # ---- what is compared ----

    def result(self):
        """The outcome, with every list whose order only follows which server finished first put in order."""
        if self.outcome is None:
            raise AssertionError('the run did not finish:\n' + self.log[-3000:])
        f = dict(self.outcome.fields)
        key = lambda item: json.dumps(item, sort_keys=True, default=str)
        for name in ('failures', 'differences', 'unstable', 'noChange', 'skipped'):
            if name in f:
                f[name] = sorted(f[name], key=key)
        for name in ('notHandled',):
            if name in f:
                f[name] = {k: sorted(v) for k, v in f[name].items()}
        for name in ('wronglyClassified', 'wrongArguments'):
            if name in f:
                # Which build's record set the value follows the order servers finish in, in the old orchestration as well. The scenarios do not.
                f[name] = sorted(f[name])
        f.pop('discovered', None)
        # A record's timing is this replay's own clock, which no comparison reads.
        records = sorted(json.dumps({k: v for k, v in r.items() if k != 'timing'}, sort_keys=True) for r in self.records) if self.records is not None else None
        return {'status': self.outcome.status, 'fields': f, 'records': records, 'replies': self.replies}


def from_run(run_folder, target, builds, reading):
    """A case from a real run's records: what each build recorded for each scenario, in the order the file holds them."""
    records = {}
    for record in json.load(open(os.path.join(run_folder, 'behaviour-records.json'), encoding='utf-8')):
        records.setdefault((record['build'], record['scenario']), []).append(record)
    return {'target': target, 'builds': builds, 'classification': reading, 'records': records}
