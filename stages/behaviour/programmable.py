"""Programmable blocks for the behaviour fixture, for a target with "programmableBlocks": true in its behaviour section.

Each is on a static grid of its own out in space with a battery, so nothing else in the world changes. The run times are targets the scripts tune
themselves to, because a loop size that takes 1 ms on one host takes something else on another. Each block's custom name says what it is for.
"""
import fixture as f
from xml.sax.saxutils import escape

# Not in the Empty World. Saved so that blocks can belong to them from the first tick: handing a block over later ends its script in Survival.
CALLER = 144115188075855898
CALLER_STEAM = 76561190000000003
COMBINED = 144115188075855899
COMBINED_STEAM = 76561190000000005
ROW_Z = 300.0

# Scans a string a tuned number of times: it allocates nothing, so a collection does not hide the run from a plugin that skips those.
TUNED = '''
string text = new string('x', 200000);
double loops = 10;
int runs;
bool tune;
const double Target = %TARGET%;
public Program() { Runtime.UpdateFrequency = UpdateFrequency.%FREQUENCY%; }
public void Main(string argument, UpdateType updateSource)
{
    if (argument.StartsWith("GracefulShutDown::")) { Me.CustomData = argument; tune = false; return; }
    double last = Runtime.LastRunTimeMs;
    if (tune && last > 0) loops = Math.Max(1, Math.Min(5000, loops * Math.Max(0.5, Math.Min(2, (%NOW%) / last))));
    bool spike = ++runs % 40 == 0;
    tune = !spike;
    int count = (int)loops * (spike ? %SPIKE% : 1);
    int found = 0;
    for (int i = 0; i < count; i++) found += text.IndexOf('y');
    Echo(count.ToString());
}
'''
MEMORY = '''
byte[] held = new byte[11000000];
public Program() { Runtime.UpdateFrequency = UpdateFrequency.Update10; held[5] = 1; }
public void Main(string argument, UpdateType updateSource) { Echo(held.Length.ToString()); }
'''
HOSTILE = '''
List<object> self = new List<object>();
Action chain;
public Program()
{
    self.Add(self);
    chain = () => self.Add(chain);
    for (int i = 0; i < 2000; i++) { var next = new List<object>(); next.Add(self); self = next; }
    Runtime.UpdateFrequency = UpdateFrequency.Update10;
}
public void Main(string argument, UpdateType updateSource) { Echo(self.Count.ToString()); }
'''
FAULT = '''
int runs;
public Program() { Runtime.UpdateFrequency = UpdateFrequency.Update10; }
public void Main(string argument, UpdateType updateSource) { if (++runs == 3) throw new Exception("planned fault"); }
'''
SELF_ON = '''
public Program() { Me.Enabled = true; Runtime.UpdateFrequency = UpdateFrequency.Update100; }
public void Save() { Me.Enabled = true; Storage = "saved"; }
public void Main(string argument, UpdateType updateSource) { Echo("running"); }
'''
ANSWERS = '''
public Program() { Runtime.UpdateFrequency = UpdateFrequency.Update100; }
public void Main(string argument, UpdateType updateSource) { if (argument.Length > 0) Me.CustomData = argument; }
'''
# Writes its Storage on every run, so what the block holds after a save or a recompile shows whether that Storage reached it.
STORAGE = '''
int runs;
public Program() { Runtime.UpdateFrequency = UpdateFrequency.Update100; }
public void Main(string argument, UpdateType updateSource) { Storage = "run" + (++runs); }
'''
COUNTS = '''
int runs;
public Program() { Runtime.UpdateFrequency = UpdateFrequency.%FREQUENCY%; }
public void Main(string argument, UpdateType updateSource) { runs++; Me.CustomData = runs > 50 ? "ran over 50 times" : ""; }
'''


def tuned(target, frequency='Update10', now='Target', spike=1):
    return TUNED.replace('%TARGET%', str(target)).replace('%FREQUENCY%', frequency).replace('%NOW%', now).replace('%SPIKE%', str(spike))


LIGHT = tuned(0.05)
SLOW = tuned(1.5)
INSTANT = tuned(12, now='Runtime.LifetimeTicks < 5400 ? 0.1 : Target')
SHARE = tuned(0.6)


def block(name, position, owner, program, enabled):
    who = f.owned(owner) if owner else ''
    extra = f'{who}<CustomName>{name}</CustomName><Enabled>{str(enabled).lower()}</Enabled><Program>{escape(program)}</Program>'
    return f.block('MyProgrammableBlock', 'LargeProgrammableBlock', position, extra, f.Ids.new())


def battery(position):
    extra = '<Enabled>true</Enabled><CurrentStoredPower>3</CurrentStoredPower><ProducerEnabled>true</ProducerEnabled><MaxStoredPower>3</MaxStoredPower>'
    return f.block('BatteryBlock', 'LargeBlockBatteryBlock', position, extra, f.Ids.new())


def grid(name, at, *blocks):
    """(block name, owner, script, on)"""
    parts = [f.armor((x, 0, 0)) for x in range(len(blocks) + 2)] + [battery((0, 1, 0))]
    parts += [block(n, (i + 1, 1, 0), owner, program, on) for i, (n, owner, program, on) in enumerate(blocks)]
    return f.grid(name, at, parts, static=True)


def zone(name, at, allowed):
    x, y, z = at
    return (f'<MyObjectBuilder_EntityBase xsi:type="MyObjectBuilder_SafeZone"><SubtypeName /><EntityId>{f.Ids.new()}</EntityId><PersistentFlags>InScene</PersistentFlags>'
            f'<PositionAndOrientation><Position x="{x}" y="{y}" z="{z}" /><Forward x="0" y="0" z="-1" /><Up x="0" y="1" z="0" /></PositionAndOrientation>'
            f'<Radius>30</Radius><Enabled>true</Enabled><AllowedActions>{allowed}</AllowedActions><Shape>Sphere</Shape><DisplayName>{name}</DisplayName></MyObjectBuilder_EntityBase>')


def objects():
    """Every grid and safe zone, as sector objects."""
    player, recent, npc = f.OLD_PLAYER, f.RECENT_PLAYER, f.NPC

    def at(row):
        return (f.ORIGIN[0], f.ORIGIN[1], f.ORIGIN[2] + ROW_Z + row * f.SPACING)

    return [
        grid('pb-light', at(0), ('pb-light', player, LIGHT, True), ('pb-answers', player, ANSWERS, True)),
        grid('pb-caller', at(1), ('pb-caller-light', CALLER, LIGHT, True), ('pb-caller-slow', CALLER, SLOW, True)),
        grid('pb-single', at(2), ('pb-single', player, SLOW, True)),
        grid('pb-average', at(3), ('pb-average', recent, tuned(0.6, 'Update1'), True),
             ('pb-outlier', recent, tuned(0.1, 'Update1', spike=20), True)),
        grid('pb-instant', at(4), ('pb-instant', player, INSTANT, True)),
        grid('pb-memory', at(5), ('pb-memory', player, MEMORY, True)),
        grid('pb-hostile', at(6), ('pb-hostile', recent, HOSTILE, True)),
        grid('pb-combined', at(7), *[(f'pb-combined-{i}', COMBINED, SHARE, True) for i in range(1, 7)]),
        grid('pb-npc', at(8), ('pb-npc', npc, LIGHT, True), ('pb-npc-off', npc, SELF_ON, False)),
        grid('pb-unowned', at(9), ('pb-unowned', None, LIGHT, True)),
        grid('pb-off', at(10), ('pb-off', player, SELF_ON, False)),
        grid('pb-fault', at(11), ('pb-fault', player, FAULT, True)),
        grid('pb-timed', at(12), *[(f'pb-{frequency.lower()}', recent, COUNTS.replace('%FREQUENCY%', frequency), True) for frequency in ('Update1', 'Update10', 'Update100')]),
        grid('pb-zone-safe', at(13), ('pb-zone-slow', player, SLOW, True), ('pb-zone-instant', player, INSTANT, True)),
        zone('zone-safe', at(13), 'Shooting Drilling Welding Grinding'),
        grid('pb-zone-open', at(14), ('pb-zone-open', player, SLOW, True)),
        zone('zone-open', at(14), 'Damage Shooting Drilling Welding Grinding'),
        grid('pb-storage', at(15), ('pb-storage', player, STORAGE, True)),
    ]


PLAYERS = ((CALLER, CALLER_STEAM, 'ProbeCaller'), (COMBINED, COMBINED_STEAM, 'FixtureCombined'))
