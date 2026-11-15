"""Where a run's folder goes: <runs>\\<target>\\<yyyyMMdd-HHmmss>, under this checkout's runs folder, or the one PLUGINCHECK_RUNS_DIR
names, such as the main checkout's for a copy whose runs a viewer of that folder should see. Each run gets a folder of its own: a stamp
whose folder is there already is never used again, and the next second's is tried instead."""
import datetime, os

STAMP = '%Y%m%d-%H%M%S'


def root():
    """The runs folder, as an absolute path."""
    checkout = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.abspath(os.environ.get('PLUGINCHECK_RUNS_DIR') or os.path.join(checkout, 'runs'))


def new(target, runs=None, now=datetime.datetime.now):
    """Makes the run's folder and returns its path. The folder is made, never reused, so two runs started in the same second, from this
    copy or another that shares the runs folder, each get their own."""
    folder = os.path.join(runs or root(), target)
    os.makedirs(folder, exist_ok=True)
    stamp = now().replace(microsecond=0)
    while True:
        path = os.path.join(folder, stamp.strftime(STAMP))
        try:
            os.mkdir(path)
            return path
        except FileExistsError:
            stamp += datetime.timedelta(seconds=1)
