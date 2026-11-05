# PluginCheck

Tests Space Engineers 1 plugins for Torch, Magnetar and Quasar in containers, so crashes and test data stay off the host.
[REVIEW.md](REVIEW.md) tells an AI how to review a plugin, including security and legal checks, and how to use these tests.

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

Results land in `runs/<target>/<timestamp>/`: `results.json`, plus a log for each stage. Worlds and source copies are
deleted afterwards. Add `--keep` to look at them.

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
| `magnetar` | Official Magnetar image | The loader compiles the plugin from source and a world loads |
| `legacy` | Our Proton image | The same test on MagnetarLegacy and .NET Framework 4.8 |
| `regcheck` | .NET SDK image | Every command module registers on its own, using the plugin the loader built |
| `quasar` | Official Quasar images | The UI plugin builds against each Quasar version and really loads |
| `quasar_managed` | Official Quasar image | Quasar starts a server with the plugin and companion, as a real install does |
| `torch` | Our Proton image | Torch loads the zip and lists every command it registers |
| `reflection` | .NET SDK image | Literal reflection lookups find a real game member with those flags. Others are listed to trace by hand |
| `threads` | .NET SDK image | Lists game objects used off the game thread, following calls through the plugin. Leads to read |
| `nulls` | .NET SDK image | Lists game values that can be null, used with no check first. Leads to read |
| `hostpackages` | Host and images | The plugin ships nothing the game or its loader already provides |
| `lint` | .NET SDK and Quasar images | .NET analyzer warnings, from a real build |
| `logcheck` | Host | Reads every log from the run. A stack trace through plugin code fails it |

## Isolation
Nothing is shared between systems, so an update or a layout change in one cannot affect another:

| System | Dedicated server | Data |
|---|---|---|
| Magnetar | `plugincheck-ds-magnetar` volume | Per run |
| Magnetar Legacy | `plugincheck-ds-legacy` volume | Per run |
| Quasar | None | Per run |
| Quasar-managed | `plugincheck-ds-quasar` volume | Per run |
| Torch | `plugincheck-ds-torch` volume | Per run |

Test servers are private and send no telemetry: Torch analytics are off, Magnetar runs with `-consent deny`, and Quasar
uploads nothing without an admin's consent. The Torch and Legacy stages also block `crash.steampowered.com`, since Steam's
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

Containers have CPU and memory limits. Each plugin's source is copied into the run folder, so a test never writes to the
plugin's repository. Everything Docker creates has a `plugincheck` name.

Containers are not a hard security boundary. Do not run a plugin you would not trust on a test machine.

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
- **Commands are proven to register, not to work.** Magnetar has no console, and `!help` only runs from in-game chat.
  Torch's log lists every command it registers, which is the same list `!longhelp` draws on.
- **Torch and Legacy run under Proton.** Proton is not Windows, so a failure is a lead. Confirm it on Windows before calling it
  a plugin defect. `steamcmd`'s Linux `steamclient.so` is linked in for Proton's Steam bridge, so no Steam client or sign-in is
  needed.
- **Torch needs a display, Legacy does not.** Torch uses WPF, and hangs at startup with no display even with `-nogui`, so only
  its image has Xvfb. Proton's launcher also needs `libvulkan` and its own graphics DLLs at startup, though nothing is drawn.

## License
[Unlicense](LICENSE).
