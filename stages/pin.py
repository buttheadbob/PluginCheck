"""Pins a stage's containers to the cores of PluginCheck's pre-container lane (check.py --pre), so nothing the lane starts runs on the
cores a behaviour run measures on. Outside the lane PLUGINCHECK_CPUSET is unset, and containers are pinned to every core Docker has but the
user's own (leases.USER_RESERVED_CORES), or not at all when none of those is reserved. Every container started takes *args() after 'run',
and tests/test_lane.py fails on one that doesn't."""
import os, subprocess

NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
_docker_cores = []


def docker_cores():
    """The logical cores Docker can use: docker info's NCPU, which behaviour's capacity() makes its slots from too. Docker Desktop can have
    fewer than this computer, and Docker refuses a cpuset that names a core it does not have. Asked once per process; None when Docker
    gives no answer, and leases then counts this computer's cores."""
    if not _docker_cores:
        try:
            said = subprocess.run(['docker', 'info', '--format', '{{.NCPU}}'], capture_output=True, text=True, timeout=120, **NO_WINDOW)
            _docker_cores.append(int(said.stdout.strip()))
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    return _docker_cores[0]


def args():
    # leases is looked up when called, so a stage replay's stand-in for it (tests/replay.py) is the one asked.
    import leases
    cores = os.environ.get('PLUGINCHECK_CPUSET')
    if not cores and leases.reserved_cores():
        cores = leases.unreserved_cpuset(docker_cores())
    return ['--cpuset-cpus', cores] if cores else []
