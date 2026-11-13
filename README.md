# PluginCheck

Tests Space Engineers 1 plugins for Torch, Magnetar and Quasar in containers, so crashes and test data stay off the host.
[REVIEW.md](REVIEW.md) tells an AI how to review a plugin, including security and legal checks, and how to use these tests.
[Agent.md](Agent.md) holds the rules for the AI running it: read the code behind every lead, and give each one a verdict. No stage uses the
[`se-dev` skills](https://github.com/CometWorks/skills), so the AI loads them itself, and Agent.md says which to use and how to set them up.

## Use it
Needs Python 3.11+ and Docker Desktop on the WSL 2 backend. Clone this repository next to your plugin repositories.

```bash
python check.py SensEssentials
```

One stage at a time, or leaving some out:

```bash
python check.py SensEssentials --only manifest,scan
```

```bash
python check.py SensEssentials --skip torch
```

Anything that can be checked without a running server goes first, side by side, and no server starts until all of those are done. A
compile error from `lint` also skips every server stage and says why, since a plugin that does not compile cannot load. On a first run the
stages that read the game's assemblies wait for a server stage instead, because that is what installs them.

Work is only done once. Stages that only read files reuse their last answer when the plugin, the game and PluginCheck's own code are unchanged, and
`behaviour` keeps each server's result by build, game, settings and tools, so a published build never needs its servers again and unchanged code needs
none. A reused result is shown again, leads included, and marked as reused. `dependencies` always runs, because new advisories appear without a
plugin changing. `--refresh` ignores kept results, the thread catalog and the downloaded source of other plugins.

Stages run side by side unless one needs another's work, as `DEPENDS` in `check.py` lists. `behaviour` runs alone because it takes every
core, and `logcheck` runs last because it reads every log. Each line of output is marked with its stage.

A run reports as it goes. In a terminal, a colour panel stays at the top with the time left, every stage, each running server and the
results so far, while details scroll underneath. Each stage's time comes from how long it took last time, and `behaviour` posts each
finished server with an ETA, and each difference as soon as it is known. Without a terminal, or with `PLUGINCHECK_PLAIN=1`, the same lines
print as plain text, with a heartbeat from any quiet stage.

Always run it in a real terminal you can see, so the panel shows. Redirecting or piping the output, or starting it from a background task, turns the
panel off with no warning. An AI running PluginCheck must do the same: [Agent.md](Agent.md) has the rule. To follow a run from outside, read
`results.json` and the stage logs below.

Results land in `runs/<target>/<timestamp>/`: `results.json`, updated after every stage, plus a log for each stage that can be followed live. Worlds and source copies are
deleted afterwards. Add `--keep` to look at them.

A target can give the `behaviour` stage a `behaviour` section, all of it optional:

| Key | What it does |
|---|---|
| `legacy` | Also runs every scenario on MagnetarLegacy, under Proton |
| `shipDrills` | Adds `stages/behaviour/drilling.py`: an asteroid with ship drills of each kind of owner cutting it, and a container in front of where a player caller stands |
| `programmableBlocks` | Adds the blocks in `stages/behaviour/programmable.py` to the world: each on a static grid out in space with a battery, with scripts that run light, run slow, hold memory, fault, or turn themselves on, for players, NPCs and no one, inside and outside safe zones |
| `settleSeconds` | How long to wait once the world is ready, 15 by default. For a plugin with a warm-up, which would otherwise refuse every command |
| `removeDuring` | `commands` to run again while things leave the world, each alone on its server. `minSeconds` is the least each runs for. `remove` lists `after`, `kind` and `name`, where `kind` is `grid`, `block`, `identity`, `faction` or `player`. The player is a `MyPlayer` the probe adds under the server's own client, so this is a player leaving the session, not a real client disconnecting |
| `plugins` | Other plugins a variant can load beside this one, each built from source at a pinned commit or tag. `stages/behaviour/extraplugins.py` has the format |
| `variants` | More builds made from each working build: with `plugins` loaded and their `config`, with `files` written before start, or with an `overlay` folder laid over the source, each limited to its `scenarios` |
| `leaveOut` | Builds to leave out of the run by name, such as a published build known to hang at start |

Add `--hold` to leave the Quasar-managed server running at http://127.0.0.1:18080 with no time limit, to look around its
UI. A local request counts as trusted, so no Steam sign-in is needed. The port is only open to this machine. The run
prints the command that removes it.

Build these once, in order. The first takes about 15 minutes. It installs .NET Framework 4.8 with Wine, because that installer
fails under Proton, and only its finished prefix is carried forward, so the images that run have no separate Wine install.
The hash is the one GitHub publishes for the Windows release:

```bash
docker build -t plugincheck-prefix:latest docker/prefix
```

```bash
docker build -t plugincheck-proton:latest docker/proton
```

```bash
docker build -t plugincheck-torch-proton:latest docker/torch-proton
```

```bash
docker build --build-arg MAGNETAR_SHA256=<sha256> -t plugincheck-magnetar-legacy:latest docker/magnetar-legacy
```

Each stage ends as `pass`, `fail`, `review` (a person or AI must read it), `not run`, `not present` or `unable to verify`.
A stage that did not run is never a pass.

**GitHub limit:** without a token GitHub allows 60 requests an hour. To raise that, put a token in `github-token.txt` beside `check.py`, which
git ignores. Use a fine-grained token with read access to public repositories and nothing else, because a container that uses it also runs
plugin code. `PULSAR_GITHUB_TOKEN` works too.

## Stages
| Stage | Runs in | Proves |
|---|---|---|
| `manifest` | Host | Hub manifests follow the rules: description length, SHA, no TODOs, paths exist |
| `scan` | Host | Lists places to read: process, network, reflection, binaries, submodules and more. It proves nothing by itself |
| `policy` | Host | The hub and loader rules, by id: `error` fails, `warn` needs reading |
| `similar` | Host | Gathers every plugin on torchapi.com, MagnetarHub and QuasarHub, for an AI to compare |
| `copies` | Host | Compares code with every plugin whose source can be reached, both ways, as NOTICE, WARN or EXTREME |
| `dependencies` | .NET SDK image | Every package, from project files and hub manifests, against NuGet's security advisories |
| `inherited` | Host | How much code came from upstream, as a range. Ignores position and counts near matches |
| `lint` | .NET SDK and Quasar images | .NET analyzer warnings, from a real build |
| `reflection` | .NET SDK image | Literal reflection lookups find a real game member with those flags. Others are listed to trace by hand |
| `threads` | .NET SDK image | Lists game objects used off the game thread, following calls through the plugin, and every call to a game member that behaves differently by thread. Leads to read. Always `review`, because no leads proves nothing |
| `commands` | .NET SDK image | Warns when player-level commands can show data about other players: game data, grids, or a plugin's own records. Some are intended, so it never fails |
| `nulls` | .NET SDK image | Lists game values that can be null, used with no check first. Leads to read |
| `storage` | .NET SDK image | Lists what fails on a cluster or a network mount: file calls on the game thread, with no catch on the way up as `UNGUARDED`, failed reads that carry on with defaults, per node files, and state kept by player or grid. Leads to read |
| `hostpackages` | Host and images | The plugin ships nothing the game or its loader already provides |
| `magnetar` | Official Magnetar image | The loader compiles the plugin from source and a world loads. If Magnetar itself cannot start, such as on GitHub's rate limit, it ends as `unable to verify` and says why |
| `legacy` | Our Proton image | The same test on MagnetarLegacy and .NET Framework 4.8. Logs are copied after the server stops, and the game's own log lands in `legacy-game.log` |
| `regcheck` | .NET SDK image | Every command module registers on its own, using the plugin the loader built. If it has no plugin to test, it says why `magnetar` left none |
| `quasar` | Official Quasar images | The UI plugin builds against each Quasar version and really loads |
| `quasar_managed` | Official Quasar image | Quasar starts a server with the plugin and companion, as a real install does. A Magnetar plugin with no Quasar UI plugin runs too, since Quasar renders its settings |
| `torch` | Our Proton image | Torch loads the zip and lists every command it registers |
| `behaviour` | Official Magnetar image and our Proton image | Runs each command and setting on real servers, and compares every build, including the published one. Always `review` |
| `logcheck` | Host | Reads every log from the run. A stack trace through plugin code fails it |

## Thread-sensitive members
Some game members give a different answer depending on who calls them. `MyCubeGrid.BigOwners` gives a subgrid with no owned
blocks its parent's owners on the game thread, and an empty list anywhere else. Moving such a line between threads changes what a
plugin does, with no error and no failed stage. So `threads` lists every plugin call to one of these members, on any thread, with
what it depends on and how it got there.

`stages/threadcatalog` writes the list from the game's own IL, and nothing in it is written by hand:

- **Seeds** are methods that compare the calling thread with the game's update thread, read `MyEntities.IsAsyncUpdateInProgress`, or
  read a `[ThreadStatic]` field that holds state. A field that only ever gets a new object or null is a scratch buffer, and does not count.
- **Callers** are then followed, because `GetOwnerLogoutTimeSeconds` changes with the thread too, though it only reads `BigOwners`.
  A value is followed 4 calls up. A side effect is followed 1, since nearly everything in the game plays a sound somewhere.
- **Interface and base members** count too, so a call through `IMyCubeGrid.BigOwners` is found.

Regenerating is automatic. The catalog lives in `cache/threadcatalog/<game build>.json`, keyed by a hash of the game's assemblies and its generator, so
the first run after a game update writes a new one and prints what changed since the last catalog. `--refresh` forces it.

Each entry in `threads.json` under `threadSensitive` says:

| Field | Meaning |
|---|---|
| `dependsOnCaller` | `result` if what comes back differs, `effect` if what it does differs |
| `context` | `background` if a background walk reached it, `handed to the game thread` if inside an invoke, or `not traced` |
| `seed`, `via` | What the game checks, and the chain of calls from the plugin's member to that check |

`not traced` means no background path was found. It does not mean the game thread. Limits of the catalog: it does not see thread
checks through `ManagedThreadId` fields or `ThreadLocal<T>`, the depth limits leave out distant callers, and delegates the game calls
later are not followed. A member that is not listed may still depend on the thread.

`MyEntities.GetEntityById` shows why thread-static fields count. World load can leave entities in the game thread's `[ThreadStatic]`
list in `MyEntityIdentifier`, so another thread gets null for an entity that exists. Measured on Magnetar Interim.

## Behaviour
Loading proves little: a plugin can change which grids it acts on, with every other stage still passing. `behaviour` runs what a
plugin does and records the result. It needs nothing written by a person, and runs by default with no limit on server starts.

1. **Discovers:** a probe plugin loads beside the plugin and reads its commands and settings, on Magnetar and on Torch.
2. **Generates:** one idle run, then every command, on its own and with each on/off setting its code can reach flipped. The AI reading
   the commands gives each one real example lines, and who runs them: the console, a player or an Admin. A command with no example has its
   arguments filled in by type, which only reaches its first check, so it is listed as `NO EXAMPLE`.
3. **Runs:** every scenario runs on every build it fits. Each server starts from a fresh copy of the world, and is killed as soon as the
   probe has its record. Its cores go back at the kill, and its logs are copied and its container removed while the next server starts.
   The probe also copies its record into a small folder mounted from this computer, so the wait sees it without asking Docker. Docker is
   asked every 5 seconds instead of every second, for a record the copy missed, a Torch fatal error, or a server that ended.
4. **Compares:** the published build against each working build, and the working builds against each other.

Published builds come from the repositories listed on MagnetarHub and QuasarHub, and from torchapi.com, into `cache/behaviour`. A plugin
on none of them is new, so it has no baseline. Comparisons cross platforms on purpose. A port should behave like what it came from, so a
difference between a Torch and a Magnetar build is worth reading, and matching behaviour can support a finding that a plugin was ported
without credit. A difference across platforms can also come from the host, such as Proton or timing, so read it against the code.

The AI running PluginCheck reads each command's code and records what it reads and writes, as [Agent.md](Agent.md) says. Commands share a server
unless they overlap, meaning one writes something another reads or writes, so changing the world is no reason to run alone. Only scenarios with
the same settings can share one. Writers go first, so the first command in each server starts clean. A later command that shows a difference, or any
command that changes something it does not declare, reruns alone before it is reported. The same reading says which settings each command can
reach. With no current reading, every command gets its own server and every setting is flipped.

Servers run side by side, as many as fit in Docker's cores and memory, each pinned to its own 2 logical cores. Measured on AutoStation, 16 servers
on 2 cores finished about 1.4 times faster than 8 on 4, with the same results.

The world is generated by `stages/behaviour/fixture.py` from the game's own saves: grids of each size and kind of owner, subgrids on
rotors, wheels, landing gear and connectors, and a moon for gravity. Every build runs at least twice, and a result that differs between runs of one build
is marked `UNSTABLE`, with the builds it happened on, because it depends on timing. `PLUGINCHECK_REPEATS` sets how many runs each build gets.

The probe records on the game thread, the only place the game gives the full answer. Results land in `behaviour.json`, with every
record in `behaviour-records.json`, and what every command said in `behaviour-replies.txt`. Each `DIFF` is a lead that two builds disagree, not a verdict on which is right.

A finished server only proves that a server finished. Two builds that fail the same way agree, so a command that never ran fails the stage,
and how far each command got has to be read from `behaviour-replies.txt`.

Commands run as the console unless an example names a caller. A caller is a made-up online player with a character, because no real client can
join a test server, and a second one, ProbeTarget, is there to be acted on. Replies are captured, since nothing could receive them. On Torch, a
player's command skips `OnCommandExecuting`, so a plugin's own permission hook does not run. Settings that hold a list, such as auto commands
and presets, start empty, so the commands that use them stop at "not found".

The world has no programmable blocks unless a target asks for them, and on a world with them the probe also records each block's state, and what its script was doing in `behaviour-records.json`. An example can then say `settle` and `min`, the seconds before it runs and after, and `settings` of any type. Lines joined with ` && ` run on one server, and `probe:recompile <block name>` and `probe:save` do what a client or an autosave would. An example with `node` runs on Magnetar as a cluster node, set by the two variables PluginSdk reads, with a shared folder, and `probe:sharedwrite`, `probe:sharedread` and `probe:sharedremove` stand in for another node writing it. That is one node, not a cluster: no gateway, no handover and no second process. Blocks named `watch-*` are recorded with the names of what they hold. Example lines use a command's real case, because Torch's are case sensitive. `stages/classification.py` has the details.

**What it cannot prove:**
- **Correctness.** No difference only means the builds agree, and both may be wrong.
- **That there are no races.** A race depends on timing, and a short run on a small world rarely hits one.
- **Anything needing a real player.** There is no game client, so online player checks stay untested.
- **That it reached the logic.** A plugin that only reacts to players or chat may do nothing here. `noChange` lists scenarios that left
  the world unchanged. These are never reported as correct.
- **A new plugin's regressions.** With no baseline, its first review rests on reading the code.
- **Scale, long runs, or other plugins.** A run lasts minutes on a small world, with only this plugin and the probe loaded.
- **Commands that need particular arguments.** Generated values may be refused. Replies are in the record, so read them, and read the
  command's code.
- **Settings that work together.** On/off settings flip one at a time, and other kinds of setting are not changed.
- **Legacy.** Only Magnetar Interim and Torch run this stage.

### Timing
Every server prints one `timing` line in `behaviour.log` when its slot is freed. It gives the seconds from getting the slot until each point: the container is up, the game is ready, the record appears (or the server stops without one), it is killed, its slot is freed, and it is cleaned up. It also gives the wait for the slot before that, and the sim speed: game seconds per wall second while the probe ran, where 1 is full speed. The same numbers are in each record's `timing` field, and the probe's own numbers are in `probeTiming`. Game ready is worked out back from the record using the probe's own time span, so the container's clock doesn't have to match the host's. These numbers are for measuring only: no comparison reads them. A result kept for later runs is saved without them, so a reused result has no `timing` or `probeTiming`.

## Lending slots to other sessions
The behaviour stage runs its servers in slots of two logical cores each. Other sessions can borrow some of them for in-game work,
such as mod or Torch fixes. They may not borrow them to check another plugin: that is a PluginCheck run.

- A session borrows at most 2 slots, and at most 3 sessions borrow at once, so a run always keeps the rest.
- A slot is handed over only when one of PluginCheck's servers finishes in it. A running server is never stopped. With no run
  going, a request is granted at once.
- The borrower pins its container to the lease's cores and names it `<session>-<n>`, never `plugincheck-*`.
- A lease with no heartbeat for 5 minutes ends. If its container still runs, the slot stays out of use until it stops.
  PluginCheck never stops another session's container.
- An exclusive request, for measurements that need a quiet machine, drains everything: no new lease and no new PluginCheck
  server. It is granted, as cores `all`, once nothing else runs.

The leases are one file, `machine/leases.json` in this folder (`PLUGINCHECK_MACHINE_DIR` moves it), changed under a
lock. Sessions use `lease.py`:

```
python lease.py request   --session NAME --count 2 --purpose "mod fix test"
python lease.py heartbeat --session NAME        # at least every minute while working
python lease.py release   --session NAME [--cores 4-5]
python lease.py status
```

Each call prints the session's leases as JSON. Keep calling `request` or `heartbeat` until the count is reached, then start each
container with `docker run --cpuset-cpus <cores> --name <container> ...`. The rules are in `stages/leases.py`, tested without
Docker by `python -m unittest discover -s tests`.

## Isolation
Nothing is shared between systems, so an update or a layout change in one cannot affect another:

| System | Dedicated server | Data |
|---|---|---|
| Magnetar | `plugincheck-ds-magnetar` volume | Per run |
| Magnetar Legacy | `plugincheck-ds-legacy` volume | Per run |
| Quasar | None | Per run |
| Quasar-managed | `plugincheck-ds-quasar` volume | Per run |
| Torch | `plugincheck-ds-torch` volume | Per run |

Test servers are private. Torch analytics are off, and Quasar uploads nothing without an admin's consent. Magnetar runs with its
defaults: it adds its companion mod MagnetarMod to the world, and turns its own telemetry off when it has no interactive terminal, as in these containers. The Torch and Legacy stages also block `crash.steampowered.com`, since Steam's
library hits internal asserts under Legacy and would upload a crash dump for each. Failed test runs do not reach anyone's error
reports.

Tests are set up as a real server is: a Torch plugin's zip goes into Torch's own `Plugins` folder, and a loader compiles from
its dev folder. Run data lives on Docker volumes. Mounted Windows folders only hand files in, partly because .NET under Wine,
which Proton is built on, cannot load larger DLLs from them, and logs are copied out at the end.

Our images are kept lean, and each removal was tested by loading a real server. They run as the `server` user, hold no setuid
programs, and contain no download tools, since each download happens in a build stage that is thrown away. Log files are copied
to the container's console, so a run can be followed from Docker. If you built these images before they ran as `server`, hand
the old game volumes over once:

```bash
docker run --rm -u 0 --entrypoint chown -v plugincheck-ds-torch:/ds plugincheck-proton:latest -R server:server /ds
```

```bash
docker run --rm -u 0 --entrypoint chown -v plugincheck-ds-legacy:/ds plugincheck-proton:latest -R server:server /ds
```

`behaviour` starts its servers from the Magnetar and Torch volumes with updates off, and each server has its own data volume.

Containers have CPU and memory limits, and each `behaviour` server is pinned to its own cores. Each plugin's source is copied into the run folder, so a test never writes to the
plugin's repository. Everything Docker creates has a `plugincheck` name.

Containers are not a hard security boundary. Do not run a plugin you would not trust on a test machine.

## What it keeps
`cache/` is ignored by git, and deleting any of it only costs time.

| Folder | Holds |
|---|---|
| `cache/stages/<target>` | The last answer of each stage that only reads files, by a hash of what it read |
| `cache/threadcatalog` | Thread-sensitive members, by game build |
| `cache/behaviour` | Published builds, each server's result in `results`, and the AI's reading of the commands in `commands-<target>.json` |
| `cache/magnetar-github` | What Magnetar downloads from GitHub, by game build |
| `cache/sources` | The source of other plugins, for `copies` |

## Environment variables
| Variable | Effect |
|---|---|
| `PLUGINCHECK_PLAIN=1` | Plain lines instead of the panel |
| `PLUGINCHECK_REPEATS` | Runs of each build in `behaviour`. The default is 2, which is also the least |
| `PLUGINCHECK_BEHAVIOUR_WORKERS` | Servers at a time. The default is as many as fit |
| `PLUGINCHECK_BEHAVIOUR_CORES` | Logical cores per server. The default is 2 |
| `PLUGINCHECK_BEHAVIOUR_ONLY` | Only scenarios containing this text, for working on the stage |
| `PLUGINCHECK_TORCH_IMAGE`, `PLUGINCHECK_LEGACY_IMAGE` | Use a different image |
| `PLUGINCHECK_WORLD` | A save folder for `quasar_managed` to start from, in place of Star System, such as one with grids a plugin acts on, for a hand test with `--hold` |
| `PULSAR_GITHUB_TOKEN` | A GitHub token, in place of `github-token.txt` |

## Add a plugin
Add `targets/<name>.json`. Leave out the sections the plugin does not have:

| Field | Meaning |
|---|---|
| `name`, `repo` | Plugin name, and its repository relative to this folder, such as `../SensEssentials` |
| `magnetar.manifest` | The hub manifest. Its `SourceDirectories` decide what gets compiled |
| `quasar.manifest`, `quasar.packageManifest` | The hub manifest and `quasar-plugin.json` |
| `torch.zip`, `torch.guid`, `torch.project` | The zip from the Windows build, the plugin GUID and its project |
| `upstream`, `upstreamSubfolder` | Zip URL of the project this one derives from, if any |
| `knownNew` | Folders with no upstream equivalent. They measure how often unrelated code matches by chance |
| `exclude` | Folders to leave out of the inherited-code measure |

## Known issues
- **Quasar 1.4.1.1 image:** `release.json` says `1.4.1-1` while the program reports `1.4.1.1`, so it cannot write the
  migration receipt that Quasar 1.5 requires, and a fresh 1.5 container cannot start. The `quasar` stage corrects that
  one field for the 1.4.1.1 step, so Quasar's own code writes the real receipt. Results record `releaseJsonWorkaround`.
- **Torch passes the wrong arguments for some values.** `CommandTree.GetCommand` finds a command's arguments by searching the whole line for
  the first one, so `on` in `!monitor everything on` is found in "monitor" and the handler gets `onitor everything on`. The `commands` stage warns
  about values a Torch handler compares that also appear in its own words, and lists the ones taking free text. The Torch probe compares what
  Torch would pass with what was typed on every line it runs, and `behaviour` prints a `TORCH-ARGS` lead when they differ.
- **GitHub's limit stops Magnetar.** Magnetar fetches its core plugins from GitHub, 60 requests an hour without a token. When it runs out,
  `magnetar` and `legacy` end as `unable to verify` with the time it resets, and `regcheck` as `not run`. Run them again after that.
  `behaviour` waits for the reset by itself. A token avoids this, and `cache/magnetar-github` keeps each game build's downloads, so later
  runs ask for less.
- **Magnetar's downloads can time out.** GitHub can take 9 to 14 seconds to answer for the LinuxCompat release files, and Pulsar gives
  up. `magnetar` and `legacy` end as `unable to verify`. `behaviour` tries a server 3 times, then reports it as `FAILED`, or lists the
  build in `buildsLeftOut`. A working build that can't be discovered stops the stage as `not run`, and a published one only narrows the comparison.
- **Load stages prove commands register, not that they work.** Magnetar has no console, and `!help` only runs from in-game chat. `behaviour`
  runs each command through the probe, within the limits under [Behaviour](#behaviour). Quasar only proves the plugin loads, serves its pages and
  reaches its settings, and Legacy runs commands only with `"legacy": true` in the target's `behaviour` section.
- **Torch and Legacy run under Proton.** Proton is not Windows, so a failure is a lead. Confirm it on Windows before calling it
  a plugin defect. `steamcmd`'s Linux `steamclient.so` is linked in for Proton's Steam bridge, so no Steam client or sign-in is
  needed.
- **Torch needs a display, Legacy does not.** Torch uses WPF, and hangs at startup with no display even with `-nogui`, so only
  its image has Xvfb. Proton's launcher also needs `libvulkan` and its own graphics DLLs at startup, though nothing is drawn.

## License
[Unlicense](LICENSE).
