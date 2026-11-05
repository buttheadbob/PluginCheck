using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using PluginSdk.Commands;
using VRage.Game.ModAPI;
using VRage.Plugins;

namespace PluginCheckProbe;

/// <summary>The probe on Magnetar. It runs commands the way Quasar's console does, as the server, with no player.</summary>
// ReSharper disable once UnusedType.Global
public class MagnetarProbe : IPlugin, IProbeHost
{
    private ProbeCore? _core;

    public void Init(object gameInstance) => _core = new ProbeCore(this);

    public void Update() => _core?.Tick();

    public void Dispose() => _core?.Dispose();

    // The game sees one plugin, Pulsar's loader, which holds the real ones in wrappers. They are private to Magnetar, so they are reached
    // by name. The loader names a plugin's assembly after the plugin, with a random end.
    public object? FindPlugin(string name)
    {
        Type? loader = AppDomain.CurrentDomain.GetAssemblies().Select(a => a.GetType("Pulsar.Legacy.Loader.PluginLoader")).FirstOrDefault(t => t != null);
        object? instance = loader?.GetField("Instance", BindingFlags.Public | BindingFlags.Static)?.GetValue(null);
        if (loader?.GetProperty("Plugins")?.GetValue(instance) is not System.Collections.IEnumerable wrappers)
            return null;
        foreach (object wrapper in wrappers)
        {
            object? plugin = wrapper.GetType().GetField("plugin", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(wrapper);
            if (plugin != null && plugin != this && plugin.GetType().Assembly.GetName().Name!.StartsWith(name + "_", StringComparison.OrdinalIgnoreCase))
                return plugin;
        }
        return null;
    }

    public IEnumerable<CommandInfo> Commands(object plugin)
    {
        foreach (Type module in plugin.GetType().Assembly.GetTypes())
        {
            var root = module.GetCustomAttribute<CommandRootAttribute>();
            if (root == null)
                continue;
            foreach (MethodInfo method in module.GetMethods())
            {
                var command = method.GetCustomAttribute<CommandAttribute>(false);
                if (command != null)
                    yield return new CommandInfo
                    {
                        Line = ("!" + root.Prefix + " " + command.Command).Trim(),
                        Parameters = method.GetParameters().Select(p => (p.ParameterType.Name, p.IsOptional)).ToList(),
                    };
            }
        }
    }

    public List<string> Run(string line)
    {
        var replies = new Replies();
        var console = new CommandCaller(0UL, 0L, "PluginCheck", MyPromoteLevel.Owner, isConsole: true);
        bool handled = ServerCommands.Execute(line, console, replies);
        if (!handled)
            replies.Lines.Add("the command was not handled");
        return replies.Lines;
    }

    private sealed class Replies : ICommandResponder
    {
        public readonly List<string> Lines = new();

        public void Send(in CommandReply reply, in CommandCaller caller) => Lines.Add(reply.Text ?? "");
    }
}
