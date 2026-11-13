"""What of PluginCheck's own code shapes a behaviour server's result, for the key a kept result is found by (behaviour.py, job_key).

Only that code is hashed: behaviour.py's functions and constants that a server's inputs reach, from ROOTS through everything they use, the
helper modules that build the world and the probe, and the probes' source. What decides which servers run, in what order and how their
records are compared is left out, so changing it keeps every saved result. The scenarios a server gets are in the key themselves.

A name ROOTS reach is hashed unless it is in DATA. A wrong DATA entry would let a changed server reuse a stale result, so
tests/test_cache_keys.py checks each is a value set while the stage runs, never code.
"""
import ast, hashlib, os
import classification

# Where a server's inputs come from: the server itself, the scenario it is given, the world it starts from and the Torch probe it loads.
ROOTS = ('serve', 'scenario_file', 'copy_world_files', 'build_torch_probe')
# Values set while the stage runs. What they hold reaches the key another way (the build's fingerprint, the target's options in job_key,
# the world and probe through ROOTS), or does not shape a result (folders, counters, locks and lending).
DATA = {'work', 'here', 'repo', 'target', 'run_dir', 'result_path', 'cache', 'counter', 'options', 'extra', 'magnetar_cache',
        'magnetar_cache_lock', 'slots', 'lending', 'running', 'running_lock'}
MODULES = (os.path.join('behaviour', 'fixture.py'), os.path.join('behaviour', 'programmable.py'), os.path.join('behaviour', 'drilling.py'),
           os.path.join('behaviour', 'extraplugins.py'), 'startlog.py')
FOLDERS = (os.path.join('behaviour', 'probe'), os.path.join('behaviour', 'torchprobe'))


def defined(tree):
    """behaviour.py's own top-level names: each function, and each name assigned, with every statement that defines it."""
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            found.setdefault(node.name, []).append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        found.setdefault(name.id, []).append(node)
    return found


NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def own_scope(node):
    """The nodes of a function's own scope: not those inside a function, lambda, class or comprehension within it."""
    todo = list(ast.iter_child_nodes(node))
    while todo:
        child = todo.pop()
        yield child
        if not isinstance(child, NESTED):
            todo += ast.iter_child_nodes(child)


def used(node):
    """The names a statement reads from the module: in a function, those it does not bind in its own scope or take as arguments, unless it
    declares them global. A name bound only in a nested scope still counts as read, which can only make the hash take in more."""
    loads = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    if not isinstance(node, ast.FunctionDef):
        return loads
    mine = list(own_scope(node))
    local = {n.id for n in mine if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
    local |= {a.arg for a in ast.walk(node.args) if isinstance(a, ast.arg)}
    local |= {n.name for n in mine if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    local -= {name for n in mine if isinstance(n, ast.Global) for name in n.names}
    return loads - local


def code(source):
    """{name: its source} for every top-level name of behaviour.py that ROOTS reach, apart from DATA."""
    tree = ast.parse(source)
    names = defined(tree)
    reached, todo = {}, [r for r in ROOTS if r in names]
    while todo:
        name = todo.pop()
        if name in reached or name in DATA:
            continue
        reached[name] = '\n'.join(ast.get_source_segment(source, node) for node in names[name])
        for node in names[name]:
            todo += [n for n in used(node) if n in names and n not in reached]
    return reached


def tool_hash(stages):
    """Of everything of ours that shapes a server, from the stages folder."""
    digest = hashlib.sha256()
    for name, text in sorted(code(open(os.path.join(stages, 'behaviour.py'), encoding='utf-8').read()).items()):
        digest.update(name.encode() + b'\0' + text.encode() + b'\0')
    for path in MODULES:
        digest.update(open(os.path.join(stages, path), 'rb').read())
    for folder in FOLDERS:
        digest.update(classification.folder_hash(os.path.join(stages, folder)).encode())
    return digest.hexdigest()[:16]
