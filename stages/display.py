"""What a person sees while check.py runs: a live colour panel at the top of the terminal, with results scrolling underneath it.

It uses only terminal escape codes, so PluginCheck still needs nothing but Python and Docker. When the output is not a real terminal, such
as a log, a pipe or an AI reading the run, it prints the same plain lines as ever. PLUGINCHECK_PLAIN=1 asks for that on a terminal too.

A stage sends status with lines starting @@, holding JSON. They drive the panel and never reach the logs.
"""
import atexit, json, os, re, shutil, subprocess, sys, threading, time

STATUS_MARK = '@@'
RESET = '\x1b[0m'
BOLD = '\x1b[1m'
DIM = '\x1b[2m'
RED = '\x1b[91m'
GREEN = '\x1b[92m'
YELLOW = '\x1b[93m'
BLUE = '\x1b[94m'
MAGENTA = '\x1b[95m'
CYAN = '\x1b[96m'
WHITE = '\x1b[97m'
TITLE = '\x1b[1;97;44m'
STATUS_COLOURS = {'pass': GREEN, 'fail': RED, 'review': YELLOW, 'not run': DIM, 'not present': DIM, 'unable to verify': MAGENTA}
STATUS_MARKS = {'pass': '+', 'fail': 'x', 'review': '?', 'not run': '-', 'not present': '-', 'unable to verify': '!'}
COUNTED = (('DIFF', re.compile(r'^DIFF\b'), YELLOW), ('UNSTABLE', re.compile(r'^UNSTABLE\b'), MAGENTA), ('FAILED', re.compile(r'^FAILED\b'), RED),
           ('WARN', re.compile(r'^WARN\b'), YELLOW), ('LEAD', re.compile(r'^LEAD\b'), CYAN), ('THREAD', re.compile(r'^THREAD-SENSITIVE\b'), CYAN))
LINE_COLOURS = ((re.compile(r'^=====.*'), BOLD + BLUE), (re.compile(r'^(DIFF|WARN)\b'), YELLOW), (re.compile(r'^UNSTABLE\b'), MAGENTA),
                (re.compile(r'^(FAILED|FAIL|Traceback|.*\bfail in\b)'), RED), (re.compile(r'^(LEAD|THREAD-SENSITIVE|CHECK)\b'), CYAN),
                (re.compile(r'^\[\d+/\d+ '), DIM), (re.compile(r'^\s+\.\.\. '), DIM), (re.compile(r'^\S+: pass in\b'), GREEN),
                (re.compile(r'^\S+: review in\b'), YELLOW), (re.compile(r'^\S+: (not run|unable to verify)\b'), MAGENTA))
SERVER_ROWS = 8
ROOM_FOR_SERVERS = 30


def clock(seconds):
    seconds = max(int(seconds), 0)
    return f'{seconds // 3600}h{seconds % 3600 // 60:02}m' if seconds >= 3600 else f'{seconds // 60}m{seconds % 60:02}s'


def bar(done, total, width, colour=GREEN):
    filled = round(width * done / total) if total else 0
    return f'{colour}{"█" * filled}{DIM}{"░" * (width - filled)}{RESET}'


def fit(parts, width):
    """Joins (text, colour) pieces, cut to the width. Measured on the text, because escape codes take no room."""
    out, used = [], 0
    for text, colour in parts:
        text = text[:max(width - used, 0)]
        used += len(text)
        out.append(f'{colour}{text}{RESET}' if colour else text)
    return ''.join(out)


def can_draw():
    if os.environ.get('PLUGINCHECK_PLAIN') or os.environ.get('TERM') == 'dumb' or not sys.stdout.isatty():
        return False
    if os.name != 'nt':
        return True
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.SetConsoleOutputCP(65001)
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)) and kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (OSError, AttributeError):
        return False


class Display:
    def __init__(self, target, planned, past, time_left):
        self.target, self.planned, self.past, self.time_left = target, planned, past, time_left
        self.fancy = can_draw()
        self.lock = threading.RLock()
        self.begun = time.time()
        self.statuses = {}
        self.running = []
        self.counts = {name: 0 for name, _, _ in COUNTED}
        self.progress = None
        self.servers = {}
        self.containers = []
        self.height = 0
        self.log_row = 1
        self.size = (0, 0)
        self.closed = False
        if self.fancy:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            atexit.register(self.close)
            self.layout()
            threading.Thread(target=self.tick, daemon=True).start()
            threading.Thread(target=self.watch_docker, daemon=True).start()

    def watch_docker(self):
        """Docker is the truth about what is running. Every stage's containers count, not just the ones a stage reports."""
        while not self.closed:
            try:
                listed = subprocess.run(['docker', 'ps', '--filter', 'name=plugincheck-', '--format', '{{.Names}}|{{.Status}}'],
                                        capture_output=True, text=True, timeout=10).stdout.splitlines()
            except (OSError, subprocess.SubprocessError):
                listed = []
            with self.lock:
                self.containers = [line.split('|', 1) for line in listed if '|' in line]
            time.sleep(3)

    # ---- what happens during a run ----

    def stage_started(self, stage, text):
        with self.lock:
            self.running.append(stage)
            self.line(text)

    def stage_finished(self, stage, status, text):
        with self.lock:
            self.statuses[stage] = status
            self.running.remove(stage)
            self.line(text)

    def heartbeat(self, text):
        # The panel already shows that a stage is alive.
        if not self.fancy:
            print(text, flush=True)

    def status(self, raw):
        """A @@ line from a stage."""
        try:
            event = json.loads(raw[len(STATUS_MARK):])
        except ValueError:
            return None
        with self.lock:
            kind = event.get('kind')
            if kind == 'server':
                self.servers[event['cores']] = {**event, 'since': time.time()}
            elif kind == 'done':
                self.servers.pop(event['cores'], None)
            elif kind == 'progress':
                self.progress = {**event, 'at': time.time()}
        return event

    def line(self, text, stage=None):
        """A line of output. Stages run side by side, so each line says whose it is."""
        with self.lock:
            text = text.rstrip('\n')
            for name, rule, _ in COUNTED:
                if rule.match(text):
                    self.counts[name] += 1
            label = f'[{stage}] ' if stage else ''
            if not self.fancy:
                print(label + text, flush=True)
                return
            colour = next((c for rule, c in LINE_COLOURS if rule.match(text)), '')
            self.write(f'{DIM}{label}{RESET}{colour}{text}{RESET}', len(label) + len(text))
            self.draw()

    def write(self, text, length):
        """Results start right under the panel and fill downwards, and only scroll once they reach the bottom."""
        columns, bottom = self.size
        rows = max((length - 1) // columns + 1, 1)
        sys.stdout.write(f'\x1b[{self.log_row};1H{text}')
        if self.log_row + rows > bottom:
            sys.stdout.write('\n')
        self.log_row = min(self.log_row + rows, bottom)
        sys.stdout.flush()

    def close(self):
        with self.lock:
            if self.closed or not self.fancy:
                return
            self.closed = True
            self.draw()
            sys.stdout.write(f'\x1b[r\x1b[?25h\x1b[{self.log_row};1H\n')
            sys.stdout.flush()

    def summary(self, stages, path):
        self.close()
        for stage, outcome in stages.items():
            status = outcome['status']
            reason = f" - {outcome['reason']}" if outcome.get('reason') else ''
            colour = STATUS_COLOURS.get(status, '') if self.fancy else ''
            print(f"{stage:10} {colour}{status}{RESET if colour else ''}{reason}")
        print(path)

    # ---- drawing ----

    def tick(self):
        while not self.closed:
            time.sleep(1)
            with self.lock:
                if not self.closed:
                    self.draw()

    def layout(self):
        columns, rows = shutil.get_terminal_size((100, 30))
        self.size = (columns, rows)
        self.height = len(self.panel(columns - 1))
        self.log_row = self.height + 1
        sys.stdout.write(f'\x1b[2J\x1b[{self.height + 1};{rows}r\x1b[?25l')
        sys.stdout.flush()

    def panel(self, width):
        finished = sum(1 for s in self.planned if s in self.statuses)
        rows = [fit([(f' PluginCheck  {self.target} ', TITLE), (f'  {clock(time.time() - self.begun)} elapsed', WHITE), (f'   about {clock(self.time_left())} left', BOLD + CYAN)], width)]
        rows.append(f'{fit([("Stages  ", BOLD)], 8)}{bar(finished, len(self.planned), max(width - 26, 10))} {finished}/{len(self.planned)}  {len(self.running)} running')
        rows += self.chips(width)
        rows.append(self.server_bar(width))
        if self.size[1] >= ROOM_FOR_SERVERS:
            rows += self.server_rows(width)
        pieces = [('Results ', BOLD)]
        for status in ('pass', 'review', 'fail'):
            pieces.append((f'{sum(1 for v in self.statuses.values() if v == status)} {status}  ', STATUS_COLOURS[status]))
        for name, _, colour in COUNTED:
            pieces.append((f'{self.counts[name]} {name}  ', colour if self.counts[name] else DIM))
        rows.append(fit(pieces, width))
        rows.append(f'{DIM}{"─" * width}{RESET}')
        return rows

    def chips(self, width):
        """Every stage by name, in the colour of how it ended, on as many rows as it takes."""
        rows, pieces, used = [], [], 0
        for stage in self.planned:
            status = self.statuses.get(stage)
            running = stage in self.running
            mark = STATUS_MARKS.get(status, '>' if running else ' ')
            colour = STATUS_COLOURS.get(status, BOLD + BLUE if running else DIM)
            text = f'{mark} {stage}  '
            if used + len(text) > width:
                rows.append(fit(pieces, width))
                pieces, used = [], 0
            pieces.append((text, colour))
            used += len(text)
        rows.append(fit(pieces, width))
        # Two rows always, so the panel keeps its height as stages finish.
        return rows + [''] * (2 - len(rows))

    def server_bar(self, width):
        if not self.progress:
            count = len(self.containers)
            return fit([('Servers ', BOLD), (f'{count} running' if count else 'none running', CYAN if count else DIM)], width)
        done, total, failed = self.progress['done'], self.progress['total'], self.progress.get('failed', 0)
        left = max(self.progress['left'] - (time.time() - self.progress['at']), 0)
        tail = f' {done}/{total}  {len(self.servers)} running  {failed} failed  {clock(left)} left'
        return f'{fit([("Servers ", BOLD)], 8)}{bar(done, total, max(width - 8 - len(tail), 10), RED if failed else CYAN)}{tail}'

    def server_rows(self, width):
        rows = []
        if not self.servers:
            for name, state in self.containers[:SERVER_ROWS]:
                rows.append(fit([(f'  {name[len("plugincheck-"):]:34}', GREEN), (state, WHITE)], width))
        for cores, server in sorted(self.servers.items(), key=lambda s: int(s[0].split('-')[0]))[:SERVER_ROWS]:
            spent = clock(time.time() - server['since'])
            rows.append(fit([(f'  {cores:>6}  ', DIM), (f"{server['build']:18}", GREEN if server['build'].startswith('working') else MAGENTA), (f'{spent:>7}  ', WHITE), (server['scenario'], '')], width))
        return rows + [''] * (SERVER_ROWS - len(rows))

    def draw(self):
        if not self.fancy:
            return
        columns, rows = shutil.get_terminal_size((100, 30))
        if (columns, rows) != self.size:
            self.layout()
        # One short of the edge: a row that fills the last column makes a terminal wrap it.
        panel = self.panel(columns - 1)
        out = ['\x1b7'] + [f'\x1b[{i + 1};1H{row}\x1b[K' for i, row in enumerate(panel)] + ['\x1b8']
        sys.stdout.write(''.join(out))
        sys.stdout.flush()
