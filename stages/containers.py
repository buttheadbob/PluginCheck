"""Docker calls with time limits, and servers that get their files by docker cp instead of mounted folders.

Shared by the behaviour stage and the single-server stages (magnetar, legacy, torch, quasar, quasar_managed).

When Docker Desktop's file sharing stalled, a server holding mounted Windows folders waited on them in state D for good, could not be
removed, and every docker call waited with it. So a server is made with docker create, its files are copied in as one tar (hand_in),
and then it starts (start_server). Every call through docker() has a time limit. A call that runs out gives TIMED_OUT and says so on
stderr. A copy in or out is tried COPY_TRIES times (copy).
"""
import contextlib, os, subprocess, tarfile, time

DOCKER_SECONDS = 120
TIMED_OUT = 124
COPY_SECONDS, COPY_TRIES = 120, 2
BUILD_SECONDS = 30 * 60
# A look inside a running server (docker exec), and stopping one.
LOOK_SECONDS = 30
STOP_SECONDS = 90


def docker(*args, timeout=DOCKER_SECONDS, stdin=None, env=None):
    try:
        return subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, stdin=stdin, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(['docker', *args], TIMED_OUT, '', f'docker {args[0]} gave no answer in {timeout}s')


def docker_long(*args):
    """docker for a call that can take minutes: a build in a container, or a run whose image may have to be pulled first."""
    return docker(*args, timeout=BUILD_SECONDS)


def copy(*args, archive=None):
    """docker cp, tried COPY_TRIES times with COPY_SECONDS each, from the tar file archive on stdin when given. Returns None, or why it
    failed. A path or container that does not exist is an answer, so it is not tried again."""
    for attempt in range(COPY_TRIES):
        with open(archive, 'rb') if archive else contextlib.nullcontext() as given:
            done = docker('cp', *args, timeout=COPY_SECONDS, stdin=given)
        if done.returncode == 0:
            return None
        why = done.stderr.strip()[-300:] or f'exit code {done.returncode}'
        if 'Could not find the file' in why or 'No such container' in why:
            break
        if attempt + 1 < COPY_TRIES:
            time.sleep(2)
    return f"docker cp {' '.join(a for a in args if a != '-')} failed: {why}"


def readable(info):
    """A file handed in as the read-only mounts gave it before: owned by root, as docker cp makes it anyway, readable by everyone and
    writable by root alone. Torch and Legacy run as the user server, and only read these."""
    info.uid = info.gid = 0
    info.uname = info.gname = 'root'
    info.mode = 0o755 if info.isdir() else 0o644
    return info


def hand_in(container, folder, files):
    """Copies files into a created container, as one tar through docker cp: files maps a file or folder here to a path in the
    container. The folders above each path are in the tar too, so the copy needs nothing of the image. Returns None, or why it failed."""
    # One tar per container: stages that share a run folder hand in at the same time (check.py runs magnetar, torch and quasar side by
    # side), and a shared name let one stage delete, or send, another's tar.
    archive = os.path.join(folder, f'hand-in-{container}.tar')
    began = time.time()
    try:
        with tarfile.open(archive, 'w') as tar:
            above = sorted({'/'.join(p[:i]) for p in (inside.strip('/').split('/') for inside in files.values()) for i in range(1, len(p))})
            for name in above:
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                tar.addfile(readable(info))
            for source, inside in files.items():
                tar.add(source, arcname=inside.strip('/'), filter=readable)
        built, size = time.time(), os.path.getsize(archive)
        why = copy('-', f'{container}:/', archive=archive)
        # Measuring only, for what a server's start costs (README, Behaviour).
        print(f'  hand-in {container}: {size / 2 ** 20:.1f} MB, tar {built - began:.1f}s, docker cp {time.time() - built:.1f}s', flush=True)
        return why
    finally:
        # Also when the tar could not be built or sent: none is left behind in the folder.
        with contextlib.suppress(FileNotFoundError):
            os.remove(archive)


def start_server(container, create, folder, files, env=None, replace=()):
    """docker create, the server's files copied in (hand_in), then docker start. A running server holds no folder of this computer: when
    Docker Desktop's file sharing stalled, servers that did waited on it for good, and could not be removed. replace lists (file here, path
    in the container) for a file of the image to replace, copied on its own so the folders above it keep the image's owner and mode.
    Returns None, or why the server did not start."""
    made = docker('create', '--name', container, *create, env=env)
    if made.returncode:
        return f'docker create failed: {made.stderr.strip()[-300:]}'
    why = hand_in(container, folder, files)
    for here, inside in replace:
        why = why or copy(here, f'{container}:{inside}')
    if why:
        return f'its files could not be copied in: {why}'
    started = docker('start', container)
    return f'docker start failed: {started.stderr.strip()[-300:]}' if started.returncode else None
