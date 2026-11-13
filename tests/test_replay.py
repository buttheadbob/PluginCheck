"""Replays of the behaviour stage without Docker. Run with: python -I -m unittest discover -s tests

The replays run behaviour.py as it is, with Docker and the servers replaced (replay.py). To compare against another version of behaviour.py,
such as the one before a change, set PLUGINCHECK_REPLAY_BEFORE to its path: the old-against-new tests run only then.
"""
import glob, json, os, shutil, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import replay

SOURCE = os.path.join(replay.STAGES, 'behaviour.py')
BEFORE = os.environ.get('PLUGINCHECK_REPLAY_BEFORE')
RUNS = os.path.normpath(os.path.join(HERE, '..', 'runs'))
CACHE = os.path.normpath(os.path.join(HERE, '..', 'cache', 'behaviour'))


def command(line, *parameters):
    return {'line': line, 'parameters': [{'type': kind, 'optional': optional} for kind, optional in parameters]}


def synthetic_case(**changes):
    """Five builds and two variants, with commands that share servers, settings that flip, a read-only command, examples with callers,
    and a published Torch build that lacks a command and has one of its own."""
    common = [command('!demo info'), command('!demo pay', ('String', False), ('Int32', False)), command('!demo weather', ('String', False)),
              command('!demo clean', ('Boolean', True)), command('!demo reset')]
    settings = [{'name': 'Enabled', 'type': 'Boolean', 'default': 'True'}, {'name': 'Verbose', 'type': 'Boolean', 'default': 'False'}]
    discoveries = {name: {'commands': common, 'settings': settings} for name in ('working-magnetar', 'working-legacy', 'working-torch', 'published-magnetarhub')}
    # A setting only the published builds have, with different defaults: its flipped value comes from the first of them in build order.
    discoveries['published-magnetarhub'] = {'commands': common, 'settings': settings + [{'name': 'Legacy', 'type': 'Boolean', 'default': 'True'}]}
    discoveries['published-torch'] = {'commands': common[:3] + [command('!demo legacy')], 'settings': settings[:1] + [{'name': 'Legacy', 'type': 'Boolean', 'default': 'False'}]}
    for base in ('working-magnetar', 'working-legacy'):
        discoveries[base + '~fast'] = discoveries[base]
    reading = {'commands': {
        '!demo info': {'reads': ['grids'], 'writes': []},
        '!demo pay': {'reads': ['players'], 'writes': ['players'], 'settings': ['Enabled'], 'examples': [{'line': '!demo pay player-large 5', 'caller': 'admin'}, {'line': '!demo pay nobody 1', 'caller': 'player'}]},
        '!demo weather': {'reads': [], 'writes': ['weather'], 'examples': ['!demo weather rain']},
        '!demo clean': {'reads': ['grids'], 'writes': ['grids'], 'settings': ['Enabled', 'Verbose']},
        '!demo reset': {'reads': [], 'writes': ['*']},
        '!demo legacy': {'reads': [], 'writes': []}}}
    builds = [{'name': 'working-magnetar', 'host': 'magnetar', 'published': False, 'plugin': 'Demo', 'id': 'demo'},
              {'name': 'working-legacy', 'host': 'legacy', 'published': False, 'plugin': 'Demo', 'id': 'demo'},
              {'name': 'working-torch', 'host': 'torch', 'published': False, 'plugin': 'Demo', 'id': 'demo'},
              {'name': 'published-magnetarhub', 'host': 'magnetar', 'published': True, 'plugin': 'Demo', 'id': 'demo', 'version': 'abc', 'from': 'MagnetarHub'},
              {'name': 'published-torch', 'host': 'torch', 'published': True, 'plugin': 'Demo', 'id': 'demo', 'version': '1.0', 'from': 'torchapi.com'}]
    builds += [{**b, 'name': b['name'] + '~fast', 'variant': 'fast', 'only': ['!demo pay', 'idle']} for b in builds[:2]]
    case = {'target': {'name': 'Demo', 'repo': '.', 'magnetar': {}, 'torch': {}, 'behaviour': {'removeDuring': {'commands': ['!demo clean'], 'remove': [{'after': 3, 'kind': 'grid', 'name': 'g2'}]}}},
            'builds': builds, 'discoveries': discoveries, 'classification': reading, 'records': {}, 'seed': 1}
    case.update(changes)
    return case


def replay_of(source, case):
    return replay.Replay(source, dict(case), replay.folder()).run()


class StageTests(unittest.TestCase):
    """behaviour.py on its own, through the replay."""

    def test_results_do_not_depend_on_which_server_finishes_first(self):
        base = replay_of(SOURCE, synthetic_case()).result()
        for seed, delays in ((2, {'working-magnetar': 0.4}), (3, {'published-torch': 0.6, 'working-legacy': 0.2}), (4, {'published-magnetarhub': 0.5})):
            # With published-magnetarhub last, servers start with published-torch's Legacy value, and are left out once it is in.
            with self.subTest(seed=seed):
                self.assertEqual(replay_of(SOURCE, synthetic_case(seed=seed, discover_delay=delays)).result(), base)

    def test_a_working_build_left_out_starts_no_server_and_ends_as_before(self):
        r = replay_of(SOURCE, synthetic_case(fail_discovery={'working-torch': 'the plugin is not loaded'}, discover_delay={'published-torch': 0.5}))
        self.assertEqual(r.outcome.status, 'not run')
        self.assertTrue(r.outcome.fields['reason'].startswith('working-torch could not be discovered, so nothing was run: the plugin is not loaded'))
        self.assertEqual(r.server_started, [])
        self.assertEqual(set(r.outcome.fields['discovered']), {b['name'] for b in synthetic_case()['builds']})

    def test_no_build_discovered_fails_as_before(self):
        every = {b['name']: 'the plugin is not loaded' for b in synthetic_case()['builds']}
        r = replay_of(SOURCE, synthetic_case(fail_discovery=every))
        self.assertEqual((r.outcome.status, r.outcome.fields['reason']), ('fail', 'the plugin is not loaded'))
        self.assertEqual(r.server_started, [])

    def test_alone_reruns_still_happen(self):
        r = replay_of(SOURCE, synthetic_case())
        self.assertGreater(r.result()['fields']['aloneReruns'], 0)
        self.assertEqual(sum(1 for c in r.calls if '"readonly"' not in c[1] and '"command"' in c[1] and c[1].count('"name"') == 1), r.result()['fields']['aloneReruns'])




@unittest.skipUnless(BEFORE, 'set PLUGINCHECK_REPLAY_BEFORE to another behaviour.py to compare with')
class BeforeAndAfterTests(unittest.TestCase):
    """The same case through the other behaviour.py and this one: the same servers, results, records and replies."""

    def same(self, case, patterns):
        old = replay_of(BEFORE, case)
        expected = old.result()
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                new = replay_of(SOURCE, {**case, **pattern})
                self.assertEqual(new.result(), expected)
                self.assertEqual(sorted(new.calls), sorted(old.calls))
        return expected

    def test_discovery_failures(self):
        """The outcome when working builds can't be discovered: the old order of checks, every name, whatever arrives first."""
        names = [b['name'] for b in synthetic_case()['builds']]
        for failing, delays in (({'working-torch': 'the plugin is not loaded'}, {'published-torch': 0.3}),
                                ({n: 'the plugin is not loaded' for n in names[:3]}, {'working-torch': 0.2}),
                                ({n: 'the plugin is not loaded' for n in names[:3]}, {'working-magnetar': 0.3}),
                                ({n: 'the plugin is not loaded' for n in names}, {})):
            with self.subTest(failing=sorted(failing), delays=delays):
                case = synthetic_case(fail_discovery=failing)
                old, new = replay_of(BEFORE, case), replay_of(SOURCE, dict(case, discover_delay=delays))
                # A discovery record's timing is the replay's own clock, which nothing compares.
                plain = lambda f: {**f, 'discovered': {n: {k: v for k, v in r.items() if k != 'timing'} for n, r in f.get('discovered', {}).items()}}
                self.assertEqual((new.outcome.status, plain(new.outcome.fields)), (old.outcome.status, plain(old.outcome.fields)))
                self.assertEqual(new.calls, [])

    def test_synthetic(self):
        found = self.same(synthetic_case(), [{}, {'seed': 5, 'discover_delay': {'published-torch': 0.8}}, {'seed': 6, 'discover_delay': {'working-magnetar': 0.3}}])
        self.assertGreater(found['fields']['aloneReruns'], 0)
        self.assertTrue(found['fields']['differences'])

    def test_reused_results(self):
        """A second run reusing the first one's results. Each version reuses its own first run's cache, since a kept result's key may
        change between versions, and then the second runs must agree."""
        case = synthetic_case()
        second = {}
        for name, source in (('old', BEFORE), ('new', SOURCE)):
            first = replay_of(source, case)
            again = replay.Replay(source, dict(case, discover_delay={'published-torch': 0.5}), first.folder)
            second[name] = again.run().result()
        self.assertGreater(second['old']['fields']['reused'], 0)
        self.assertEqual(second['new'], second['old'])

    def real_case(self, target_name, stamp):
        folder = os.path.join(RUNS, target_name, stamp)
        if not os.path.exists(os.path.join(folder, 'behaviour-records.json')):
            self.skipTest(f'no recorded run {target_name} {stamp}')
        line = next(l for l in open(os.path.join(folder, 'behaviour.log'), encoding='utf-8') if l.startswith('builds: '))
        names = [part.strip().split(' (')[0] for part in line[len('builds: '):].split(', ')]
        target = json.load(open(os.path.join(HERE, '..', 'targets', f'{target_name}.json'), encoding='utf-8'))
        options = {k: v for k, v in target.get('behaviour', {}).items() if k not in ('variants', 'leaveOut', 'plugins')}
        reading_path = os.path.join(CACHE, f'commands-{target_name}.json')
        reading = json.load(open(reading_path, encoding='utf-8')) if os.path.exists(reading_path) else None
        case = replay.from_run(folder, {'name': target_name, 'repo': '.', 'magnetar': {}, 'torch': {}, 'behaviour': options}, [], reading)
        # Each build's newest kept discovery, and a variant's from its base build when it has none of its own.
        discoveries = {}
        for path in sorted(glob.glob(os.path.join(CACHE, 'results', '*.json')), key=os.path.getmtime):
            try:
                kept = json.load(open(path, encoding='utf-8'))
            except ValueError:
                continue
            if len(kept) == 1 and isinstance(kept[0], dict) and kept[0].get('scenario') == 'discover' and 'commands' in kept[0] and kept[0].get('build') in names:
                discoveries[kept[0]['build']] = kept[0]
        builds = []
        for name in names:
            base = name.split('~')[0]
            host = 'torch' if 'torch' in base else 'legacy' if 'legacy' in base else 'magnetar'
            build = {'name': name, 'host': host, 'published': base.startswith('published'), 'plugin': target_name, 'id': target_name, 'version': 'v', 'from': 'hub'}
            if '~' in name:
                scenarios = sorted({s for (b, s) in case['records'] if b == name})
                build.update({'variant': name.split('~')[1], 'only': ['=' + s for s in scenarios]})
            if name not in discoveries and base not in discoveries:
                self.skipTest(f'no kept discovery for {name}')
            builds.append(build)
            case.setdefault('discoveries', {})[name] = discoveries.get(name) or discoveries[base]
        case['builds'] = builds
        case['job_scale'] = 0.00002
        return case

    def test_recorded_advanced_pb_limiter(self):
        case = self.real_case('AdvancedPBLimiter', '20261108-184023')
        found = self.same(case, [{'seed': 7, 'discover_delay': {'published-torch': 1.0}}])
        print(f"\nAdvancedPBLimiter replay: {found['fields']['servers']} servers, {found['fields']['aloneReruns']} alone, {len(found['fields']['differences'])} differences, "
              f"{len(found['fields']['scenarios'])} scenarios, {sum(1 for p in found['fields']['scenarios'] if any(s == p for (_, s) in case['records']))} with recorded records", file=sys.stderr)

    def test_recorded_monitor(self):
        case = self.real_case('Monitor', '20261108-054630')
        found = self.same(case, [{'seed': 8, 'discover_delay': {'working-legacy': 0.5}}])
        print(f"\nMonitor replay: {found['fields']['servers']} servers, {found['fields']['aloneReruns']} alone, {len(found['fields']['differences'])} differences, "
              f"{len(found['fields']['scenarios'])} scenarios, {sum(1 for p in found['fields']['scenarios'] if any(s == p for (_, s) in case['records']))} with recorded records", file=sys.stderr)


if __name__ == '__main__':
    unittest.main()
