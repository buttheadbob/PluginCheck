using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;

// Checks fixed reflection lookups against the real game assemblies, without running them.
// usage: reflectcheck <lookups.json> <DedicatedServer64 folder>
var lookups = JsonDocument.Parse(File.ReadAllText(args[0])).RootElement;
var game = Directory.GetFiles(args[1], "*.dll");
var runtime = Directory.GetFiles(RuntimeEnvironment.GetRuntimeDirectory(), "*.dll");
var paths = runtime.Concat(game.Where(g => !runtime.Any(r => Path.GetFileName(r).Equals(Path.GetFileName(g), StringComparison.OrdinalIgnoreCase))));

using var context = new MetadataLoadContext(new PathAssemblyResolver(paths), "System.Private.CoreLib");
var types = new Dictionary<string, List<Type>>();
foreach (var path in game.Concat(runtime.Where(r => Path.GetFileName(r).StartsWith("System."))))
{
    Type[] found;
    try { found = context.LoadFromAssemblyPath(path).GetTypes(); }
    catch (ReflectionTypeLoadException e) { found = e.Types.Where(t => t != null).ToArray(); }
    catch (Exception) { continue; } // Native libraries are not assemblies.
    foreach (var type in found)
    {
        var name = type.Name.Split('`')[0];
        if (!types.TryGetValue(name, out var list))
            types[name] = list = [];
        list.Add(type);
    }
}

const BindingFlags Everything = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.Instance;

bool Has(Type type, string kind, string member, BindingFlags flags)
{
    try
    {
        return kind switch
        {
            "Method" => type.GetMethods(flags).Any(m => m.Name == member),
            "Field" => type.GetFields(flags).Any(m => m.Name == member),
            "Property" => type.GetProperties(flags).Any(m => m.Name == member),
            "NestedType" => type.GetNestedTypes(flags).Any(m => m.Name == member),
            _ => false,
        };
    }
    catch (Exception) { return false; } // A base type in an assembly that is not available.
}

var results = new List<object>();
foreach (var lookup in lookups.EnumerateArray())
{
    var typeName = lookup.GetProperty("type").GetString();
    var kind = lookup.GetProperty("kind").GetString();
    var member = lookup.GetProperty("member").GetString();
    var flagNames = lookup.GetProperty("flags").EnumerateArray().Select(f => f.GetString()).ToArray();

    string status;
    if (!types.TryGetValue(typeName.Split('.').Last().Split('<')[0], out var candidates))
        status = "unknown type";
    else if (!candidates.Any(t => Has(t, kind, member, Everything)))
        status = "missing";
    else if (flagNames.Length == 0)
        status = candidates.Any(t => Has(t, kind, member, BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static)) ? "found" : "wrong flags";
    else if (flagNames.Any(f => !Enum.TryParse<BindingFlags>(f, out _)))
        status = "found, flags not checked";
    else
    {
        var flags = flagNames.Select(Enum.Parse<BindingFlags>).Aggregate(BindingFlags.Default, (a, b) => a | b);
        status = candidates.Any(t => Has(t, kind, member, flags)) ? "found" : "wrong flags";
    }

    results.Add(new { file = lookup.GetProperty("file").GetString(), line = lookup.GetProperty("line").GetInt32(), type = typeName, kind, member, status });
}

Console.WriteLine(JsonSerializer.Serialize(results));
