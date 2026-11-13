using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Torch;
using Torch.API;
using Torch.API.Managers;
using Torch.API.Plugins;
using Torch.Commands;

namespace PluginCheckProbe
{
    /// <summary>The probe on Torch. It runs commands as Torch's own console does.</summary>
    // ReSharper disable once UnusedType.Global
    public class TorchProbe : TorchPluginBase, IProbeHost
    {
        // behaviour.py reads this. A reply starting with it comes from PluginCheck, not the plugin.
        private const string WrongArguments = "PluginCheck: Torch passed ";

        private ProbeCore _core;

        public override void Init(ITorchBase torch)
        {
            base.Init(torch);
            _core = new ProbeCore(this);
        }

        public override void Update() => _core?.Tick();

        public override void Dispose()
        {
            _core?.Dispose();
            base.Dispose();
        }

        public object FindPlugin(string name)
        {
            foreach (ITorchPlugin plugin in Torch.Managers.GetManager<IPluginManager>())
                if (!ReferenceEquals(plugin, this) && string.Equals(plugin.Name, name, StringComparison.OrdinalIgnoreCase))
                    return plugin;
            return null;
        }

        public IEnumerable<CommandInfo> Commands(object plugin)
        {
            foreach (Type module in plugin.GetType().Assembly.GetTypes().Where(t => typeof(CommandModule).IsAssignableFrom(t)))
            {
                var category = module.GetCustomAttribute<CategoryAttribute>();
                foreach (MethodInfo method in module.GetMethods())
                {
                    var command = method.GetCustomAttribute<CommandAttribute>();
                    if (command == null)
                        continue;
                    // Name is only the last word of [Command("rep wipe")]. Path has all of them.
                    var path = (category?.Path ?? new List<string>()).Concat(command.Path);
                    yield return new CommandInfo
                    {
                        Line = "!" + string.Join(" ", path),
                        Parameters = method.GetParameters().Select(p => (p.ParameterType.Name, p.IsOptional)).ToList(),
                    };
                }
            }
        }

        public List<string> Run(string line)
        {
            var replies = new List<string>();
            var manager = Torch.CurrentSession.Managers.GetManager<CommandManager>();
            string wrong = ArgumentsCheck(manager, line);
            if (wrong != null)
                replies.Add(wrong);
            if (!manager.HandleCommandFromServer(line, message => replies.Add(message.Message)))
                replies.Add("the command was not handled");
            return replies;
        }

        // Torch finds the arguments by searching the whole line for the first one, so one that also appears in the command's own words starts too early.
        private static string ArgumentsCheck(CommandManager manager, string line)
        {
            string text = line.Substring(1);
            Command command = manager.Commands.GetCommand(text, out string argText);
            if (command == null)
                return null;
            string typed = string.Join(" ", Words(text).Skip(command.Path.Count));
            string passed = string.Join(" ", Words(argText));
            return passed == typed ? null : $"{WrongArguments}'{passed}' for '{typed}'";
        }

        private static string[] Words(string text) => text.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
    }
}
