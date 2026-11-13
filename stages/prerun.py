"""The two halves of a run split by PluginCheck's pre-container lane (README, Pre-container lane).

check.py --pre runs every stage before behaviour, and behaviour's discovery, on one slot of PluginCheck's own while another run's
behaviour goes. It writes pre.json, saying whether the rest may follow and what it was run on. check.py --continue <run dir> then runs
behaviour and logcheck in the same folder, and refuses when the first half did not end ready, or when anything it read has changed
since. Nothing is reused across runs: both halves are one run.
"""
import hashlib, json, os

SKIP = {'__pycache__', 'bin', 'obj', '.git', '.vs', '.claude'}


def tools_hash(here):
    """Of every file of PluginCheck's own stages, so a change to any of them between the halves is seen."""
    digest = hashlib.sha256()
    for folder, subfolders, files in os.walk(os.path.join(here, 'stages')):
        subfolders[:] = sorted(s for s in subfolders if s not in SKIP)
        for name in sorted(files):
            path = os.path.join(folder, name)
            digest.update(os.path.relpath(path, here).replace(os.sep, '/').encode() + b'\0')
            digest.update(open(path, 'rb').read())
    return digest.hexdigest()[:16]


def target_hash(target):
    return hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()[:16]


def readiness(stages, reading_exists):
    """Why the rest of the run should not follow, from the first half's stage outcomes. Empty when it may."""
    reasons = []
    for name in ('manifest', 'lint'):
        if stages.get(name, {}).get('status') == 'fail':
            reasons.append(f"{name} failed: {str(stages[name].get('reason', ''))[:200]}")
    behaviour = stages.get('behaviour')
    # A plugin with no Magnetar or Torch build has no behaviour stage, so there is nothing to discover.
    if behaviour is not None and behaviour.get('status') != 'not present':
        if not behaviour.get('discoverOnly') or behaviour.get('status') != 'pass':
            reasons.append(f"behaviour's discovery did not pass: {behaviour.get('status')} {str(behaviour.get('reason', ''))[:300]}")
        elif behaviour.get('commands') and not reading_exists:
            reasons.append('there is no current reading of the commands, so every command would get its own server. Write it first (Agent.md)')
    return reasons


def record(stages, reading_exists, inputs):
    """pre.json: ready or failed, why, and the inputs --continue checks."""
    reasons = readiness(stages, reading_exists)
    return {'status': 'failed' if reasons else 'ready', 'reasons': reasons, **inputs}


def problems(pre, inputs):
    """Why --continue must not run on this first half. Empty when it may."""
    if pre is None:
        return ['it has no pre.json, so it was not started with --pre']
    if pre.get('status') != 'ready':
        return ['its first half did not end ready: ' + '; '.join(pre.get('reasons') or [str(pre.get('status'))])]
    names = {'source': "the plugin's files", 'tools': "PluginCheck's stages", 'game': 'the game', 'target': 'the target'}
    changed = [names[key] for key in names if pre.get(key) != inputs.get(key)]
    return [f"{', '.join(changed)} changed since --pre. Run --pre again"] if changed else []
