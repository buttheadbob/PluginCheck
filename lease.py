"""Borrow PluginCheck's server slots for in-game work: mod, Torch and similar fixes. Never for checking another plugin.

usage:
  python lease.py request   --session NAME [--count 1|2] [--purpose TEXT] [--exclusive] [--log PATH]
  python lease.py heartbeat --session NAME [--log PATH]
  python lease.py release   --session NAME [--cores 4-5]
  python lease.py status

request asks for slots and prints the leases this session holds, as JSON. A slot is granted when one of PluginCheck's servers
finishes, or at once when no PluginCheck run is going, so call request or heartbeat again until the count is reached. Then run
each container pinned to its lease: docker run --cpuset-cpus <cores> --name <container> ... The container name is
<session>-<n>, never plugincheck-*. Call heartbeat at least every minute while working: a lease with none for 5 minutes ends.
release when done. A session gets at most 2 slots, and at most 3 sessions borrow at once.

--exclusive asks for the whole machine, for measurements that need it quiet. PluginCheck starts no new server, nothing new is
lent, and the lease (cores "all") is granted once nothing else runs. Release it when done.
"""
import argparse, json, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stages'))
import leases


def cores_of_machine():
    # The same slots the behaviour stage makes.
    return leases.machine_cores()


def show(state, session=None):
    return {'leases': [l for l in state['leases'] if session is None or l['session'] == session],
            'waiting': [r for r in state['requests'] if session is None or r['session'] == session],
            'orphans': state['orphans'] if session is None else [o for o in state['orphans'] if o['session'] == session],
            'pluginCheckRunning': leases.owner_active(state, time.time())}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=['request', 'heartbeat', 'release', 'status'])
    parser.add_argument('--session')
    parser.add_argument('--count', type=int, default=1, choices=[1, 2])
    parser.add_argument('--purpose', default='')
    parser.add_argument('--exclusive', action='store_true')
    parser.add_argument('--cores')
    parser.add_argument('--log', help='a file this session writes for its work, which a viewer may show')
    args = parser.parse_args()
    if args.action != 'status' and not (args.session and leases.SESSION.match(args.session)):
        parser.error('--session is required: 1 to 64 letters, digits, "_", "." or "-", starting with a letter or digit')
    if args.session == leases.LANE:
        parser.error(f"{leases.LANE} is PluginCheck's own pre-container lane, which check.py --pre runs")
    running_now = leases.running_check(leases.docker_names())  # read before the lock: nothing slow runs under it
    with leases.Store() as state:
        now = time.time()
        leases.expire(state, now, running_now)
        if args.action == 'request':
            leases.request(state, args.session, args.count, now, args.exclusive, args.purpose, args.log)
        elif args.action == 'heartbeat':
            leases.heartbeat(state, args.session, now, args.log)
        elif args.action == 'release':
            leases.release(state, args.session, args.cores)
        leases.grant_when_idle(state, now, cores_of_machine())
        print(json.dumps(show(state, args.session if args.action != 'status' else None), indent=1))


if __name__ == '__main__':
    main()
