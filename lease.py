"""Borrow PluginCheck's server slots for in-game work: mod, Torch and similar fixes. Never for checking another plugin.

usage:
  python lease.py request   --session NAME [--count 1|2] [--purpose TEXT] [--exclusive] [--log PATH]
  python lease.py heartbeat --session NAME [--log PATH]
  python lease.py release   --session NAME [--cores 4-5]
  python lease.py status
  python lease.py gate      --purpose TEXT [--timeout SECONDS] [--session NAME]

request asks for slots and prints the leases this session holds, as JSON. A slot is granted when one of PluginCheck's servers
finishes, or at once when no PluginCheck run is going, so call request or heartbeat again until the count is reached. Then run
each container pinned to its lease: docker run --cpuset-cpus <cores> --name <container> ... The container name is
<session>-<n>, never plugincheck-*. Call heartbeat at least every minute while working: a lease with none for 5 minutes ends.
release when done. A session gets at most 2 slots, and at most 3 sessions borrow at once.

--exclusive asks for the whole machine, for measurements that need it quiet. PluginCheck starts no new server and no new Docker
stage of a full run (each finishes the step it is on), nothing new is lent, and the lease (cores "all") is granted once nothing
else runs: no server, no stage, no lease, and no running container the lease file does not know. Release it when done. Repeat the
request about every 30 seconds until the reply shows the lease, and send a heartbeat every minute while holding it.

Every reply carries exclusiveWaiting (an exclusive request waits: finish the container you are on, release, and ask again after
it is done), exclusiveHeld, and, while one waits, waitingFor: what the drain still waits for. status adds unknownContainers: the
running containers the lease file does not know, or null when Docker could not be asked.

gate is for work that holds no lease: a session's build, test suite or live run calls it first. It waits while an exclusive request
waits or holds, then exits 0. With --timeout it exits 3 once that many seconds pass with the gate still shut (0: look once), and
names who claims the machine and since when. --session leaves that session's own claim out. It prints its waiting lines to stderr
and one JSON line to stdout, starts nothing and changes nothing. Python callers import this file and call gate(purpose, timeout=...),
which raises GateTimeout instead.
"""
import argparse, json, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stages'))
import leases

gate, GateTimeout = leases.gate, leases.GateTimeout


def cores_of_machine():
    # The same slots the behaviour stage makes.
    return leases.machine_cores()


def show(state, session=None, containers=leases.NOT_CHECKED, now=None):
    now = time.time() if now is None else now
    waiting = any(r.get('exclusive') for r in state['requests'])
    shown = {'leases': [l for l in state['leases'] if session is None or l['session'] == session],
             'waiting': [r for r in state['requests'] if session is None or r['session'] == session],
             'orphans': state['orphans'] if session is None else [o for o in state['orphans'] if o['session'] == session],
             'pluginCheckRunning': leases.owner_active(state, now),
             'exclusiveWaiting': waiting,
             'exclusiveHeld': any(l.get('exclusive') for l in state['leases'])}
    if waiting:
        running = (state.get('owner') or {}).get('servers', 0) if leases.owner_active(state, now) else 0
        shown['waitingFor'] = leases.exclusive_blockers(state, now, running, containers)
    if session is None:
        shown['stages'] = leases.run_stages(state, now)
        if containers is not leases.NOT_CHECKED:
            shown['unknownContainers'] = leases.unknown_containers(state, containers)
    return shown


def run_gate(purpose, timeout, session):
    say = lambda text: print(text, file=sys.stderr, flush=True)
    try:
        waited = gate(purpose, timeout, session, say=say)
    except GateTimeout as shut:
        say(str(shut))
        print(json.dumps({'gate': 'shut', 'purpose': purpose, 'claims': shut.claims}))
        return 3
    print(json.dumps({'gate': 'free', 'purpose': purpose, 'waitedSeconds': round(waited)}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=['request', 'heartbeat', 'release', 'status', 'gate'])
    parser.add_argument('--session')
    parser.add_argument('--count', type=int, default=1, choices=[1, 2])
    parser.add_argument('--purpose', default='')
    parser.add_argument('--exclusive', action='store_true')
    parser.add_argument('--cores')
    parser.add_argument('--log', help='a file this session writes for its work, which a viewer may show')
    parser.add_argument('--timeout', type=float, help='gate: give up after this many seconds (0: look once)')
    args = parser.parse_args()
    if args.action == 'gate':
        if not args.purpose:
            parser.error('gate needs --purpose: what waits, such as "Inertia build"')
        if args.session and not leases.SESSION.match(args.session):
            parser.error('--session: 1 to 64 letters, digits, "_", "." or "-", starting with a letter or digit')
        return run_gate(args.purpose, args.timeout, args.session)
    if args.action != 'status' and not (args.session and leases.SESSION.match(args.session)):
        parser.error('--session is required: 1 to 64 letters, digits, "_", "." or "-", starting with a letter or digit')
    if args.session == leases.LANE:
        parser.error(f"{leases.LANE} is PluginCheck's own pre-container lane, which check.py --pre runs")
    names = leases.docker_names()  # read before the lock: nothing slow runs under it
    running_now = leases.running_check(names)
    with leases.Store() as state:
        now = time.time()
        leases.expire(state, now, running_now)
        if args.action == 'request':
            leases.request(state, args.session, args.count, now, args.exclusive, args.purpose, args.log)
        elif args.action == 'heartbeat':
            leases.heartbeat(state, args.session, now, args.log)
        elif args.action == 'release':
            leases.release(state, args.session, args.cores)
        leases.grant_when_idle(state, now, cores_of_machine(), names)
        print(json.dumps(show(state, args.session if args.action != 'status' else None, names, now), indent=1))


if __name__ == '__main__':
    sys.exit(main())
