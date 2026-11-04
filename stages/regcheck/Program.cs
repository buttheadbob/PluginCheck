using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.Loader;
using PluginSdk.Commands;

// Registers every command module on its own, so one failure cannot hide the ones after it.
var dll = Path.GetFullPath(args[0]);
var ds = args[1];
AssemblyLoadContext.Default.Resolving += (_, n) =>
{
    foreach (var dir in new[] { ds, Path.GetDirectoryName(dll) })
    {
        var p = Path.Combine(dir, n.Name + ".dll");
        if (File.Exists(p)) return AssemblyLoadContext.Default.LoadFromAssemblyPath(p);
    }
    return null;
};
var asm = AssemblyLoadContext.Default.LoadFromAssemblyPath(dll);
var types = asm.GetTypes().Where(t => t.GetCustomAttribute<CommandRootAttribute>() != null).OrderBy(t => t.FullName, StringComparer.Ordinal).ToArray();
var registry = new CommandRegistry();
int ok = 0, bad = 0;
foreach (var t in types)
{
    try { registry.RegisterModule(t, "regcheck"); ok++; Console.WriteLine("OK   " + t.Name); }
    catch (Exception e) { bad++; Console.WriteLine("FAIL " + t.Name + ": " + e.Message); }
}
Console.WriteLine($"{ok} ok, {bad} failed; roots: {string.Join(" ", registry.Prefixes.OrderBy(p => p))}");
// Registration overwrites silently on a repeated path, so look for those too.
var dupes = types.SelectMany(t => t.GetMethods().Select(m => (Root: t.GetCustomAttribute<CommandRootAttribute>().Prefix, Cmd: m.GetCustomAttribute<CommandAttribute>(false)?.Command, M: t.Name + "." + m.Name)))
    .Where(x => x.Cmd != null).GroupBy(x => (x.Root.ToLowerInvariant(), x.Cmd.ToLowerInvariant().Trim())).Where(g => g.Count() > 1).ToList();
Console.WriteLine($"commands: {types.Sum(t => t.GetMethods().Count(m => m.GetCustomAttribute<CommandAttribute>(false) != null))}, duplicate paths: {dupes.Count}");
foreach (var g in dupes) Console.WriteLine("DUP  !" + g.Key.Item1 + " " + g.Key.Item2 + ": " + string.Join(", ", g.Select(x => x.M)));
var fresh = new CommandRegistry();
try { Console.WriteLine("RegisterAssembly: " + fresh.RegisterAssembly(asm) + " modules"); }
catch (Exception e) { Console.WriteLine("RegisterAssembly threw: " + e.Message); }
return bad == 0 ? 0 : 1;
