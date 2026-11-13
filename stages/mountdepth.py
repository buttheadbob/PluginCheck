"""How deep PluginCheck's folders may be for Docker Desktop.

On Windows, Docker Desktop gives a container that runs as a user, as the Legacy and Torch images do, an Input/output error on a bind mount
whose host folder has more than LIMIT path parts, the drive counted, whatever the length. A container that runs as root, as Magnetar's
does, reads it, and only the mounted folder's own depth counts, not that of the files inside it. Every Legacy and Torch server would then exit
at start, so a run says why before it starts instead.
"""
import os, pathlib

LIMIT = 17


def deepest(run_dir):
    """The deepest folder a run bind-mounts: behaviour's per-server folders, <run>/behaviour/runs/<server>/<seed|marker|source|plugins>."""
    return os.path.join(run_dir, 'behaviour', 'runs', '0000-server', 'seed')


def problem(run_dir, windows=os.name == 'nt'):
    """Why this run folder is too deep, or None."""
    if not windows:
        return None
    folder = deepest(run_dir)
    parts = len(pathlib.PureWindowsPath(folder).parts)
    if parts <= LIMIT:
        return None
    return (f"PluginCheck is in a folder too deep for Docker Desktop: a server's folders would be {parts} levels deep ({folder}), and a container "
            f"that runs as a user, as Legacy and Torch do, can't read a mounted folder more than {LIMIT} levels deep. Move PluginCheck at least "
            f"{parts - LIMIT} level{'s' if parts - LIMIT > 1 else ''} higher.")
