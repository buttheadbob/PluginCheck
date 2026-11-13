using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using Sandbox.Engine.Physics;
using Sandbox.Game.Entities;
using Sandbox.Game.Entities.Cube;
using Sandbox.Game.GameSystems;
using Sandbox.Game.GameSystems.BankingAndCurrency;
using Sandbox.Game.Multiplayer;
using Sandbox.Game.Screens.Helpers;
using Sandbox.Game.World;
using VRage;
using VRage.FileSystem;
using VRage.Game.Entity;
using VRage.Game.ModAPI;
using VRage.Utils;
using VRageMath;

namespace PluginCheckProbe;

/// <summary>A command line a plugin registered, and the parameters it takes.</summary>
public sealed class CommandInfo
{
    public string Line = "";
    public List<(string Type, bool Optional)> Parameters = new();
}

/// <summary>Something to take out of the world while a step's command is running.</summary>
public sealed class Removal
{
    public int Step;
    public int AfterSeconds;
    public string Kind = "";
    public string Name = "";
    public bool Done;
}

/// <summary>The online player a command runs as. With none, it runs as the server's console.</summary>
public sealed class Caller
{
    public ulong SteamId;
    public long IdentityId;
    public string Name = "";
    public MyPromoteLevel Level;
}

/// <summary>What differs between hosts: the plugin's commands, and how to run one.</summary>
public interface IProbeHost
{
    object? FindPlugin(string name);
    IEnumerable<CommandInfo> Commands(object plugin);
    List<string> Run(string line, Caller? caller);
}

/// <summary>Discovers a plugin, or runs its scenarios in turn and records the world before and after each. Everything here runs on the
/// game thread, because that is the only thread where the game gives the full answer: off it, BigOwners and entity lookups differ.</summary>
public sealed class ProbeCore
{
    private const int CheckTicks = 60;
    private const int ProbePlayerSerial = 77;
    private const ulong CallerSteamId = 76561190000000003;
    private const ulong TargetSteamId = 76561190000000004;

    private readonly IProbeHost _host;
    private string _mode = "";
    private string _pluginName = "";
    private int _maxSeconds = 600;
    private readonly List<(string Command, int MinSeconds, int QuietChecks, string Caller)> _steps = new();
    private bool _addPlayers;
    private MyPlayer? _callerPlayer;
    private readonly List<string> _results = new();
    private readonly List<Removal> _removals = new();
    private readonly List<string> _removed = new();
    private int _settleTicks = 15 * 60;
    private MyPlayer? _player;
    private int _step;
    private readonly Dictionary<string, string> _settings = new();
    private object? _plugin;
    private string _before = "";
    private string _last = "";
    private readonly List<List<string>> _replies = new();
    private readonly Queue<string> _lines = new();
    // The caller, for probe lines that act as them.
    private static MyPlayer? _seated;
    private int _resumeAt;
    private readonly List<string> _settingsApplied = new();
    private int _ticks;
    private int _startedAt = -1;
    private int _quiet;
    private bool _finished;
    private bool _prepared;
    private string _settingsWhen = "";

    // Plugins have their config by now, and have not started their session. Some settings are only read at that start, such as
    // AutoStation's GridTrackingMode, so a setting changed after it would test something no real server does.
    public ProbeCore(IProbeHost host)
    {
        _host = host;
        MySession.BeforeLoading += BeforeLoading;
    }

    public void Dispose() => MySession.BeforeLoading -= BeforeLoading;

    private void BeforeLoading()
    {
        try
        {
            Prepare("before the world loaded");
        }
        catch (Exception e)
        {
            MyLog.Default.WriteLineAndConsole("PROBE could not prepare: " + e);
        }
    }

    private void Prepare(string when)
    {
        if (_prepared)
            return;
        _prepared = true;
        ReadScenario();
        _plugin = _host.FindPlugin(_pluginName);
        if (_plugin != null && _mode != "discover")
            ApplySettings(_plugin);
        _settingsWhen = when;
    }

    public void Tick()
    {
        if (_finished || MySession.Static == null || !MySession.Static.Ready)
            return;
        _ticks++;
        try
        {
            if (_startedAt >= 0)
                Watch();
            else if (_ticks >= _settleTicks)
                Start();
        }
        catch (Exception e)
        {
            Finish("{\"failed\":" + Quote(e.ToString()) + "}");
        }
    }

    private void Start()
    {
        Prepare("after the world loaded, too late for settings read at startup");
        if (_plugin == null && _pluginName.Length > 0)
        {
            Finish("{\"failed\":" + Quote("the plugin is not loaded: " + _pluginName) + "}");
            return;
        }

        if (_mode == "discover")
        {
            Finish(Discovery());
            return;
        }

        if (_addPlayers)
        {
            _callerPlayer = OnlinePlayer(CallerSteamId, "ProbeCaller");
            _seated = _callerPlayer;
            OnlinePlayer(TargetSteamId, "ProbeTarget");
        }

        Begin(Record());
    }

    // No real client can join a test server, so a client is made up. Replies never reach it, and the hosts capture them instead.
    private static MyPlayer OnlinePlayer(ulong steamId, string name)
    {
        Sync.Clients.AddClient(steamId, name);
        // A saved player keeps its identity, because blocks in the world belong to it.
        long saved = MySession.Static.Players.TryGetIdentityId(steamId);
        MyIdentity identity = saved != 0 ? MySession.Static.Players.TryGetIdentity(saved) : MySession.Static.Players.CreateNewIdentity(name);
        MyPlayer player = MySession.Static.Players.CreateNewPlayer(identity, new MyPlayer.PlayerId(steamId, 0), name, true, false, true);
        MyBankingSystem.Static.CreateAccount(identity.IdentityId);
        // Clear of every grid in the fixture, whose first row starts at 1000, 1000, 1000.
        player.SpawnAt(MatrixD.CreateWorld(new Vector3D(1000, 1400, 1000 + (long)(steamId % 10) * 20)), Vector3.Zero, null, null);
        return player;
    }

    private Caller? CallerFor(string level)
    {
        if (level.Length == 0)
            return null;
        // Running this as the console instead would pass every permission check, and look like a test of them.
        if (_callerPlayer == null)
            throw new InvalidOperationException("a scenario asked for a " + level + " caller, but no player was added");
        MyPromoteLevel promote = level == "admin" ? MyPromoteLevel.Admin : MyPromoteLevel.None;
        // Written directly, because SetUserPromoteLevel only tells clients, and there are none.
        MySession.Static.PromotedUsers[CallerSteamId] = promote;
        return new Caller { SteamId = CallerSteamId, IdentityId = _callerPlayer.Identity.IdentityId, Name = _callerPlayer.DisplayName, Level = promote };
    }

    private void Begin(string before)
    {
        _before = before;
        string command = _steps[_step].Command;
        _removed.Clear();
        if (_removals.Any(removal => removal.Step == _step && removal.Kind == "player"))
            AddPlayer();
        _replies.Clear();
        foreach (string line in command.Split(new[] { " && " }, StringSplitOptions.RemoveEmptyEntries))
            _lines.Enqueue(line);
        RunLines();
        _last = "";
        _quiet = 0;
        _startedAt = _ticks;
    }

    private void RunLines()
    {
        while (_lines.Count > 0)
        {
            string line = _lines.Dequeue();
            if (line.StartsWith("probe:wait ", StringComparison.Ordinal))
            {
                _resumeAt = _ticks + int.Parse(line.Substring("probe:wait ".Length), CultureInfo.InvariantCulture) * 60;
                return;
            }
            _replies.Add(line.StartsWith("probe:", StringComparison.Ordinal) ? new List<string> { TryProbeAction(line) } : _host.Run(line, CallerFor(_steps[_step].Caller)));
        }
    }

    // A build that has no shared folder, such as one from before a plugin supported clusters, says so in its replies, and the scenario carries on.
    private static string TryProbeAction(string line)
    {
        try
        {
            return ProbeAction(line);
        }
        catch (Exception e)
        {
            return line + ": failed, " + e.GetType().Name + ": " + e.Message;
        }
    }

    // What a client, an autosave or another node does, which a scenario has no other way to cause.
    private static string ProbeAction(string line)
    {
        string[] words = line.Split(new[] { ' ' }, 3);
        switch (words[0])
        {
            case "probe:sharedwrite":
                string path = Path.Combine(SharedFolder(), words[1]);
                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                File.WriteAllText(path, words[2]);
                return line + ": done";
            case "probe:off":
                MyFunctionalBlock off = MyEntities.GetEntities().OfType<MyCubeGrid>().SelectMany(grid => grid.GetFatBlocks().OfType<MyFunctionalBlock>()).First(b => b.CustomName.ToString() == words[1]);
                off.Enabled = false;
                return line + ": " + (off.Enabled ? "still on" : "off");
            case "probe:file":
                // A file under the server's user data folder, such as a plugin's config, as it is now.
                string read = Path.Combine(MyFileSystem.UserDataPath, string.Join(" ", words.Skip(1)));
                return line + ": " + (File.Exists(read) ? File.ReadAllText(read).Replace("\r", "").Replace("\n", " ") : "(no file)");
            case "probe:sit":
                MyCockpit cockpit = MyEntities.GetEntities().OfType<MyCubeGrid>().SelectMany(grid => grid.GetFatBlocks().OfType<MyCockpit>()).First(c => c.CustomName.ToString() == words[1]);
                cockpit.AttachPilot(_seated?.Character ?? throw new InvalidOperationException("no caller to seat"), 0);
                return line + ": " + (cockpit.Pilot != null ? "seated" : "not seated");
            case "probe:sharedread":
                return line + ": " + File.ReadAllText(Path.Combine(SharedFolder(), words[1]));
            case "probe:sharedremove":
                Directory.Delete(Path.Combine(SharedFolder(), words[1]), true);
                return line + ": done";
        }

        if (line == "probe:save")
        {
            MyAsyncSaving.Start();
            return line + ": ServerSaving=" + Lower(MySession.Static.ServerSaving);
        }
        throw new InvalidOperationException("unknown probe line");
    }

    private static string SharedFolder() => Directory.GetDirectories(Path.Combine(Environment.GetEnvironmentVariable("CLUSTER_SHARED_ROOT")!, "plugins")).Single();

    // Waits for the world to stop changing, since nothing says when a plugin has finished.
    private void Watch()
    {
        RemoveWhatIsDue();
        if (_lines.Count > 0)
        {
            if (_ticks >= _resumeAt)
                RunLines();
            return;
        }
        if ((_ticks - _startedAt) % CheckTicks != 0)
            return;
        string now = Record();
        _quiet = now == _last ? _quiet + 1 : 0;
        _last = now;
        int seconds = (_ticks - _startedAt) / 60;
        if (seconds >= _maxSeconds)
            Conclude(now, "the time limit, while the world was still changing");
        else if (seconds >= _steps[_step].MinSeconds && _quiet >= _steps[_step].QuietChecks)
            Conclude(now, "the world staying the same");
    }

    private void Conclude(string after, string ended)
    {
        _results.Add("{\"command\":" + Quote(_steps[_step].Command) + ",\"replies\":[" + string.Join(",", _replies.SelectMany(lines => lines).Select(Quote)) + "],\"removed\":[" + string.Join(",", _removed.Select(Quote)) + "],\"ended\":" + Quote(ended) + ",\"before\":" + _before + ",\"after\":" + after + "}");
        _step++;
        if (_step < _steps.Count)
        {
            Begin(after);
            return;
        }
        Finish("{\"settingsApplied\":[" + string.Join(",", _settingsApplied.Select(Quote)) + "],\"settingsWhen\":" + Quote(_settingsWhen) + ",\"steps\":[" + string.Join(",", _results) + "]}");
    }

    private void Finish(string json)
    {
        _finished = true;
        File.WriteAllText(Path.Combine(MyFileSystem.UserDataPath, "probe.json"), json);
        MyLog.Default.WriteLineAndConsole("PROBE done");
    }

    // On the game thread, the same way an admin or a cleanup plugin would remove these, so a plugin in the middle of work meets it as it really happens.
    private void RemoveWhatIsDue()
    {
        int seconds = (_ticks - _startedAt) / 60;
        foreach (Removal removal in _removals.Where(removal => removal.Step == _step && !removal.Done && seconds >= removal.AfterSeconds))
        {
            removal.Done = true;
            string what = removal.Kind + " " + removal.Name + " at " + seconds.ToString(CultureInfo.InvariantCulture) + " s";
            try
            {
                Remove(removal);
                _removed.Add(what);
            }
            catch (Exception e)
            {
                _removed.Add(what + " failed: " + e.GetType().Name + ": " + e.Message);
            }
        }
    }

    private void Remove(Removal removal)
    {
        switch (removal.Kind)
        {
            case "grid":
                Grid(removal.Name).Close();
                break;
            case "block":
                MyCubeGrid grid = Grid(removal.Name);
                grid.RemoveBlock(grid.GetBlocks().Last(), updatePhysics: true);
                break;
            case "identity":
                MySession.Static.Players.RemoveIdentity(MySession.Static.Players.GetAllIdentities().First(identity => identity.DisplayName == removal.Name).IdentityId);
                break;
            case "faction":
                MyFactionCollection.RemoveFaction(MySession.Static.Factions.TryGetFactionByTag(removal.Name).FactionId);
                break;
            case "player":
                if (_player == null)
                    throw new InvalidOperationException("no player was added");
                MySession.Static.Players.RemovePlayer(_player);
                _player = null;
                break;
            default:
                throw new InvalidOperationException("nothing removes a " + removal.Kind);
        }
    }

    private static MyCubeGrid Grid(string name) => MyEntities.GetEntities().OfType<MyCubeGrid>().First(grid => grid.DisplayName == name);

    // The server's own client stands in, because a player needs one and no real client can join a test server.
    private void AddPlayer()
    {
        MyIdentity identity = MySession.Static.Players.CreateNewIdentity("ProbePlayer");
        _player = MySession.Static.Players.CreateNewPlayer(identity, new MyPlayer.PlayerId(Sync.MyId, ProbePlayerSerial), "ProbePlayer", true, false, true);
    }

    private void ReadScenario()
    {
        string path = Path.Combine(MyFileSystem.UserDataPath, "probe-scenario.txt");
        foreach (string line in File.ReadAllLines(path))
        {
            int split = line.IndexOf('=');
            if (split < 0)
                continue;
            string key = line.Substring(0, split);
            string value = line.Substring(split + 1);
            if (key == "mode") _mode = value;
            else if (key == "settle") _settleTicks = int.Parse(value, CultureInfo.InvariantCulture) * 60;
            else if (key == "remove")
            {
                string[] parts = value.Split(new[] { '|' }, 4);
                _removals.Add(new Removal { Step = int.Parse(parts[0], CultureInfo.InvariantCulture), AfterSeconds = int.Parse(parts[1], CultureInfo.InvariantCulture), Kind = parts[2], Name = parts[3] });
            }
            else if (key == "plugin") _pluginName = value;
            else if (key == "step")
            {
                string[] parts = value.Split(new[] { '|' }, 4);
                _steps.Add((parts[3], int.Parse(parts[0], CultureInfo.InvariantCulture), int.Parse(parts[1], CultureInfo.InvariantCulture), parts[2]));
            }
            else if (key == "players") _addPlayers = value == "1";
            else if (key.StartsWith("set:", StringComparison.Ordinal)) _settings[key.Substring(4)] = value;
        }
    }

    // A plugin's settings live on an object of its own type, which it exposes as a property: PluginConfig on Magnetar, and on
    // Torch whatever the plugin chose. So anything of that shape counts, and no plugin has to say so.
    private static IEnumerable<(object Owner, PropertyInfo Setting)> Settings(object plugin)
    {
        Assembly assembly = plugin.GetType().Assembly;
        foreach (PropertyInfo holder in plugin.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance))
        {
            if (holder.GetIndexParameters().Length > 0 || holder.PropertyType == plugin.GetType())
                continue;
            object? value;
            try { value = holder.GetValue(plugin); }
            catch (Exception) { continue; }
            if (value == null || value.GetType().Assembly != assembly)
                continue;
            foreach (PropertyInfo setting in value.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance))
                if (setting.CanRead && setting.CanWrite && setting.GetIndexParameters().Length == 0)
                    yield return (value, setting);
        }
    }

    private void ApplySettings(object plugin)
    {
        foreach ((object owner, PropertyInfo setting) in Settings(plugin))
        {
            if (!_settings.TryGetValue(setting.Name, out string? text))
                continue;
            object value = setting.PropertyType.IsEnum ? Enum.Parse(setting.PropertyType, text) : Convert.ChangeType(text, setting.PropertyType, CultureInfo.InvariantCulture);
            setting.SetValue(owner, value);
            _settingsApplied.Add(setting.Name + "=" + text);
        }
    }

    private string Discovery()
    {
        var commands = new List<string>();
        var settings = new List<string>();
        if (_plugin != null)
        {
            foreach (CommandInfo command in _host.Commands(_plugin))
            {
                string parameters = string.Join(",", command.Parameters.Select(p => "{\"type\":" + Quote(p.Type) + ",\"optional\":" + Lower(p.Optional) + "}"));
                commands.Add("{\"line\":" + Quote(command.Line) + ",\"parameters\":[" + parameters + "]}");
            }
            foreach ((object owner, PropertyInfo setting) in Settings(_plugin))
                settings.Add("{\"name\":" + Quote(setting.Name) + ",\"type\":" + Quote(setting.PropertyType.Name) + ",\"default\":" + Quote(Convert.ToString(setting.GetValue(owner), CultureInfo.InvariantCulture) ?? "") + "}");
        }
        return "{\"commands\":[" + string.Join(",", commands) + "],\"settings\":[" + string.Join(",", settings) + "],\"world\":" + Record() + "}";
    }

    private static string Record()
    {
        var grids = new SortedDictionary<string, string>(StringComparer.Ordinal);
        foreach (MyCubeGrid grid in MyEntities.GetEntities().OfType<MyCubeGrid>())
        {
            string name = grid.DisplayName ?? "";
            for (int i = 2; grids.ContainsKey(name); i++)
                name = (grid.DisplayName ?? "") + "#" + i;
            grids[name] = Describe(grid);
        }
        int others = MyEntities.GetEntities().Count(e => e is not MyCubeGrid);
        return "{\"grids\":{" + string.Join(",", grids.Select(g => Quote(g.Key) + ":" + g.Value)) + "},\"otherEntities\":" + others
            + ",\"identities\":" + MySession.Static.Players.GetAllIdentities().Count + ",\"factions\":" + MySession.Static.Factions.Count() + "}";
    }

    private static string Describe(MyCubeGrid grid)
    {
        MyCubeGrid? parent = MyGridPhysicalHierarchy.Static.GetParent(grid);
        MyEntity? connection = MyGridPhysicalHierarchy.Static.GetEntityConnectingToParent(grid);
        return "{\"isStatic\":" + Lower(grid.IsStatic) + ",\"size\":" + Quote(grid.GridSizeEnum.ToString()) + ",\"blocks\":" + grid.BlocksCount
            + ",\"owners\":" + Quote(string.Join(",", grid.BigOwners ?? new List<long>())) + ",\"parent\":" + Quote(parent?.DisplayName ?? "")
            + ",\"connection\":" + Quote(connection?.GetType().Name ?? "")
            + ",\"inGravity\":" + Lower(!Vector3D.IsZero(MyGravityProviderSystem.CalculateNaturalGravityInPoint(grid.PositionComp.GetPosition()))) + "}";
    }

    private static string Quote(string text)
    {
        var quoted = new StringBuilder("\"");
        foreach (char c in text)
        {
            if (c == '"' || c == '\\') quoted.Append('\\').Append(c);
            else if (c < ' ') quoted.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
            else quoted.Append(c);
        }
        return quoted.Append('"').ToString();
    }

    private static string Lower(bool value) => value ? "true" : "false";
}
