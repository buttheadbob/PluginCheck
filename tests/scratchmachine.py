"""Keeps every test off the lease folder this checkout would use for real (stages/leases.py, machine_folder): the one machine-dir.txt names,
which live runs and other sessions may share, or the checkout's own machine folder.

Each test module, and tests/replay.py, imports this before anything of PluginCheck's own (tests/test_machine_guard.py checks it). It points
PLUGINCHECK_MACHINE_DIR at a scratch folder of this test process, removed when the process ends; the variable wins over machine-dir.txt,
so leases.FOLDER and every process a test starts with this environment use the scratch folder. PLUGINCHECK_RUNS_DIR is dropped, and
PLUGINCHECK_RUN_QUEUE names a run queue of this process, so no test waits on or stands for a real run. The forbidden folders are listed in
PLUGINCHECK_MACHINE_DIR_FORBIDDEN, so leases.machine_folder refuses them in every process a test starts too, whatever environment it was
given otherwise. A lease file opened in a forbidden folder from this process raises, and is listed in touched; if any was, the process
ends with exit code 1 whatever the tests said, since a lending call that catches the error would hide it. Folders compare by real path.
"""
import atexit, os, shutil, sys, tempfile, uuid

FOLDER = tempfile.mkdtemp(prefix='pc-machine-')
os.environ['PLUGINCHECK_MACHINE_DIR'] = FOLDER
atexit.register(shutil.rmtree, FOLDER, True)
# A run a test starts goes to its own copy's runs folder, never to one this environment names for real runs.
os.environ.pop('PLUGINCHECK_RUNS_DIR', None)
os.environ['PLUGINCHECK_RUN_QUEUE'] = f'pc-test-{os.getpid()}-{uuid.uuid4().hex[:8]}'

STAGES = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'stages'))
if STAGES not in sys.path:
    sys.path.insert(0, STAGES)
import leases  # noqa: E402  (after PLUGINCHECK_MACHINE_DIR, which it reads once)


def _forbidden():
    """The checkout's own machine folder, and the folder its machine-dir.txt names when it holds a valid one: read only, to refuse it."""
    found = [os.path.join(leases.CHECKOUT, 'machine')]
    try:
        found.append(leases.machine_folder({}))
    except (OSError, ValueError):
        pass
    return sorted({os.path.normcase(os.path.realpath(f)) for f in found})


FORBIDDEN = _forbidden()
os.environ[leases.FORBIDDEN_ENV] = os.pathsep.join(FORBIDDEN)


def forbidden(folder):
    return os.path.normcase(os.path.realpath(folder)) in FORBIDDEN


touched = []
_enter = leases.Store.__enter__


def _guarded(store):
    if forbidden(store.folder):
        touched.append(store.folder)
        raise AssertionError(f'a test opened the lease file in {store.folder}, a folder this checkout uses for real; tests use '
                             f'PLUGINCHECK_MACHINE_DIR ({FOLDER})')
    return _enter(store)


leases.Store.__enter__ = _guarded


@atexit.register
def _fail_if_touched():
    if touched:
        sys.stderr.write(f'FAILED: tests opened a real lease folder {len(touched)} times ({touched[0]})\n')
        sys.stderr.flush()
        shutil.rmtree(FOLDER, True)
        os._exit(1)
