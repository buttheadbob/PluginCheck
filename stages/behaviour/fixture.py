"""Generates the behaviour fixture world from the game's own saves, so it is plain text and rebuilt after a game update.

The world is general, and shaped around no one plugin: grids of each size and kind of owner, with subgrids on rotors, wheels, landing
gear and connectors, plus a moon for gravity. The ownerless subgrids are in it because MyCubeGrid.BigOwners gives one its parent's owners
on the game thread and an empty list elsewhere, unless it connects through a landing gear or a connector.
"""
import datetime, os, re, shutil

OLD_PLAYER = 144115188075855895  # The Empty World's own identity, renamed, so the save stays consistent.
OLD_PLAYER_STEAM = 76561190000000001
RECENT_PLAYER = 144115188075855897
RECENT_PLAYER_STEAM = 76561190000000002
NPC = 144115188075855896  # The Empty World's pirates.
ORIGIN = (1000.0, 1000.0, 1000.0)
SPACING = 80.0
LARGE_BLOCK = 2.5


class Ids:
    next = 510000000000000000

    @classmethod
    def new(cls):
        cls.next += 1
        return cls.next


def block(kind, subtype, position, extra='', entity=None, forward='Forward', up='Up'):
    entity_id = f'<EntityId>{entity}</EntityId>' if entity else ''
    x, y, z = position
    return (f'<MyObjectBuilder_CubeBlock xsi:type="MyObjectBuilder_{kind}"><SubtypeName>{subtype}</SubtypeName>{entity_id}'
            f'<Min x="{x}" y="{y}" z="{z}" /><BlockOrientation Forward="{forward}" Up="{up}" />{extra}</MyObjectBuilder_CubeBlock>')


def armor(position, size='Large'):
    return block('CubeBlock', f'{size}BlockArmorBlock', position)


def owned(owner):
    return f'<Owner>{owner}</Owner><BuiltBy>{owner}</BuiltBy><ShareMode>None</ShareMode>'


def cargo(position, owner, size='Large'):
    return block('CargoContainer', f'{size}BlockSmallContainer', position, owned(owner), Ids.new())


def grid(name, position, blocks, size='Large', static=False, entity=None):
    x, y, z = position
    return (f'<MyObjectBuilder_EntityBase xsi:type="MyObjectBuilder_CubeGrid"><SubtypeName /><EntityId>{entity or Ids.new()}</EntityId>'
            '<PersistentFlags>CastShadows InScene</PersistentFlags><PositionAndOrientation>'
            f'<Position x="{x}" y="{y}" z="{z}" /><Forward x="0" y="0" z="-1" /><Up x="0" y="1" z="0" /></PositionAndOrientation>'
            f'<GridSizeEnum>{size}</GridSizeEnum><CubeBlocks>{"".join(blocks)}</CubeBlocks><IsStatic>{str(static).lower()}</IsStatic>'
            f'<DisplayName>{name}</DisplayName><DestructibleBlocks>true</DestructibleBlocks></MyObjectBuilder_EntityBase>')


def hull(size='Large'):
    return [armor((x, 0, 0), size) for x in range(3)] + [armor((0, 1, 0), size), armor((2, 1, 0), size)]


def plain(name, at, owner=None, size='Large', static=False):
    blocks = hull(size)
    if owner:
        blocks.append(cargo((1, 0, 1), owner, size))
    return [grid(name, at, blocks, size, static)]


def rotor_pair(parent_name, sub_name, at, owner, sub_owner=None):
    """A parent grid with a player's container and rotor, and a subgrid on the rotor."""
    stator, head = Ids.new(), Ids.new()
    extra = f'{owned(owner)}<Enabled>true</Enabled><TopBlockId>{head}</TopBlockId><IsWelded>false</IsWelded>'
    parent = hull() + [cargo((0, 0, 1), owner), block('MotorStator', 'LargeStator', (1, 1, 0), extra, stator)]
    sub = [block('MotorRotor', 'LargeRotor', (0, 0, 0), f'<ParentEntityId>{stator}</ParentEntityId><YieldLastComponent>false</YieldLastComponent>', head),
           armor((0, 1, 0))]
    if sub_owner:
        sub.append(cargo((0, 2, 0), sub_owner))
    head_at = (at[0] + LARGE_BLOCK, at[1] + 2 * LARGE_BLOCK, at[2])
    return [grid(parent_name, at, parent), grid(sub_name, head_at, sub)]


def wheel_pair(parent_name, sub_name, at, owner):
    """A player's grid with a wheel, whose grid has no owned blocks. To the game this is a rotor, and to a plugin it should not be."""
    suspension, wheel = Ids.new(), Ids.new()
    extra = f'{owned(owner)}<Enabled>true</Enabled><TopBlockId>{wheel}</TopBlockId><IsWelded>false</IsWelded>'
    parent = hull() + [cargo((0, 0, 1), owner), block('MotorSuspension', 'Suspension1x1', (1, 1, 0), extra, suspension)]
    tyre = [block('Wheel', 'RealWheel1x1', (0, 0, 0), f'<ParentEntityId>{suspension}</ParentEntityId><YieldLastComponent>false</YieldLastComponent>', wheel)]
    return [grid(parent_name, at, parent), grid(sub_name, (at[0] + LARGE_BLOCK, at[1] + 2 * LARGE_BLOCK, at[2]), tyre)]


def gear_pair(parent_name, sub_name, at, owner):
    """A player's grid held by a locked landing gear to a grid with no owned blocks."""
    sub_id = Ids.new()
    gear = (f'{owned(owner)}<Enabled>true</Enabled><IsLocked>true</IsLocked><AttachedEntityId>{sub_id}</AttachedEntityId>'
            '<LockMode>Locked</LockMode>')
    parent = hull() + [cargo((0, 0, 1), owner), block('LandingGear', 'LargeBlockLandingGear', (1, 1, 0), gear, Ids.new(), 'Forward', 'Down')]
    sub_at = (at[0] + LARGE_BLOCK, at[1] + 2 * LARGE_BLOCK, at[2])
    return [grid(parent_name, at, parent), grid(sub_name, sub_at, [armor((0, 0, 0)), armor((0, 1, 0))], entity=sub_id)]


def connector_pair(parent_name, sub_name, at, owner):
    """A player's grid docked by connectors to a grid with no owned blocks."""
    near, far = Ids.new(), Ids.new()
    state = '<Enabled>true</Enabled><Connected>true</Connected><ConnectedEntityId>{}</ConnectedEntityId>'
    parent = hull() + [cargo((0, 0, 1), owner), block('ShipConnector', 'Connector', (1, 1, 0), owned(owner) + state.format(far), near, 'Up', 'Forward')]
    sub_at = (at[0] + LARGE_BLOCK, at[1] + 2 * LARGE_BLOCK, at[2])
    sub = [block('ShipConnector', 'Connector', (0, 0, 0), state.format(near), far, 'Down', 'Forward'), armor((0, 1, 0))]
    return [grid(parent_name, at, parent), grid(sub_name, sub_at, sub)]


def pad(name, at):
    """A static plate just under a grid at this position. 0.1 m of air keeps the two from starting inside each other."""
    plate = [armor((x, 0, z)) for x in range(-1, 4) for z in range(-1, 3)]
    return [grid(name, (at[0], at[1] - LARGE_BLOCK - 0.1, at[2]), plate, static=True)]


def world(moon_position=None, moon_radius=None):
    """Every grid in the fixture, as sector objects."""
    Ids.next = 510000000000000000
    def at(slot):
        return (ORIGIN[0] + slot * SPACING, ORIGIN[1], ORIGIN[2])

    grids = []
    for slot, build in enumerate([
        lambda p: plain('player-large', p, OLD_PLAYER),
        lambda p: plain('player-recent', p, RECENT_PLAYER),
        lambda p: plain('player-small', p, OLD_PLAYER, 'Small'),
        lambda p: plain('npc-large', p, NPC),
        lambda p: plain('npc-small', p, NPC, 'Small'),
        lambda p: plain('unowned-large', p),
        lambda p: plain('unowned-small', p, size='Small'),
        lambda p: plain('already-static', p, OLD_PLAYER, static=True),
        lambda p: rotor_pair('rotor-parent', 'rotor-sub-owned', p, OLD_PLAYER, OLD_PLAYER),
        lambda p: rotor_pair('rotor-parent-2', 'rotor-sub-bare', p, OLD_PLAYER),
        lambda p: gear_pair('gear-parent', 'gear-sub-bare', p, OLD_PLAYER),
        lambda p: connector_pair('connector-parent', 'connector-sub-bare', p, OLD_PLAYER),
        lambda p: wheel_pair('wheel-parent', 'wheel-sub', p, OLD_PLAYER),
    ]):
        grids += build(at(slot))
    if moon_position:
        # Inside the moon's gravity, which ends at 5% strength: 9785 * 5^(1/7), about 12300 m from its centre. Each stands on a static pad, because
        # a grid left to fall hits the moon about a minute later, so what a scenario found depended on how long its server had been up.
        x, y, z = moon_position
        high = moon_radius + 2500
        for name, at, build in (('planet-pad', (x, y + high, z), lambda p: plain('planet-large', p, OLD_PLAYER)),
                                ('planet-pad-2', (x + SPACING, y + high, z), lambda p: rotor_pair('planet-rotor-parent', 'planet-rotor-sub', p, OLD_PLAYER))):
            grids += pad(name, at) + build(at)
    return grids


def moon(text):
    """The Moon from the game's Moon Base save, with where it sits and how big it is."""
    planet = re.search(r'<MyObjectBuilder_EntityBase xsi:type="MyObjectBuilder_Planet">.*?</MyObjectBuilder_EntityBase>', text, re.S).group(0)
    p = re.search(r'<Position x="([-\d.]+)" y="([-\d.]+)" z="([-\d.]+)"', planet)
    # A save holds a planet's lowest corner, not its centre. Its size is the first power of two that fits it, as FindBestOctreeSize works it out.
    hills = float(re.search(r'<MaximumHillRadius>([\d.]+)</MaximumHillRadius>', planet).group(1))
    size = 32
    while size < 2 * hills:
        size *= 2
    centre = tuple(float(v) + size / 2 for v in p.groups())
    return planet, centre, float(re.search(r'<Radius>([\d.]+)</Radius>', planet).group(1))


def second_player(checkpoint, now):
    """Copies the save's only player as one who logged out moments ago, so offline limits have both sides."""
    item = re.search(r'<AllPlayersData>\s*<dictionary>\s*(<item>.*?</item>)', checkpoint, re.S).group(1)
    copy = (item.replace(str(OLD_PLAYER_STEAM), str(RECENT_PLAYER_STEAM)).replace(str(OLD_PLAYER), str(RECENT_PLAYER))
            .replace('FixturePlayer', 'FixtureRecent'))
    checkpoint = checkpoint.replace(item, item + copy, 1)
    identity = (f'<MyObjectBuilder_Identity><IdentityId>{RECENT_PLAYER}</IdentityId><DisplayName>FixtureRecent</DisplayName>'
                f'<CharacterEntityId>0</CharacterEntityId><Model>Default_Astronaut</Model><BlockLimitModifier>0</BlockLimitModifier>'
                f'<LastLoginTime>{now}</LastLoginTime><LastLogoutTime>{now}</LastLogoutTime></MyObjectBuilder_Identity>')
    return checkpoint.replace('</Identities>', identity + '</Identities>', 1)


def server_settings(text):
    """An Empty World save says OFFLINE, where the game makes everyone an Owner, so no permission check would ever refuse anyone. Trash removal is
    off because it deletes small grids far from a player as soon as one is online."""
    return text.replace('<OnlineMode>OFFLINE</OnlineMode>', '<OnlineMode>PRIVATE</OnlineMode><TrashRemovalEnabled>false</TrashRemovalEnabled>')


def build(empty_world, moon_sector, out):
    """Writes the fixture save to out, from copies of the game's Empty World and Moon Base saves."""
    os.makedirs(out, exist_ok=True)
    read = lambda name: open(os.path.join(empty_world, name), encoding='utf-8').read()

    planet, moon_position, moon_radius = moon(open(moon_sector, encoding='utf-8').read())
    objects = planet + ''.join(world(moon_position, moon_radius))
    sector = re.sub(r'<SectorObjects>.*</SectorObjects>', lambda _: f'<SectorObjects>{objects}</SectorObjects>', read('SANDBOX_0_0_0_.sbs'), flags=re.S)

    now = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    checkpoint = server_settings(read('Sandbox.sbc'))
    checkpoint = re.sub(r'<ControlledEntities>.*?</ControlledEntities>', '<ControlledEntities><dictionary /></ControlledEntities>', checkpoint, flags=re.S)
    checkpoint = checkpoint.replace('<CameraController>Entity</CameraController>', '<CameraController>Spectator</CameraController>')
    checkpoint = re.sub(r'<CharacterEntityId>\d+</CharacterEntityId>', '<CharacterEntityId>0</CharacterEntityId>', checkpoint)
    checkpoint = re.sub(r'<ClientId>\d+</ClientId>', f'<ClientId>{OLD_PLAYER_STEAM}</ClientId>', checkpoint)
    checkpoint = checkpoint.replace('Skinface', 'FixturePlayer').replace('<Connected>true</Connected>', '<Connected>false</Connected>')
    checkpoint = second_player(checkpoint, now)
    checkpoint = checkpoint.replace('</LastLoginTime>\n    </MyObjectBuilder_Identity>', '</LastLoginTime>\n      <LastLogoutTime>2016-12-19T15:09:41</LastLogoutTime>\n    </MyObjectBuilder_Identity>')
    checkpoint = re.sub(r'<NonPlayerIdentities\s*/>|<NonPlayerIdentities>.*?</NonPlayerIdentities>', f'<NonPlayerIdentities><long>{NPC}</long></NonPlayerIdentities>', checkpoint, flags=re.S)

    open(os.path.join(out, 'SANDBOX_0_0_0_.sbs'), 'w', encoding='utf-8', newline='\n').write(sector)
    open(os.path.join(out, 'Sandbox.sbc'), 'w', encoding='utf-8', newline='\n').write(checkpoint)
    # The moon's terrain lives in a file of its own beside the save.
    for name in os.listdir(empty_world):
        if name.endswith('.vx2'):
            shutil.copy(os.path.join(empty_world, name), out)
    # The compressed copy would win over the text one, so it is left behind.
    open(os.path.join(out, 'Sandbox_config.sbc'), 'w', encoding='utf-8', newline='\n').write(server_settings(read('Sandbox_config.sbc')))
