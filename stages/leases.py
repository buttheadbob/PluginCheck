"""Slot lending: other sessions borrow PluginCheck's server slots for in-game work, such as mod or Torch fixes, never
other plugin checks.

PluginCheck's behaviour stage runs its servers in slots of two logical cores each. A borrower asks for up to MAX_PER_SESSION
slots, and at most MAX_SESSIONS sessions borrow at once, so PluginCheck always keeps the rest. PluginCheck hands a slot over only
when one of its own servers finishes there: it never stops a running server. When no PluginCheck run is going, a request is
granted at once from the free slots. A borrower keeps its leases alive with a heartbeat; a lease with none for HEARTBEAT_LIMIT
seconds returns, but a slot whose borrowed container still runs is not reused until it stops. PluginCheck never stops another
session's container.

An exclusive request, for measurements that need a quiet machine, drains everything: no new grants, no new PluginCheck
servers, and it is granted once nothing else runs.

PluginCheck's own pre-container lane (check.py --pre) takes one slot as the session LANE. It counts against neither borrower limit,
so with 3 sessions borrowing 2 slots each a run keeps 16 - 6 - 1 = 9. It is granted like any lease, gives its slot back when it
ends, and steps aside for an exclusive request (Lane).

Everything lives in one JSON file, changed under a lock, so any session can use it from the command line (lease.py) and
PluginCheck from Python. The decisions are plain functions of the state and the time, so they are tested without Docker.
"""
import contextlib, json, os, re, threading, time, uuid

# Shared by every session on the machine. PLUGINCHECK_MACHINE_DIR moves it, such as to a folder outside this checkout.
FOLDER = os.environ.get('PLUGINCHECK_MACHINE_DIR') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'machine')
MAX_PER_SESSION = 2
MAX_SESSIONS = 3
HEARTBEAT_LIMIT = 300
OWNER_LIMIT = 120
LOCK_STALE = 120
LOCK_REMOVE_SECONDS = 5
# How long the lane's last give-back is tried when it ends, before its lease is left to expire.
RELEASE_SECONDS = 10
SESSION = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
# PluginCheck's pre-container lane: at most one slot, outside MAX_SESSIONS and MAX_PER_SESSION. lease.py refuses the name.
LANE = 'plugincheck-pre'


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
    return {'owner': None, 'requests': [], 'leases': [], 'orphans': []}


# ---- Decisions: pure functions of a state and the time ----

def owner_active(state, now):
    owner = state.get('owner')
    return bool(owner and owner.get('running') and now - owner.get('heartbeat', 0) <= OWNER_LIMIT)


def exclusive_wanted(state):
    return any(r.get('exclusive') for r in state['requests']) or any(l.get('exclusive') for l in state['leases'])


def held(state, session):
    return [l for l in state['leases'] if l['session'] == session]


def wants(state, request):
    """How many more slots a request may still get."""
    if request.get('exclusive'):
        return 0
    have = len(held(state, request['session']))
    if request['session'] == LANE:
        return max(0, 1 - have)
    if have == 0 and len({l['session'] for l in state['leases'] if not l.get('exclusive') and l['session'] != LANE}) >= MAX_SESSIONS:
        return 0
    return max(0, min(request['count'], MAX_PER_SESSION) - have)


def next_borrower(state):
    """The earliest request that may still get a slot, or None. An exclusive request, granted or waiting, stops all others."""
    if exclusive_wanted(state):
        return None
    for request in sorted(state['requests'], key=lambda r: r['since']):
        if wants(state, request) > 0:
            return request
    return None


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


def expire(state, now, container_running=lambda name: False):
    """Leases with no heartbeat for HEARTBEAT_LIMIT end. Their slots come back, unless the borrowed container still runs: then
    the slot stays out of use as an orphan until it stops. Returns the cores that came back."""
    back = []
    for lease in list(state['leases']):
        if now - lease['heartbeat'] > HEARTBEAT_LIMIT:
            state['leases'].remove(lease)
            if lease.get('exclusive'):
                continue
            if container_running(lease['container']):
                state['orphans'].append(lease)
            else:
                back.append(lease['cores'])
    for orphan in list(state['orphans']):
        if not container_running(orphan['container']):
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


def grant_when_idle(state, now, all_cores):
    """With no PluginCheck run going, requests are granted straight from the slots no lease holds."""
    if owner_active(state, now):
        return []
    granted = []
    exclusive = next((r for r in sorted(state['requests'], key=lambda r: r['since']) if r.get('exclusive')), None)
    if exclusive and not state['leases'] and not state['orphans']:
        state['leases'].append({'session': exclusive['session'], 'cores': 'all', 'index': 1, 'granted': now, 'heartbeat': now,
                                'container': '', 'exclusive': True, 'purpose': exclusive.get('purpose', '')})
        state['requests'].remove(exclusive)
        return ['all']
    taken = {l['cores'] for l in state['leases'] + state['orphans']}
    for cores in all_cores:
        if cores in taken:
            continue
        borrower = next_borrower(state)
        if borrower is None:
            break
        granted.append(grant(state, borrower, cores, now)['cores'])
    return granted


def owner_free(state, cores, now):
    """PluginCheck's server in these cores finished. Returns the lease if a borrower takes the slot, else None."""
    borrower = next_borrower(state)
    return grant(state, borrower, cores, now) if borrower else None


def owner_may_start(state):
    """False while an exclusive request waits or holds the machine: PluginCheck starts no new server."""
    return not exclusive_wanted(state)


def owner_grant_exclusive(state, now, running):
    """With an exclusive request waiting, it is granted once PluginCheck runs nothing and no slot is lent or orphaned."""
    exclusive = next((r for r in sorted(state['requests'], key=lambda r: r['since']) if r.get('exclusive')), None)
    if exclusive and running == 0 and not state['leases'] and not state['orphans']:
        state['leases'].append({'session': exclusive['session'], 'cores': 'all', 'index': 1, 'granted': now, 'heartbeat': now,
                                'container': '', 'exclusive': True, 'purpose': exclusive.get('purpose', '')})
        state['requests'].remove(exclusive)
        return True
    return False


# ---- The file, under a lock ----

class Store:
    def __init__(self, folder=FOLDER, wait=60):
        self.folder, self.wait = folder, wait
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
            if kind is None and owned:
                temporary = self.path + '.tmp'
                with open(temporary, 'w', encoding='utf-8') as out:
                    json.dump(self.state, out, indent=1)
                replace(temporary, self.path)
        finally:
            # Only a lock this holder still owns: one taken over as abandoned belongs to someone else now.
            if owned:
                self.remove_lock()
        if kind is None and not owned:
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

    def __init__(self, all_cores, folder=FOLDER, names=docker_names, clock=time.time):
        # names() reads the running containers before the lock is taken: nothing slow runs under it.
        self.all_cores, self.folder, self.names, self.clock = list(all_cores), folder, names, clock
        self.lent = set()

    def owner_entry(self, now, running):
        return {'name': 'plugincheck', 'pid': os.getpid(), 'heartbeat': now, 'running': True, 'servers': running, 'cores': self.all_cores}

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
        try:
            running_now = running_check(self.names())
        except Exception:
            running_now = running_check(None)
        failed = None
        with Store(self.folder) as state:
            now = self.clock()
            # The heartbeat is saved even if what follows fails: a silent owner would be taken for idle, and lent from freely.
            state['owner'] = self.owner_entry(now, running)
            try:
                expire(state, now, running_now)
                owner_grant_exclusive(state, now, running)
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
            lease = owner_free(state, cores, self.clock())
            if lease:
                self.lent.add(cores)
            return lease

    def may_start(self):
        with Store(self.folder) as state:
            return owner_may_start(state)

    def stop(self):
        """At the end of a run: slots still lent stay with their borrowers, the owner just stops running."""
        with Store(self.folder) as state:
            if state.get('owner'):
                state['owner']['running'] = False


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
    """The slots the behaviour stage makes: 2 logical cores each, from the cores Docker can use."""
    count = int(os.environ.get('PLUGINCHECK_LEASE_CORES', os.cpu_count() or 2))
    return [f'{first}-{first + 1}' for first in range(0, count - 1, 2)]


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
            running_now = running_check(self.names())
        except Exception:
            running_now = running_check(None)
        with Store(self.folder) as state:
            now = self.clock()
            expire(state, now, running_now)
            request(state, LANE, 1, now, purpose='PluginCheck pre-container lane')
            grant_when_idle(state, now, self.all_cores)
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
