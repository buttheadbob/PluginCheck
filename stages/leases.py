"""Slot lending: other sessions borrow PluginCheck's server slots for in-game work, such as mod or Torch fixes, never
other plugin checks.

PluginCheck's behaviour stage runs its servers in slots of two logical cores each, made from every core but the user's own
(USER_RESERVED_CORES), which no slot ever holds. A borrower asks for up to MAX_PER_SESSION slots, a bench session (BENCH_SESSIONS)
for up to BENCH_MAX_PER_SESSION adjacent ones, and at most MAX_SESSIONS sessions of either kind borrow at once. While a run is going,
borrowers together get no slot past all but OWN_FLOOR, so the run keeps at least that many; with none going they may take every slot,
and a run that then starts uses what is left and takes slots back as leases end. PluginCheck hands a slot over only
when one of its own servers finishes there: it never stops a running server. When no PluginCheck run is going, a request is
granted at once from the free slots. A borrower keeps its leases alive with a heartbeat; a lease with none for HEARTBEAT_LIMIT
seconds returns, but a slot whose borrowed container still runs is not reused until it stops. PluginCheck never stops another
session's container.

An exclusive request, for measurements that need a quiet machine, drains everything: no new grants, no new PluginCheck
servers, and it is granted once nothing else runs. "Nothing else" is checked three ways: no PluginCheck server (the behaviour
stage's owner), no stage of a full run (RunHold: each Docker-using stage of check.py waits at its start while an exclusive request
waits or holds, so every stage boundary is a hold point), and no running container the lease file does not know
(unknown_containers). exclusive_blockers says what the drain still waits for.

PluginCheck's own pre-container lane (check.py --pre) takes one slot as the session LANE. It counts against neither borrower limit,
so with 3 sessions borrowing 2 slots each a run on 12 slots keeps 12 - 6 - 1 = 5; for OWN_FLOOR it is PluginCheck's own. It is
granted like any lease, gives its slot back when it ends, and steps aside for an exclusive request (Lane).

Work that holds no lease, such as a session's build, test suite or live run, calls the machine gate first (gate, lease.py gate): it
waits while an exclusive request waits or holds, so a measurement can claim the machine at once and waits only for work already running,
and while a PluginCheck run is going. Runs themselves go one at a time from any copy: a second check.py waits for its turn (RunQueue).

Everything lives in one JSON file, changed under a lock, so any session can use it from the command line (lease.py) and
PluginCheck from Python. The decisions are plain functions of the state and the time, so they are tested without Docker.

While the behaviour stage holds the owner entry, it also names its run there for a viewer of the file, such as a machine view: the run
folder (run), what it is doing (stage) and its progress, the same numbers its live panel shows (OWNER_RUN_FIELDS). They go with the owner:
when it stops, and when a stale owner is found.
"""
import contextlib, fnmatch, json, os, re, threading, time, uuid

# A file in the checkout's root, kept out of git, naming the lease folder to share with other copies and sessions (machine_folder).
MACHINE_DIR_FILE = 'machine-dir.txt'
CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Folders a process must not use as its lease folder, separated by os.pathsep. The test guard sets it (tests/scratchmachine.py), so a process
# a test starts is refused too, whatever environment it was given.
FORBIDDEN_ENV = 'PLUGINCHECK_MACHINE_DIR_FORBIDDEN'


def same_folder(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def machine_folder(environ=os.environ, checkout=CHECKOUT):
    """The lease folder: PLUGINCHECK_MACHINE_DIR when set (blank counts as unset); else the one absolute path in the checkout's
    MACHINE_DIR_FILE; else the checkout's own machine folder. A file that is there but does not hold exactly one absolute path is refused
    with ValueError, never passed over: a copy meant to share the machine's folder would otherwise run unseen in a folder of its own. A
    folder FORBIDDEN_ENV lists is refused with ValueError too."""
    folder = (environ.get('PLUGINCHECK_MACHINE_DIR') or '').strip()
    if not folder:
        path = os.path.join(checkout, MACHINE_DIR_FILE)
        try:
            with open(path, encoding='utf-8-sig') as file:
                text = file.read()
        except FileNotFoundError:
            text = None
        if text is None:
            folder = os.path.join(checkout, 'machine')
        else:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if len(lines) != 1 or not os.path.isabs(lines[0]):
                raise ValueError(f'{path} must hold one line, the absolute path of the lease folder to use; it holds {text[:200]!r}. '
                                 f'Fix it or remove it (PLUGINCHECK_MACHINE_DIR, when set, is used instead).')
            folder = lines[0]
    for forbidden in filter(None, environ.get(FORBIDDEN_ENV, '').split(os.pathsep)):
        if same_folder(folder, forbidden):
            raise ValueError(f'the lease folder {folder} is refused here ({FORBIDDEN_ENV}); set PLUGINCHECK_MACHINE_DIR to another folder')
    return folder


# Every copy that names the same folder (PLUGINCHECK_MACHINE_DIR or MACHINE_DIR_FILE) sees the others' leases and runs. A lease folder that
# cannot be worked out stops whatever opens the lease file, with the reason on one line (Store), and nothing before it: check.py --help works.
try:
    FOLDER, FOLDER_ERROR = machine_folder(), None
except ValueError as error:
    FOLDER, FOLDER_ERROR = None, str(error)
MAX_PER_SESSION = 2
# Bench sessions and the others together.
MAX_SESSIONS = 3
# The user's own logical cores, which no slot holds: no PluginCheck server or container and no borrower runs on them. A range or a list,
# such as "24-31" or "8,9,12-15"; empty for none. PLUGINCHECK_USER_RESERVED_CORES overrides it. A slot with either core reserved is left out.
USER_RESERVED_CORES = '24-31'
# Sessions that measure (an fnmatch pattern, case sensitive; PLUGINCHECK_BENCH_SESSIONS overrides it, empty for none) may hold up to
# BENCH_MAX_PER_SESSION slots, always on adjacent cores, and at most BENCH_MAX_SESSIONS of them borrow at once, within MAX_SESSIONS.
BENCH_SESSIONS = 'physbench-*'
BENCH_MAX_PER_SESSION = 4
BENCH_MAX_SESSIONS = 3
# The slots a running PluginCheck keeps, its pre-container lane's included: while a run is going, borrowers get no slot past the rest.
# With no run going there is no floor.
OWN_FLOOR = 4
SLOT_CORES = 2
HEARTBEAT_LIMIT = 300
OWNER_LIMIT = 120
LOCK_STALE = 120
LOCK_REMOVE_SECONDS = 5
# How long the lane's last give-back is tried when it ends, before its lease is left to expire.
RELEASE_SECONDS = 10
SESSION = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
# PluginCheck's pre-container lane: at most one slot, outside MAX_SESSIONS and MAX_PER_SESSION. lease.py refuses the name.
LANE = 'plugincheck-pre'
# Containers that are not anyone's work and never stop, such as Docker's own build daemon: never "unknown". More prefixes in
# PLUGINCHECK_IGNORE_CONTAINERS, comma separated.
IGNORED_CONTAINERS = ('buildx_buildkit_',)
# The stage that has its own owner (behaviour: its servers count themselves). Every other Docker stage is a run's to count.
OWN_OWNER_STAGE = 'behaviour'
# "The running containers were not read": the exclusive grant then looks only at the lease file, as before.
NOT_CHECKED = object()
# What the owner entry says about its run while it runs: the run folder, the stage and the progress ({done, total, left, ...}).
OWNER_RUN_FIELDS = ('run', 'stage', 'progress')
# How long a stage change or progress step waits for the lock. The next heartbeat writes it anyway, so the stage never waits long on it.
REPORT_WAIT = 5


def docker_names():
    """The names of the running containers, read once before the lock is taken, so nothing slow runs under it. None when
    Docker can't be asked: then every borrowed container counts as running, and no slot is taken back from under one."""
    import subprocess
    try:
        found = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return set(found.stdout.split()) if found.returncode == 0 else None


def running_check(names):
    """container_running for expire(), from a docker_names() snapshot."""
    return (lambda name: True) if names is None else (lambda name: name in names)


def empty():
    return {'owner': None, 'requests': [], 'leases': [], 'orphans': [], 'runs': []}


# ---- Decisions: pure functions of a state and the time ----

def owner_active(state, now):
    owner = state.get('owner')
    return bool(owner and owner.get('running') and now - owner.get('heartbeat', 0) <= OWNER_LIMIT)


def drop_owner_run(owner):
    for field in OWNER_RUN_FIELDS:
        owner.pop(field, None)


# The reason a run folder's stage gets when its owner's process went without cleaning up (end_run_folder).
END_REASON = 'ended without cleaning up'
# check.py rewrites live.json every second while it runs: one older than this has no writer left.
LIVE_QUIET = 10


def end_run_folder(run, now):
    """Marks the run folder of an owner whose process is gone as ended, once, so a viewer does not show it running for ever: live.json
    finished, its running stages not run, no time left and no servers; and, when there is none, a results.json with every stage not run, the
    running ones for END_REASON. Only an absolute <yyyyMMdd-HHmmss> folder whose live.json says not finished and has not been rewritten for
    LIVE_QUIET seconds (check.py may still be going). Best effort: True when marked, False on anything else, never an error."""
    try:
        if not (isinstance(run, str) and os.path.isabs(run) and re.fullmatch(r'[0-9]{8}-[0-9]{6}', os.path.basename(run))):
            return False
        with open(os.path.join(run, 'live.json'), encoding='utf-8') as file:
            live = json.load(file)
        if live.get('finished') is not False or now - live.get('updated', now) < LIVE_QUIET:
            return False
        stages = live.get('stages') or []
        results = os.path.join(run, 'results.json')
        if not os.path.exists(results):
            started = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(live.get('started') or now))
            outcome = {s['name']: {'status': 'not run', **({'reason': END_REASON} if s.get('state') == 'running' else {})} for s in stages}
            with open(results, 'x', encoding='utf-8') as out:
                json.dump({'target': live.get('target', ''), 'started': started, 'stages': outcome}, out, indent=1)
        for stage in stages:
            if stage.get('state') == 'running':
                stage['state'] = 'not run'
        live.update(finished=True, timeLeftSeconds=0, servers=[], updated=int(now))
        temporary = os.path.join(run, 'live.json.tmp')
        with open(temporary, 'w', encoding='utf-8') as out:
            json.dump(live, out)
        replace(temporary, os.path.join(run, 'live.json'))
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def exclusive_wanted(state):
    return any(r.get('exclusive') for r in state['requests']) or any(l.get('exclusive') for l in state['leases'])


def held(state, session):
    return [l for l in state['leases'] if l['session'] == session]


# ---- Cores and slots ----

def parse_cores(text):
    """The logical cores in "24-31", "8,9,12-15" or "" (none), as a set. ValueError on anything else."""
    found = set()
    for part in (text or '').replace(' ', '').split(','):
        if not part:
            continue
        match = re.fullmatch(r'(\d+)(?:-(\d+))?', part)
        if not match or (match.group(2) is not None and int(match.group(2)) < int(match.group(1))):
            raise ValueError(f'reserved cores: "{part}" is not a core or a range such as 24-31')
        found.update(range(int(match.group(1)), int(match.group(2) or match.group(1)) + 1))
    return found


def reserved_cores():
    return parse_cores(os.environ.get('PLUGINCHECK_USER_RESERVED_CORES', USER_RESERVED_CORES))


def core_count():
    return int(os.environ.get('PLUGINCHECK_LEASE_CORES', os.cpu_count() or 2))


def make_slots(count, reserved=(), size=SLOT_CORES):
    """Slots of size adjacent cores from cores 0 to count - 1, lowest first. One that would hold a reserved core is left out."""
    return [f'{first}-{first + size - 1}' for first in range(0, count - size + 1, size)
            if not any(core in reserved for core in range(first, first + size))]


def cpuset(cores):
    """Logical cores as a cpuset, such as "0-7,16-23"."""
    ranges = []
    for core in sorted(set(cores)):
        if ranges and core == ranges[-1][1] + 1:
            ranges[-1][1] = core
        else:
            ranges.append([core, core])
    return ','.join(f'{a}-{b}' if a != b else f'{a}' for a, b in ranges)


def unreserved_cpuset(count=None):
    """The cores PluginCheck's containers may use, as a cpuset: every core but the user's. Empty when none of them is reserved."""
    count = core_count() if count is None else count
    reserved = reserved_cores()
    if not any(core < count for core in reserved):
        return ''
    return cpuset(core for core in range(count) if core not in reserved)


def bounds(slot):
    first, last = slot.split('-')
    return int(first), int(last)


def slot_cores(slots):
    return [core for slot in slots for core in range(bounds(slot)[0], bounds(slot)[1] + 1)]


def adjacent(a, b):
    return bounds(a)[1] + 1 == bounds(b)[0] or bounds(b)[1] + 1 == bounds(a)[0]


# ---- Who may borrow which slot ----

def bench(session):
    return session != LANE and fnmatch.fnmatchcase(session, os.environ.get('PLUGINCHECK_BENCH_SESSIONS', BENCH_SESSIONS))


def per_session(session):
    return BENCH_MAX_PER_SESSION if bench(session) else MAX_PER_SESSION


def borrowed(state):
    """The leases that count as borrowed: not an exclusive lease, and not the lane's, which is PluginCheck's own."""
    return [l for l in state['leases'] if not l.get('exclusive') and l['session'] != LANE]


def wants(state, request):
    """How many more slots a request may still get."""
    if request.get('exclusive'):
        return 0
    session = request['session']
    have = len(held(state, session))
    if session == LANE:
        return max(0, 1 - have)
    if have == 0:
        sessions = {l['session'] for l in borrowed(state)}
        if len(sessions) >= MAX_SESSIONS or (bench(session) and len({s for s in sessions if bench(s)}) >= BENCH_MAX_SESSIONS):
            return 0
    return max(0, min(request['count'], per_session(session)) - have)


def room(state, total):
    """How many more slots borrowers may take, so PluginCheck keeps OWN_FLOOR of total. None when total is not known."""
    return None if total is None else total - OWN_FLOOR - len(borrowed(state)) - len(state['orphans'])


def open_run(state, session, slot, universe):
    """The adjacent slots around slot that no other session's lease and no orphan holds: as far as session's block could grow."""
    others = {l['cores'] for l in state['leases'] if l['session'] != session} | {o['cores'] for o in state['orphans']}
    ordered = sorted(set(universe) | {slot}, key=bounds)
    first = last = ordered.index(slot)
    while first > 0 and ordered[first - 1] not in others and adjacent(ordered[first - 1], ordered[first]):
        first -= 1
    while last < len(ordered) - 1 and ordered[last + 1] not in others and adjacent(ordered[last], ordered[last + 1]):
        last += 1
    return ordered[first:last + 1]


def bench_target(state, request, spare):
    """The size a bench session's block is to reach: what it asked for, up to BENCH_MAX_PER_SESSION and what the floor leaves."""
    target = min(request['count'], BENCH_MAX_PER_SESSION)
    return target if spare is None else min(target, len(held(state, request['session'])) + max(0, spare))


def may_take(state, request, slot, universe, spare):
    """A bench session's slots are always adjacent cores. Its first slot must sit where the whole block fits, among slots no other session
    holds (PluginCheck's busy ones count, since each comes free when its server ends); each next one must touch the block. Others take any."""
    session = request['session']
    if not bench(session):
        return True
    mine = [l['cores'] for l in held(state, session)]
    if mine and not any(adjacent(slot, m) for m in mine):
        return False
    return universe is None or len(open_run(state, session, slot, universe)) >= bench_target(state, request, spare)


def cuts_a_block(state, taker, slot, universe, spare):
    """True when giving slot to taker would leave a bench session that holds part of its block no room to finish it."""
    if universe is None:
        return False
    for request in state['requests']:
        session = request['session']
        if session == taker['session'] or not bench(session) or not held(state, session) or wants(state, request) == 0:
            continue
        start = held(state, session)[0]['cores']
        before = open_run(state, session, start, universe)
        target = bench_target(state, request, None if spare is None else spare - 1)
        after = open_run({**state, 'orphans': state['orphans'] + [{'cores': slot}]}, session, start, universe)
        if len(before) >= target > len(after):
            return True
    return False


def borrower_for(state, slot, universe=None, floor=True):
    """The request that gets slot, or None. An exclusive request, granted or waiting, stops all others. Within the floor (OWN_FLOOR, only
    with floor, which is while a run is going), a bench session whose block this slot extends goes first, then the earliest request that may
    take it. universe: every slot of the machine, for the floor and the blocks; None leaves both out."""
    if exclusive_wanted(state):
        return None
    spare = room(state, len(set(universe) | {slot})) if floor and universe is not None else None
    fits = [r for r in sorted(state['requests'], key=lambda r: r['since'])
            if wants(state, r) > 0 and (r['session'] == LANE or spare is None or spare > 0)
            and may_take(state, r, slot, universe, spare) and not cuts_a_block(state, r, slot, universe, spare)]
    growing = [r for r in fits if bench(r['session']) and held(state, r['session'])]
    return (growing or fits or [None])[0]


def grant(state, request, cores, now):
    index = 1 + max((l['index'] for l in held(state, request['session'])), default=0)
    lease = {'session': request['session'], 'cores': cores, 'index': index, 'granted': now, 'heartbeat': now,
             'container': f"{request['session']}-{index}", 'purpose': request.get('purpose', '')}
    if request.get('log'):
        lease['log'] = request['log']
    state['leases'].append(lease)
    if wants(state, request) == 0:
        state['requests'] = [r for r in state['requests'] if r is not request]
    return lease


def expire(state, now, container_running=lambda name: False, alive=None):
    """Leases with no heartbeat for HEARTBEAT_LIMIT end. Their slots come back, unless the borrowed container still runs: then
    the slot stays out of use as an orphan until it stops. Runs that stopped beating, or whose process is gone, end too, and so
    does an owner that stopped beating, such as a behaviour stage that ended with no clean-up. Returns the cores that came back."""
    back = []
    alive = alive or process_alive
    owner = state.get('owner')
    if owner and owner.get('run') and not alive(owner.get('pid')):
        # Its run folder too, before the run is dropped: the process that would have ended it is gone.
        end_run_folder(owner['run'], now)
    if owner and not owner_active(state, now):
        owner['running'] = False
        # A stale owner's run, stage and progress are not true any more.
        drop_owner_run(owner)
    elif owner and any(field in owner for field in OWNER_RUN_FIELDS) and not alive(owner.get('pid')):
        # Nor are those of an owner whose process is gone, such as one ended by a hard kill: they go at once, though the owner keeps
        # counting as running until its heartbeat is OWNER_LIMIT old, as before.
        drop_owner_run(owner)
    state['runs'] = [r for r in state.get('runs', []) if now - r.get('heartbeat', 0) <= OWNER_LIMIT and alive(r.get('pid'))]
    # A bench session may run one container over its whole block, named after any of its leases: each of its slots waits for all of them.
    blocks = {}
    for item in state['leases'] + state['orphans']:
        if item.get('container') and bench(item['session']):
            blocks.setdefault(item['session'], set()).add(item['container'])
    for lease in list(state['leases']):
        if now - lease['heartbeat'] > HEARTBEAT_LIMIT:
            state['leases'].remove(lease)
            if lease.get('exclusive'):
                continue
            if lease['session'] in blocks:
                lease['watch'] = sorted(blocks[lease['session']])
            if any(container_running(name) for name in lease.get('watch', [lease['container']])):
                state['orphans'].append(lease)
            else:
                back.append(lease['cores'])
    for orphan in list(state['orphans']):
        if not any(container_running(name) for name in orphan.get('watch', [orphan['container']])):
            state['orphans'].remove(orphan)
            back.append(orphan['cores'])
    state['requests'] = [r for r in state['requests'] if now - r.get('heartbeat', r['since']) <= HEARTBEAT_LIMIT]
    return back


def request(state, session, count, now, exclusive=False, purpose='', log=None):
    # It becomes part of container names, so it must be one plain name: no pattern that could match other containers.
    if not session or not SESSION.match(session):
        raise ValueError('a session name is 1 to 64 letters, digits, "_", "." or "-", starting with a letter or digit')
    if log:
        set_log(state, session, log)
    # Asking again until the count is reached is the borrower's heartbeat meanwhile: a bench block can take minutes to grow.
    for lease in held(state, session):
        lease['heartbeat'] = now
    existing = next((r for r in state['requests'] if r['session'] == session), None)
    if existing:
        existing.update({'count': count, 'exclusive': exclusive, 'heartbeat': now, 'purpose': purpose or existing.get('purpose', '')})
        return existing
    entry = {'session': session, 'count': count, 'exclusive': exclusive, 'since': now, 'heartbeat': now, 'purpose': purpose}
    if log:
        entry['log'] = log
    state['requests'].append(entry)
    return entry


def heartbeat(state, session, now, log=None):
    for item in held(state, session) + [r for r in state['requests'] if r['session'] == session]:
        item['heartbeat'] = now
    if log:
        set_log(state, session, log)


def set_log(state, session, log):
    """A file the borrower writes, which a viewer may read. Only stored, never opened here."""
    for item in held(state, session) + [r for r in state['requests'] if r['session'] == session]:
        item['log'] = log


def release(state, session, cores=None):
    """Ends a session's leases, or one of them, and its request. Returns the cores freed (none for an exclusive lease)."""
    gone = [l for l in held(state, session) if cores is None or l['cores'] == cores]
    state['leases'] = [l for l in state['leases'] if l not in gone]
    if cores is None:
        state['requests'] = [r for r in state['requests'] if r['session'] != session]
    return [l['cores'] for l in gone if not l.get('exclusive')]


def grant_when_idle(state, now, all_cores, containers=NOT_CHECKED):
    """With no PluginCheck run going, requests are granted straight from the slots no lease holds, with no floor. An exclusive request is also
    held back by a stage of a full run, and, when containers (docker_names()) is given, by any running container the lease file
    does not know."""
    if owner_active(state, now):
        return []
    granted = []
    exclusive = next((r for r in sorted(state['requests'], key=lambda r: r['since']) if r.get('exclusive')), None)
    if exclusive and not exclusive_blockers(state, now, 0, containers):
        state['leases'].append({'session': exclusive['session'], 'cores': 'all', 'index': 1, 'granted': now, 'heartbeat': now,
                                'container': '', 'exclusive': True, 'purpose': exclusive.get('purpose', '')})
        state['requests'].remove(exclusive)
        return ['all']
    if exclusive:
        return []
    taken = {l['cores'] for l in state['leases'] + state['orphans']}
    for cores in all_cores:
        if cores in taken:
            continue
        borrower = borrower_for(state, cores, all_cores, floor=False)
        if borrower is None:
            continue
        taken.add(cores)
        granted.append(grant(state, borrower, cores, now)['cores'])
    return granted


def owner_free(state, cores, now, all_cores=None):
    """PluginCheck's server in these cores finished. Returns the lease if a borrower takes the slot, else None. all_cores: the run's
    slots (else the owner's, as saved), for the floor, which holds here since a run is going, and the bench blocks."""
    borrower = borrower_for(state, cores, all_cores or (state.get('owner') or {}).get('cores'))
    return grant(state, borrower, cores, now) if borrower else None


def owner_may_start(state):
    """False while an exclusive request waits or holds the machine: PluginCheck starts no new server."""
    return not exclusive_wanted(state)


def owner_grant_exclusive(state, now, running, containers=NOT_CHECKED, own_stage=False):
    """With an exclusive request waiting, it is granted once PluginCheck runs nothing and no slot is lent or orphaned, no stage of
    a full run is going (the behaviour stage's own owner passes own_stage: it counts its servers itself), and, when containers
    (docker_names()) is given, no running container is unknown to the lease file."""
    exclusive = next((r for r in sorted(state['requests'], key=lambda r: r['since']) if r.get('exclusive')), None)
    if exclusive and not exclusive_blockers(state, now, running, containers, own_stage):
        state['leases'].append({'session': exclusive['session'], 'cores': 'all', 'index': 1, 'granted': now, 'heartbeat': now,
                                'container': '', 'exclusive': True, 'purpose': exclusive.get('purpose', '')})
        state['requests'].remove(exclusive)
        return True
    return False


# ---- The quiet machine: what a drain waits for ----

def ignored_container(name):
    extra = tuple(p.strip() for p in os.environ.get('PLUGINCHECK_IGNORE_CONTAINERS', '').split(',') if p.strip())
    return name.startswith(IGNORED_CONTAINERS + extra)


def unknown_containers(state, names):
    """The running containers the lease file does not know: not a lease's or an orphan's container, not one named after the session that
    holds the exclusive lease (its rig's), not ignored. None when Docker could not be asked."""
    if names is None:
        return None
    known = {l['container'] for l in state['leases'] + state['orphans'] if l.get('container')}
    mine = tuple(l['session'] + '-' for l in state['leases'] if l.get('exclusive'))
    return sorted(n for n in names if n not in known and not n.startswith(mine) and not ignored_container(n))


def fresh_runs(state, now):
    return [r for r in state.get('runs', []) if now - r.get('heartbeat', 0) <= OWNER_LIMIT]


def run_stages(state, now, ignoring=()):
    """The Docker stages of full runs in progress."""
    return sorted({s for r in fresh_runs(state, now) for s in r.get('stages', []) if s not in ignoring})


def exclusive_blockers(state, now, running=0, containers=NOT_CHECKED, own_stage=False):
    """Why an exclusive request is not granted yet, in words. Empty when it may be."""
    why = []
    if running:
        why.append(f'{running} PluginCheck server{"s" if running != 1 else ""} running')
    stages = run_stages(state, now, (OWN_OWNER_STAGE,) if own_stage else ())
    if stages:
        why.append('stages of a full run: ' + ', '.join(stages))
    if state['leases']:
        why.append('leases held by ' + ', '.join(sorted({l['session'] for l in state['leases']})))
    if state['orphans']:
        why.append('slots whose borrowed container still runs: ' + ', '.join(sorted(o['cores'] for o in state['orphans'])))
    if containers is not NOT_CHECKED:
        unknown = unknown_containers(state, containers)
        if unknown is None:
            why.append('Docker could not be asked, so the running containers are not known')
        elif unknown:
            why.append('containers the lease file does not know: ' + ', '.join(unknown))
    return why


def put_run(state, now, pid, target, stages):
    """A full run's entry: who, when it last beat, and the Docker stages it has in progress."""
    state['runs'] = [r for r in state.get('runs', []) if r.get('pid') != pid]
    state['runs'].append({'pid': pid, 'target': target, 'heartbeat': now, 'stages': sorted(stages)})


def drop_run(state, pid):
    state['runs'] = [r for r in state.get('runs', []) if r.get('pid') != pid]


def run_may_begin(state):
    """False while an exclusive request waits or holds: a full run starts no Docker stage."""
    return not exclusive_wanted(state)


# ---- The file, under a lock ----

class Store:
    def __init__(self, folder=FOLDER, wait=60, save=True):
        """save=False only reads: the lock is taken and given back, and the file is left as it was."""
        if folder is None:
            raise SystemExit(f'the lease folder could not be worked out: {FOLDER_ERROR}')
        self.folder, self.wait, self.save = folder, wait, save
        self.path = os.path.join(folder, 'leases.json')
        self.lock = os.path.join(folder, 'leases.lock')

    def __enter__(self):
        os.makedirs(self.folder, exist_ok=True)
        self.token = uuid.uuid4().hex
        deadline = time.time() + self.wait
        while True:
            try:
                os.mkdir(self.lock)
            # PermissionError: on Windows the last holder's lock folder can still be on its way out, and can't be made again until it's gone.
            except (FileExistsError, PermissionError):
                if self.abandoned():
                    self.remove_lock()
                    continue
                if time.time() > deadline:
                    raise TimeoutError(f'{self.lock} is held')
                time.sleep(0.1)
                continue
            with open(os.path.join(self.lock, 'holder'), 'w', encoding='utf-8') as holder:
                json.dump({'pid': os.getpid(), 'time': time.time(), 'token': self.token}, holder)
            break
        try:
            with open(self.path, encoding='utf-8') as saved:
                self.state = json.load(saved)
        except (OSError, ValueError):
            self.state = empty()
        for key, value in empty().items():
            self.state.setdefault(key, value)
        return self.state

    def holder(self):
        try:
            with open(os.path.join(self.lock, 'holder'), encoding='utf-8') as found:
                return json.load(found)
        except (OSError, ValueError):
            return None

    def abandoned(self):
        """Taken over only if its holder's process is gone, or it is older than LOCK_STALE. A lock dir with no holder file yet
        is being made, so it counts only by its own age."""
        held = self.holder()
        try:
            age = time.time() - (held['time'] if held else os.path.getmtime(self.lock))
        except OSError:
            return False
        return age > LOCK_STALE or (held is not None and not process_alive(held.get('pid')))

    def remove_lock(self):
        """Tries again until the lock is gone. On Windows a file can't be deleted while another handle has it open, and every waiter opens
        `holder` ten times a second to see whether the lock is abandoned. Giving up at the first failure left the lock behind, so every
        call after it waited until it went stale."""
        deadline = time.time() + LOCK_REMOVE_SECONDS
        while True:
            try:
                os.remove(os.path.join(self.lock, 'holder'))
            except FileNotFoundError:
                pass
            except OSError:
                if time.time() < deadline:
                    time.sleep(0.01)
                    continue
            try:
                os.rmdir(self.lock)
                return
            except FileNotFoundError:
                return
            except OSError:
                if time.time() >= deadline:
                    return
                time.sleep(0.01)

    def __exit__(self, kind, error, trace):
        owned = (self.holder() or {}).get('token') == self.token
        try:
            if kind is None and owned and self.save:
                temporary = self.path + '.tmp'
                with open(temporary, 'w', encoding='utf-8') as out:
                    json.dump(self.state, out, indent=1)
                replace(temporary, self.path)
        finally:
            # Only a lock this holder still owns: one taken over as abandoned belongs to someone else now.
            if owned:
                self.remove_lock()
        if kind is None and not owned and self.save:
            raise RuntimeError(f'{self.lock} was taken over, so this change was not saved')
        return False


def replace(source, destination, seconds=LOCK_REMOVE_SECONDS):
    """os.replace, tried again for a moment. On Windows it is refused while another program has the file open to read
    it, and giving up then loses the change."""
    deadline = time.time() + seconds
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if time.time() >= deadline:
                raise
            time.sleep(0.01)


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.GetLastError() == 5  # access denied means it exists
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Owner:
    """PluginCheck's side, for the behaviour stage. Thread safe through the file lock: each call opens its own Store, since a Store
    holds the token and state of the one call inside it.

    start() says which slots are lent already, so the run leaves them out. free() offers a slot whose server finished; when a
    borrower takes it, it is lent. beat() runs every few seconds: it keeps the owner's heartbeat, ends leases with none, grants a
    waiting exclusive request once nothing runs, and returns the lent slots that came back, for the run to use again."""

    def __init__(self, all_cores, folder=FOLDER, names=docker_names, clock=time.time, run=None, stage='behaviour'):
        """run: the run folder, named in the owner entry with stage and progress while the owner runs (OWNER_RUN_FIELDS)."""
        # names() reads the running containers before the lock is taken: nothing slow runs under it.
        self.all_cores, self.folder, self.names, self.clock = list(all_cores), folder, names, clock
        self.lent = set()
        self.run, self.stage, self.progress = run, stage, None

    def owner_entry(self, now, running):
        entry = {'name': 'plugincheck', 'pid': os.getpid(), 'heartbeat': now, 'running': True, 'servers': running, 'cores': self.all_cores}
        for field in OWNER_RUN_FIELDS:
            if getattr(self, field) is not None:
                entry[field] = getattr(self, field)
        return entry

    def start(self):
        try:
            running_now = running_check(self.names())
        except Exception:
            running_now = running_check(None)
        with Store(self.folder) as state:
            now = self.clock()
            expire(state, now, running_now)
            self.lent = {l['cores'] for l in state['leases'] + state['orphans'] if l['cores'] in self.all_cores}
            if any(l.get('exclusive') for l in state['leases']):
                self.lent = set(self.all_cores)
            state['owner'] = self.owner_entry(now, 0)
            return set(self.lent)

    def beat(self, running):
        """running: the servers running, or a function giving it. A function is read under the lock, so a server counted while Docker
        was asked still holds an exclusive grant back."""
        try:
            names = self.names()
        except Exception:
            names = None
        running_now = running_check(names)
        failed = None
        with Store(self.folder) as state:
            now = self.clock()
            running = running() if callable(running) else running
            # The heartbeat is saved even if what follows fails: a silent owner would be taken for idle, and lent from freely.
            state['owner'] = self.owner_entry(now, running)
            try:
                expire(state, now, running_now)
                # The exclusive lease also waits for the other stages of the run and for any container the lease file does not know.
                owner_grant_exclusive(state, now, running, names, own_stage=True)
            except Exception as error:
                failed = error
            exclusive = any(l.get('exclusive') for l in state['leases'])
            still = {l['cores'] for l in state['leases'] + state['orphans']}
            back = [] if exclusive else sorted(c for c in self.lent if c not in still)
            self.lent -= set(back)
        if failed:
            raise failed
        return back

    def free(self, cores):
        with Store(self.folder) as state:
            lease = owner_free(state, cores, self.clock(), self.all_cores)
            if lease:
                self.lent.add(cores)
            return lease

    def may_start(self):
        with Store(self.folder) as state:
            return owner_may_start(state)

    def report(self, stage=None, progress=None):
        """A stage change or a progress step, written to the owner entry at once with the heartbeat. Only while this owner holds the
        entry: once it has stopped, or another owner has it, nothing is written. True when written."""
        if stage is not None:
            self.stage = stage
        if progress is not None:
            self.progress = dict(progress)
        with Store(self.folder, wait=REPORT_WAIT) as state:
            owner = state.get('owner')
            if not (owner and owner.get('running') and owner.get('pid') == os.getpid()):
                return False
            owner['heartbeat'] = self.clock()
            owner.update({field: getattr(self, field) for field in OWNER_RUN_FIELDS if getattr(self, field) is not None})
            return True

    def stop(self):
        """At the end of a run: slots still lent stay with their borrowers, the owner just stops running, and its run, stage and
        progress go. Only this process's entry: one another owner has taken over is left alone."""
        with Store(self.folder) as state:
            owner = state.get('owner')
            if owner and owner.get('pid') == os.getpid():
                owner['running'] = False
                drop_owner_run(owner)


class Lending:
    """The behaviour stage's side of lending, which no lending error can stop, slow or oversubscribe.

    If the owner can't start, such as an unreadable lease file or a lock that times out, the run goes on without lending and
    every call does nothing. After that, an error in a call is logged once per kind and the run carries on: may_start says yes,
    free keeps the slot for the run, and beat returns nothing, so a slot is never lost from the run's queue.

    stop() is final: a beat after it, or one still running when it is called, cannot mark the owner as running again."""

    def __init__(self, make_owner, say=print):
        """With make_owner None, lending is off on purpose, such as in the pre-container lane, which must never act as the owner."""
        self.say, self.seen, self.owner, self.lent_at_start = say, set(), None, set()
        self.stopped, self.ending = False, threading.Lock()
        if make_owner is None:
            return
        try:
            owner = make_owner()
            self.lent_at_start = owner.start()
            self.owner = owner
        except Exception as error:
            self.say(f'lending off for this run: {type(error).__name__}: {error}')

    def note(self, what, error):
        kind = (what, type(error).__name__)
        if kind not in self.seen:
            self.seen.add(kind)
            self.say(f'lending: {what} failed ({type(error).__name__}: {error}), carrying on')

    def may_start(self):
        if self.owner is None:
            return True
        try:
            return self.owner.may_start()
        except Exception as error:
            self.note('may_start', error)
            return True

    def free(self, cores):
        """The lease if a borrower took the slot. None, keeping it for the run, otherwise or on any error."""
        if self.owner is None:
            return None
        try:
            return self.owner.free(cores)
        except Exception as error:
            self.note('free', error)
            return None

    def beat(self, running):
        with self.ending:
            if self.owner is None or self.stopped:
                return []
            try:
                return self.owner.beat(running)
            except Exception as error:
                self.note('beat', error)
                return []

    def report(self, stage=None, progress=None):
        """The run's stage or progress for the owner entry (Owner.report). Nothing after stop(), and an error never reaches the stage.
        It holds no lock of its own while it writes: a stage's main thread calls it, and an interrupt handler that runs on that thread
        (POSIX signals) calls stop(), which would wait for ever on a lock held under it. A report racing a stop is harmless, since both
        take the lease file's lock and Owner.report writes only into a running entry."""
        if self.owner is None or self.stopped:
            return
        try:
            self.owner.report(stage, progress)
        except Exception as error:
            self.note('report', error)

    def stop(self):
        with self.ending:
            if self.owner is None or self.stopped:
                return
            self.stopped = True
            try:
                self.owner.stop()
            except Exception as error:
                self.note('stop', error)


def machine_cores():
    """The slots the behaviour stage makes: 2 logical cores each, from the cores Docker can use, leaving out the user's own."""
    return make_slots(core_count(), reserved_cores())


class Lane:
    """PluginCheck's pre-container lane (check.py --pre): one slot of its own, held as the session LANE.

    acquire() waits for the slot: granted at once with no behaviour run going, else when one of the run's servers finishes. stage()
    runs one stage at a time on it, since one slot has 2 cores. A thread keeps the heartbeat, and when an exclusive request appears
    it lets the stage going finish, gives the slot back and waits for the machine again before the next stage. release() gives the
    slot back for good, which ends the lane."""

    def __init__(self, all_cores, folder=FOLDER, names=docker_names, clock=time.time, poll=5, beat_every=30, say=print):
        self.all_cores, self.folder, self.names, self.clock = list(all_cores), folder, names, clock
        self.poll, self.beat_every, self.say = poll, beat_every, say
        self.cores, self.active, self.draining = None, 0, False
        self.changed, self.stopped, self.thread = threading.Condition(), threading.Event(), None

    def ask(self):
        """Asks for the slot once. Returns its cores once granted, else None."""
        try:
            names = self.names()
        except Exception:
            names = None
        with Store(self.folder) as state:
            now = self.clock()
            expire(state, now, running_check(names))
            request(state, LANE, 1, now, purpose='PluginCheck pre-container lane')
            grant_when_idle(state, now, self.all_cores, names)
            mine = held(state, LANE)
            return mine[0]['cores'] if mine else None

    def wait_for_slot(self):
        """Asks until the slot is granted. A lease file that can't be read or locked for a moment is asked again, so the thread that
        brings the lane back after an exclusive request never dies."""
        while not self.stopped.is_set():
            try:
                cores = self.ask()
            except Exception as error:
                self.say(f'lane: asking for the slot failed ({type(error).__name__}: {error}), trying again')
                cores = None
            if cores:
                return cores
            self.stopped.wait(self.poll)
        return None

    def acquire(self):
        cores = self.wait_for_slot()
        with self.changed:
            self.cores = cores
            self.changed.notify_all()
        if self.thread is None:
            self.thread = threading.Thread(target=self.keep, daemon=True)
            self.thread.start()
        return cores

    def beat(self):
        """The heartbeat. True when an exclusive request waits or holds the machine."""
        with Store(self.folder) as state:
            heartbeat(state, LANE, self.clock())
            return exclusive_wanted(state)

    def keep(self):
        """The heartbeat thread. Nothing may end it: without it the heartbeat stops, and a stage waiting for the lane to come back waits
        for ever."""
        while not self.stopped.wait(self.beat_every):
            try:
                self.keep_once()
            except Exception as error:
                self.say(f'lane: keeping the slot failed ({type(error).__name__}: {error}), trying again')

    def keep_once(self):
        try:
            if not self.beat():
                return
        except Exception as error:
            self.say(f'lane: heartbeat failed ({type(error).__name__}: {error}), trying again')
            return
        with self.changed:
            self.draining = True
            while self.active and not self.stopped.is_set():
                self.changed.wait()
        if self.stopped.is_set():
            return
        self.say('lane: an exclusive request is waiting, so the slot goes back until it is done')
        if not self.give_back_until_done():
            return
        cores = self.wait_for_slot()
        with self.changed:
            self.cores, self.draining = cores, False
            self.changed.notify_all()

    @contextlib.contextmanager
    def stage(self):
        """Holds the slot for one stage, and gives its cores. Waits while another stage runs, or while the lane steps aside."""
        with self.changed:
            while (self.draining or self.active or not self.cores) and not self.stopped.is_set():
                self.changed.wait()
            self.active += 1
            cores = self.cores
        try:
            yield cores
        finally:
            with self.changed:
                self.active -= 1
                self.changed.notify_all()

    def give_back(self):
        with Store(self.folder) as state:
            release(state, LANE)
        with self.changed:
            self.cores = None

    def give_back_until_done(self, seconds=None):
        """give_back, tried again with a growing pause while the lease file can't be written: on Windows a write is refused while another
        program has the file open. Without a limit it gives up only when the lane is released, which gives the slot back itself."""
        pause, deadline = 0.1, None if seconds is None else time.time() + seconds
        while True:
            try:
                self.give_back()
                return True
            except Exception as error:
                self.say(f'lane: giving the slot back failed ({type(error).__name__}: {error}), trying again')
            if (deadline is None and self.stopped.is_set()) or (deadline is not None and time.time() >= deadline):
                return False
            time.sleep(pause)
            pause = min(pause * 2, self.poll)

    def release(self):
        self.stopped.set()
        with self.changed:
            self.changed.notify_all()
        if not self.give_back_until_done(seconds=RELEASE_SECONDS):
            self.say(f'lane: the slot could not be given back, so its lease ends by itself {HEARTBEAT_LIMIT // 60} minutes after the last heartbeat')


class RunHold:
    """A full check.py run's side of the quiet-machine hold (README, Exclusive requests).

    The run registers for its whole life, and counts each Docker-using stage while it runs. Before a stage starts, begin() waits while
    an exclusive request waits or holds, so every stage boundary is a hold point; the check and the count are one step under the
    lease file's lock, so a request that arrives is never granted beside a stage that has just started. When a stage ends, or the
    run does, a waiting exclusive request is granted at once if nothing else runs. A thread keeps the run's heartbeat and tries the
    grant too (a container that was still stopping when the stage ended). A lease file that can't be read or locked never stops a
    run: the stage starts, once noted, as lending does."""

    def __init__(self, target, folder=FOLDER, names=docker_names, clock=time.time, poll=5, beat_every=30, say=print):
        self.target, self.folder, self.names, self.clock = target, folder, names, clock
        self.poll, self.beat_every, self.say = poll, beat_every, say
        self.pid, self.stages = os.getpid(), []
        self.mine, self.stopped, self.thread, self.noted = threading.Lock(), threading.Event(), None, set()
        self.fetch, self.seen = threading.Lock(), None

    def note(self, what, error):
        if what not in self.noted:
            self.noted.add(what)
            self.say(f'hold: {what} failed ({type(error).__name__}: {error}), carrying on')

    def read_names(self, max_age=0):
        """docker_names(), at most max_age seconds old, so the stages waiting together ask Docker once between them."""
        with self.fetch:
            if max_age > 0 and self.seen is not None and self.clock() - self.seen[0] <= max_age:
                return self.seen[1]
            try:
                found = self.names()
            except Exception:
                found = None
            self.seen = (self.clock(), found)
            return found

    def write(self, state, now):
        put_run(state, now, self.pid, self.target, self.stages)

    def start(self):
        """Registers the run, with no stage yet, and starts the heartbeat."""
        try:
            with Store(self.folder) as state:
                self.write(state, self.clock())
        except Exception as error:
            self.note('registering the run', error)
        if self.thread is None:
            self.thread = threading.Thread(target=self.keep, daemon=True)
            self.thread.start()

    def begin(self, stage):
        """Waits while an exclusive request waits or holds, then counts the stage as running."""
        waited = False
        while True:
            # A hold that lasts is checked for a request or lease that has lapsed, which only expire() ends.
            names = self.read_names(self.poll) if waited else None
            try:
                with Store(self.folder) as state:
                    now = self.clock()
                    if waited:
                        expire(state, now, running_check(names))
                    if run_may_begin(state) or self.stopped.is_set():
                        with self.mine:
                            self.stages.append(stage)
                        self.write(state, now)
                        if waited:
                            self.say(f'hold: the exclusive request is done, so {stage} starts')
                        return
            except Exception as error:
                self.note('checking for an exclusive request', error)
                with self.mine:
                    self.stages.append(stage)
                return
            if not waited:
                waited = True
                self.say(f'hold: an exclusive request waits or holds the machine, so {stage} waits to start')
            self.stopped.wait(self.poll)

    def end(self, stage):
        """The stage is over: it no longer counts, and a waiting exclusive request is granted if nothing else runs."""
        with self.mine:
            if stage in self.stages:
                self.stages.remove(stage)
        self.beat()

    def beat(self):
        names = self.read_names()
        try:
            with Store(self.folder) as state:
                now = self.clock()
                expire(state, now, running_check(names))
                self.write(state, now)
                owner_grant_exclusive(state, now, 0, names)
        except Exception as error:
            self.note('beating', error)

    def keep(self):
        while not self.stopped.wait(self.beat_every):
            self.beat()

    @contextlib.contextmanager
    def stage(self, name):
        self.begin(name)
        try:
            yield
        finally:
            self.end(name)

    def close(self):
        """The run is over. Its entry goes, and a waiting exclusive request is granted if nothing else runs."""
        self.stopped.set()
        names = self.read_names()
        try:
            with Store(self.folder) as state:
                now = self.clock()
                drop_run(state, self.pid)
                expire(state, now, running_check(names))
                owner_grant_exclusive(state, now, 0, names)
        except Exception as error:
            self.note('ending the run', error)


# ---- One run at a time: a second check.py waits for its turn (README, One run at a time) ----

# The run queue's name. PLUGINCHECK_RUN_QUEUE gives another, such as for tests that must not wait on a real run.
RUN_QUEUE = 'PluginCheck-run'


class RunQueue:
    """The machine's one-run-at-a-time turn, whatever lease folder a copy uses: a lock the operating system keeps for the process holding
    it and frees when that process ends, however it ends, so no turn is ever left behind. On Windows a named mutex in this logon session,
    elsewhere flock on a file in the temp folder. A full run takes it before it registers (check.py); the pre-container lane does not,
    since it is part of the run whose behaviour stage it overlaps."""

    def __init__(self, name=None):
        self.name = name or os.environ.get('PLUGINCHECK_RUN_QUEUE') or RUN_QUEUE
        self.handle = None

    def take(self, seconds=0):
        """True once this process holds the turn, waiting at most seconds for it. OSError when the operating system cannot wait on it."""
        if self.handle is not None:
            return True
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
            kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel.CreateMutexW(None, False, 'Local\\' + self.name)
            if not handle:
                raise OSError(ctypes.get_last_error(), f'the run queue {self.name} could not be opened')
            # 0: taken; 0x80: taken from a process that ended holding it; 0x102: still held when the time ran out.
            waited = kernel.WaitForSingleObject(handle, int(seconds * 1000))
            if waited in (0, 0x80):
                self.handle = handle
                return True
            error = ctypes.get_last_error()
            kernel.CloseHandle(handle)
            if waited != 0x102:
                # WAIT_FAILED comes back at once: counted as a turn still held, a waiting run would ask again and again without end.
                raise OSError(error, f'the run queue {self.name} could not be waited on (WaitForSingleObject gave {waited:#x})')
            return False
        import fcntl, tempfile
        handle = open(os.path.join(tempfile.gettempdir(), self.name + '.lock'), 'a')
        deadline = time.time() + seconds
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.handle = handle
                return True
            except OSError:
                if time.time() >= deadline:
                    handle.close()
                    return False
                time.sleep(0.1)

    def release(self):
        if self.handle is None:
            return
        handle, self.handle = self.handle, None
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.ReleaseMutex.argtypes = kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
        else:
            handle.close()

    def going(self):
        """True while another process holds the turn. A look: taken and given straight back when free."""
        if self.handle is not None:
            return False
        if self.take(0):
            self.release()
            return False
        return True

    def wait(self, say=print, poll=5, report_every=300):
        """Waits for the turn and keeps it until release() or the process ends. Returns the seconds waited."""
        started, told = time.time(), None
        while not self.take(poll):
            if told is None or time.time() - told >= report_every:
                say(f'another PluginCheck run is going: one at a time, so this run waits for its turn ({time.time() - started:.0f} s so far)')
                told = time.time()
        if told is not None:
            say(f'the run before has ended after {time.time() - started:.0f} s, so this run starts')
        return time.time() - started


# ---- The machine gate: work outside PluginCheck waits while the machine is claimed (README, Machine gate) ----

class GateTimeout(TimeoutError):
    """The gate was still shut when its time ran out. The message names who claims the machine and since when; claims lists it."""

    def __init__(self, message, claims):
        super().__init__(message)
        self.claims = claims


def when(seconds):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(seconds))


def claims(state, now, session=None):
    """What shuts the gate, in words, oldest first: each exclusive lease and each exclusive request. Empty when the machine is free.
    A claim that has lapsed does not count, and neither does one of session, the caller's own. Worked out on a copy: nothing is changed."""
    view = json.loads(json.dumps(state))
    expire(view, now, running_check(None), alive=lambda pid: True)
    mine = lambda item: item.get('exclusive') and item['session'] != session
    purpose = lambda item: f" ({item['purpose']})" if item.get('purpose') else ''
    said = [f"exclusive lease held by {l['session']}{purpose(l)} since {when(l['granted'])}"
            for l in sorted(filter(mine, view['leases']), key=lambda l: l['granted'])]
    # A holder that asks again (lease.py request until the reply shows the lease) leaves a request beside its lease: not a second claim.
    holders = {l['session'] for l in view['leases'] if l.get('exclusive')}
    waiting = sorted((r for r in view['requests'] if mine(r) and r['session'] not in holders), key=lambda r: r['since'])
    if waiting:
        running = (view.get('owner') or {}).get('servers', 0) if owner_active(view, now) else 0
        blockers = exclusive_blockers(view, now, running)
        said += [f"exclusive request by {r['session']}{purpose(r)} waiting since {when(r['since'])}"
                 + (f", for: {'; '.join(blockers)}" if blockers else '') for r in waiting]
    return said


def gate(purpose, timeout=None, session=None, folder=FOLDER, clock=time.time, sleep=time.sleep, poll=10, report_every=300, say=print,
         queue=None):
    """Waits while an exclusive lease is held or waiting, or a PluginCheck run is going, then returns the seconds it waited. Any session's
    build, test suite or live run calls it first, so a measurement that claimed the machine waits only for work already running, and work
    queues behind a run. With timeout (seconds, 0 for one look) it raises GateTimeout once that has passed with the gate still shut.
    session leaves the caller's own claim out, and a session with an exclusive claim of its own is not held back by a run (the run holds
    for that claim at its next step). It starts nothing and changes nothing: it reads the lease file under its lock and looks at the run
    queue (RunQueue.going). A lease file it cannot read counts as shut, and is named."""
    started, told = clock(), None
    while True:
        left = None if timeout is None else timeout - (clock() - started)
        try:
            with Store(folder, wait=60 if left is None else max(1, min(60, left)), save=False) as state:
                now = clock()
                why = claims(state, now, session)
                mine = session is not None and any(item.get('exclusive') and item['session'] == session
                                                   for item in state['leases'] + state['requests'])
                targets = sorted({r.get('target', '?') for r in fresh_runs(state, now)})
            if not why and not mine and RunQueue(queue).going():
                why = ['a PluginCheck run is going' + (f" ({', '.join(targets)})" if targets else '') + ': one at a time, so this waits until it ends']
        except Exception as error:
            why = [f'the lease file could not be read ({type(error).__name__}: {error})']
        waited = clock() - started
        if not why:
            if told is not None:
                say(f'machine gate ({purpose}): free after {waited:.0f} s')
            return waited
        if timeout is not None and waited >= timeout:
            raise GateTimeout(f'machine gate ({purpose}): still shut after {waited:.0f} s: ' + '; '.join(why), why)
        if told is None or clock() - told >= report_every:
            say(f'machine gate ({purpose}): waiting: ' + '; '.join(why))
            told = clock()
        sleep(poll if timeout is None else max(0.0, min(poll, timeout - waited)))
