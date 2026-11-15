"""Ship drills cutting an asteroid and a container to clean, for a target with "shipDrills": true in its behaviour section.

The drills sit inside the asteroid on a static grid, so they cut from the first tick. Blocks named watch-* are recorded with what they hold.
"""
import fixture as f
import programmable

ASTEROID = 'Asteroid128-001'
SIZE = 128


def block(kind, subtype, position, name, owner, extra=''):
    who = f.owned(owner) if owner else ''
    return f.block(kind, subtype, position, f'{who}<CustomName>{name}</CustomName><Enabled>true</Enabled>{extra}', f.Ids.new())


def asteroid(at):
    x, y, z = at
    return (f'<MyObjectBuilder_EntityBase xsi:type="MyObjectBuilder_VoxelMap"><SubtypeName /><EntityId>{f.Ids.new()}</EntityId><PersistentFlags>InScene CastShadows</PersistentFlags>'
            f'<PositionAndOrientation><Position x="{x}" y="{y}" z="{z}" /><Forward x="0" y="0" z="-1" /><Up x="0" y="1" z="0" /></PositionAndOrientation>'
            f'<StorageName>{ASTEROID}</StorageName><MutableStorage>true</MutableStorage></MyObjectBuilder_EntityBase>')


def objects():
    """The asteroid, the drills inside it, and in front of where the probe's caller stands, a container of theirs."""
    caller, recent, npc = programmable.CALLER, f.RECENT_PLAYER, f.NPC
    at = (f.ORIGIN[0], f.ORIGIN[1], f.ORIGIN[2] - 400)
    drills = [('watch-caller-tagged !ore', caller), ('watch-caller-plain', caller), ('watch-recent-tagged !ore', recent), ('watch-npc-tagged !ore', npc), ('watch-unowned-tagged !ore', None)]
    parts = [block('Drill', 'LargeBlockDrill', (i * 4, 0, 0), name, owner) for i, (name, owner) in enumerate(drills)]
    parts += [f.armor((x, 1, 3)) for x in range(len(drills) * 4)] + [programmable.battery((0, 1, 4))]
    centre = tuple(v + SIZE / 2 for v in at)

    # The probe's caller stands at x 1000, y 1400, z 1060, facing -z.
    plate = [f.armor((x, y, 0)) for x in range(-3, 4) for y in range(-3, 4) if (x, y) not in ((0, 1), (0, -1))]
    plate.append(block('CargoContainer', 'LargeBlockSmallContainer', (0, 1, 0), 'watch-cargo', caller))
    # Commands that need the caller in a cockpit get them seated by probe:sit seat-cockpit.
    plate.append(block('Cockpit', 'LargeBlockCockpit', (0, -1, 0), 'seat-cockpit', caller))
    return [asteroid(at), f.grid('drill-rig', centre, parts, static=True), f.grid('clean-target', (1000.0, 1400.0, 1035.0), plate, static=True)]
