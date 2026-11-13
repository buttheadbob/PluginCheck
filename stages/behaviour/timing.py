"""Where one behaviour server's time went, as one line in behaviour.log and a "timing" field on its records.

behaviour.py notes the host clock at each point: queued (the job was handed to the pool), slot (it got its cores), up (the container
started), stopped (the record appeared, or the server ended without one), killed, freed (the cores went back to the run or to a
borrower, right after the kill) and cleaned (logs copied, container and volume removed). The probe adds "probeTiming": the wall seconds
from the game being ready to the record, and the game ticks in that time, so sim speed is game seconds per wall second (1.0 is full speed).

Game ready is worked out from the probe's own span, back from the moment the record was seen, so the container's clock never has to agree
with the host's. The record is seen within half a second of being written when the probe's copy reaches the mounted marker folder, and
within behaviour.py's CHECK_SECONDS when only Docker finds it.

Nothing here changes what a server does or what is compared: compare() reads only "after", "before", "replies" and the like.
"""

POINTS = ('up', 'stopped', 'killed', 'freed', 'cleaned')
# Measured for one server on one run. A kept result is reused by a later run, where these would read as that run's own.
FIELDS = ('timing', 'probeTiming')


def without(records):
    """Copies of the records to keep in the results cache, without this run's timing."""
    return [{k: v for k, v in r.items() if k not in FIELDS} for r in records]


def summary(timing, probe_timing=None):
    """The line for behaviour.log and the dict for the records, from the host clock points and the probe's own timing (or None).
    Seconds count from the moment the server got its slot; 'waitedSeconds' is the time before that."""
    slot = timing.get('slot')
    if slot is None:
        return None, {}
    out = {'waitedSeconds': round(slot - timing['queued'], 1) if timing.get('queued') else None}
    for point in POINTS:
        if timing.get(point) is not None:
            out[point + 'Seconds'] = round(timing[point] - slot, 1)
    ready = None
    if probe_timing and timing.get('stopped') is not None and isinstance(probe_timing.get('readySeconds'), (int, float)):
        ready = round(timing['stopped'] - slot - probe_timing['readySeconds'], 1)
        out['readySeconds'] = ready
    speed = probe_timing.get('simSpeed') if probe_timing else None
    if isinstance(speed, (int, float)):
        out['simSpeed'] = round(speed, 3)
    out['state'] = timing.get('state')

    parts = []
    if out['waitedSeconds'] is not None:
        parts.append(f"waited {out['waitedSeconds']:g}s for a slot")
    if 'upSeconds' in out:
        parts.append(f"up {out['upSeconds']:g}s")
    if ready is not None:
        parts.append(f'game ready {ready:g}s')
    if 'stoppedSeconds' in out:
        parts.append(f"{timing.get('state') or 'stopped'} {out['stoppedSeconds']:g}s")
    for point, label in (('killed', 'killed'), ('freed', 'slot freed'), ('cleaned', 'cleaned up')):
        if point + 'Seconds' in out:
            parts.append(f"{label} {out[point + 'Seconds']:g}s")
    if 'simSpeed' in out:
        parts.append(f"sim speed {out['simSpeed']:g}")
    line = f"  timing {timing.get('server', '?')} on {timing.get('cores', '?')}: " + ', '.join(parts)
    return line, out
