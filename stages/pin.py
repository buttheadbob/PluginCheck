"""Pins a stage's containers to the cores of PluginCheck's pre-container lane (check.py --pre), so nothing the lane starts runs on the
cores a behaviour run measures on. Outside the lane PLUGINCHECK_CPUSET is unset and nothing is pinned. Every container started takes
*args() after 'run', and tests/test_lane.py fails on one that doesn't."""
import os


def args():
    cores = os.environ.get('PLUGINCHECK_CPUSET')
    return ['--cpuset-cpus', cores] if cores else []
