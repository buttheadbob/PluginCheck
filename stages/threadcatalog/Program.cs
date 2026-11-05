using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using Mono.Cecil;
using Mono.Cecil.Cil;

// Lists game members whose behaviour depends on which thread calls them, read from the game's own IL so it stays true
// after a game update. Seeds are methods that compare Thread.CurrentThread with the update thread, read
// MyEntities.IsAsyncUpdateInProgress, or read a [ThreadStatic] field that holds state. Then callers are followed, because
// MyCubeGrid.GetOwnerLogoutTimeSeconds changes with the thread too, though it never looks at one: it reads BigOwners.
//
// usage: threadcatalog <game folder> <output json> [--stats]
var folder = args[0];
var output = args[1];
var stats = args.Contains("--stats");

// How far callers are followed. Nearly everything in the game calls something that checks a thread if this is unbounded,
// and a list of everything says nothing. Values are followed further than effects, since a returned value is what a plugin
// reads and goes on to act on.
const int ValueDepth = 4;
const int EffectDepth = 1;

var fileNames = Directory.GetFiles(folder, "*.dll").Concat(Directory.GetFiles(folder, "*.exe"))
    .Where(f => Path.GetFileName(f) is var n && (n.StartsWith("Sandbox.") || n.StartsWith("SpaceEngineers") || n.StartsWith("VRage")))
    .Where(f => !f.Contains("XmlSerializers"))
    .OrderBy(f => f, StringComparer.Ordinal).ToList();

var resolver = new SharedResolver();
resolver.AddSearchDirectory(folder);
var assemblies = new List<AssemblyDefinition>();
foreach (var file in fileNames)
{
    AssemblyDefinition assembly;
    try { assembly = AssemblyDefinition.ReadAssembly(file, new ReaderParameters { AssemblyResolver = resolver, ReadingMode = ReadingMode.Deferred }); }
    catch (BadImageFormatException) { continue; }
    assemblies.Add(assembly);
    // Without this the resolver loads its own copy, and a call between two assemblies lands on a method nothing else knows about.
    resolver.Keep(assembly);
}

var gameNames = assemblies.Select(a => a.Name.Name).ToHashSet();

// Lambdas, iterators and local functions are compiled into their own methods. What they do belongs to the member they sit in.
var owners = new Dictionary<MethodDefinition, MethodDefinition>();
var all = new List<MethodDefinition>();
foreach (var type in assemblies.SelectMany(a => a.Modules).SelectMany(m => m.GetTypes()))
    all.AddRange(type.Methods);

// Most [ThreadStatic] fields are scratch buffers, which every thread builds for itself and which give the same answer anywhere.
// A field counts as state when something other than a fresh object is ever stored in it. MyEntityIdentifier.m_perThreadData does:
// it is swapped and shared, and which entities a lookup can find depends on it.
var threadStatic = all.Select(m => m.DeclaringType).Distinct().SelectMany(t => t.Fields)
    .Where(f => f.IsStatic && f.CustomAttributes.Any(a => a.AttributeType.Name == "ThreadStaticAttribute")).ToHashSet();
var stateful = new HashSet<FieldDefinition>();
foreach (var method in all.Where(m => m.HasBody))
    foreach (var instruction in method.Body.Instructions)
        if (instruction.OpCode == OpCodes.Stsfld && instruction.Operand is FieldReference { } stored
            && stored.Resolve() is { } field && threadStatic.Contains(field)
            && !FreshObject(instruction))
            stateful.Add(field);

var callers = new Dictionary<MethodDefinition, HashSet<MethodDefinition>>();
var seeds = new Dictionary<MethodDefinition, HashSet<string>>();
var valueUsed = new HashSet<(MethodDefinition caller, MethodDefinition callee)>();

foreach (var method in all.Where(m => m.HasBody))
{
    var named = Named(method);
    var readsCurrent = false;
    var readsUpdate = false;
    var readsAsync = false;
    var compares = false;
    var instructions = method.Body.Instructions;
    for (var i = 0; i < instructions.Count; i++)
    {
        var instruction = instructions[i];
        if (instruction.OpCode == OpCodes.Ldsfld && instruction.Operand is FieldReference read && read.Resolve() is { } readField && stateful.Contains(readField))
            Seed(named, $"reads the [ThreadStatic] field {readField.DeclaringType.Name}.{readField.Name}");
        if (instruction.Operand is not MethodReference call || instruction.OpCode.FlowControl != FlowControl.Call)
            continue;

        var full = call.DeclaringType.FullName + "::" + call.Name;
        if (full == "System.Threading.Thread::get_CurrentThread")
        {
            readsCurrent = true;
            compares |= Compared(instruction);
        }
        else if (call.Name == "get_UpdateThread")
        {
            readsUpdate = true;
            compares |= Compared(instruction);
        }
        else if (full == "Sandbox.Game.Entities.MyEntities::get_IsAsyncUpdateInProgress")
            readsAsync = true;

        var target = Resolve(call);
        if (target == null)
            continue;
        target = Named(target);
        if (target == named)
            continue;
        Add(callers, target, named);
        // A result dropped straight away is not a value the caller depends on.
        if (!IsVoid(call) && instruction.Next?.OpCode != OpCodes.Pop)
            valueUsed.Add((named, target));
    }

    // A debug assert compiles down to reading both and dropping them, and the game's own start-up stores one into the other.
    if (readsCurrent && readsUpdate && compares)
        Seed(named, "compares the calling thread with the game's update thread");
    if (readsAsync)
        Seed(named, "reads MyEntities.IsAsyncUpdateInProgress");
}

// An interface or base method can land on a sensitive override, so a call through it counts as well.
var above = new Dictionary<MethodDefinition, HashSet<MethodDefinition>>();
foreach (var method in all.Where(m => m.IsVirtual))
    foreach (var parent in Parents(method))
        if (Resolve(parent) is { } definition)
            Add(above, method, Named(definition));

var entries = new Dictionary<MethodDefinition, Entry>();
var queue = new Queue<MethodDefinition>();
foreach (var (method, reasons) in seeds)
{
    entries[method] = new Entry(string.Join("; ", reasons.OrderBy(r => r, StringComparer.Ordinal)), 0, null, IsValue(method));
    queue.Enqueue(method);
}

while (queue.Count > 0)
{
    var method = queue.Dequeue();
    var entry = entries[method];

    // Most overrides of an update method play a sound somewhere, which says nothing about MyEntity.Close. So a side effect only
    // climbs from the member that checks the thread. A value climbs from anywhere.
    if (entry.Value || entry.Depth == 0)
        foreach (var parent in above.GetValueOrDefault(method, []))
            Reach(parent, entry with { Via = method });

    if (entry.Depth >= (entry.Value ? ValueDepth : EffectDepth))
        continue;
    foreach (var caller in callers.GetValueOrDefault(method, []))
    {
        // A caller that drops the value, or returns nothing, is listed but goes no further than the effect limit. A getter counts
        // even when its callee returns nothing: SmallOwners runs a recalculation that is skipped off the game thread, so what it
        // returns still depends on the thread.
        var value = IsValue(caller) && ((entry.Value && valueUsed.Contains((caller, method))) || caller.IsGetter);
        Reach(caller, new Entry(entry.Seed, entry.Depth + 1, method, value));
    }
}

var list = entries.Where(e => !e.Key.DeclaringType.FullName.StartsWith("<")).Select(e => new
{
    id = Id(e.Key),
    member = Show(e.Key),
    depth = e.Value.Depth,
    value = e.Value.Value,
    seed = e.Value.Seed,
    via = Chain(e.Key),
}).OrderBy(e => e.depth).ThenBy(e => e.id, StringComparer.Ordinal).ToList();

File.WriteAllText(output, JsonSerializer.Serialize(new { assemblies = assemblies.Count, methods = all.Count, seeds = seeds.Count, ValueDepth, EffectDepth, members = list },
    new JsonSerializerOptions { WriteIndented = true }));

Console.WriteLine($"{assemblies.Count} assemblies, {all.Count} methods, {seeds.Count} seeds, {list.Count} catalogued members");
if (stats)
{
    Console.WriteLine($"[ThreadStatic] fields: {threadStatic.Count}, holding state: {stateful.Count}");
    foreach (var field in stateful.OrderBy(f => f.FullName, StringComparer.Ordinal))
        Console.WriteLine($"  {field.DeclaringType.FullName}.{field.Name} : {field.FieldType.Name}");
    foreach (var group in list.GroupBy(e => e.depth).OrderBy(g => g.Key))
        Console.WriteLine($"depth {group.Key}: {group.Count()} ({group.Count(e => e.value)} values)");
    foreach (var e in list.Where(e => e.depth == 0))
        Console.WriteLine($"  {e.member}  [{(e.value ? "value" : "effect")}]");
}

void Reach(MethodDefinition method, Entry entry)
{
    // A value outranks a side effect at any depth, since only a value carries on to the callers beyond the effect limit.
    if (entries.TryGetValue(method, out var known) && (known.Value == entry.Value ? known.Depth <= entry.Depth : known.Value))
        return;
    entries[method] = entry;
    queue.Enqueue(method);
}

string Chain(MethodDefinition method)
{
    var parts = new List<string>();
    var seen = new HashSet<MethodDefinition>();
    for (var at = method; at != null && seen.Add(at); at = entries.GetValueOrDefault(at)?.Via)
        if (parts.LastOrDefault() != Show(at))
            parts.Add(Show(at));
    return string.Join(" > ", parts);
}

void Seed(MethodDefinition method, string reason)
{
    if (!seeds.TryGetValue(method, out var reasons))
        seeds[method] = reasons = [];
    reasons.Add(reason);
}

MethodDefinition Named(MethodDefinition method)
{
    if (owners.TryGetValue(method, out var known))
        return known;
    var result = method;
    var name = method.Name;
    var type = method.DeclaringType;

    // A local function or lambda: <Outer>b__0, <Outer>g__Local|0_0.
    if (name.StartsWith("<"))
    {
        var end = name.IndexOf('>');
        var outer = name.Substring(1, end - 1);
        var host = type;
        while (host != null && IsGenerated(host))
            host = host.DeclaringType;
        if (host != null)
            result = host.Methods.FirstOrDefault(m => m.Name == outer) ?? result;
    }
    // A closure, iterator or async state machine: <Outer>d__0 or <>c__DisplayClass0_0.
    else if (IsGenerated(type))
    {
        var host = type;
        while (host != null && IsGenerated(host))
            host = host.DeclaringType;
        var outer = type.Name.StartsWith("<") && !type.Name.StartsWith("<>") ? type.Name.Substring(1, type.Name.IndexOf('>') - 1) : null;
        if (host != null && outer != null)
            result = host.Methods.FirstOrDefault(m => m.Name == outer) ?? result;
    }
    owners[method] = result;
    return result;
}

// Whether a thread just read goes straight into a comparison, directly or by its ManagedThreadId.
static bool Compared(Instruction read)
{
    var next = read.Next;
    if (next?.Operand is MethodReference { Name: "get_ManagedThreadId" })
        next = next.Next;
    if (next == null)
        return false;
    return next.OpCode.Code is Code.Ceq or Code.Beq or Code.Beq_S or Code.Bne_Un or Code.Bne_Un_S
        || next.Operand is MethodReference { Name: "op_Equality" or "op_Inequality" };
}

// Whether a store puts in a new object, or nothing, which is how a thread sets up or drops its own buffer. The ?? form has a dup first.
static bool FreshObject(Instruction store)
{
    var value = store.Previous;
    if (value?.OpCode == OpCodes.Dup)
        value = value.Previous;
    return value?.OpCode.Code is Code.Newobj or Code.Newarr or Code.Ldnull;
}

static bool IsGenerated(TypeDefinition type) => type.Name.StartsWith("<");

// Only the game's own assemblies are read. The folder also holds .NET facades that forward to each other, and following those
// recurses until the process dies. Nothing outside the game can check a game thread anyway.
bool InGame(TypeReference type) => type.GetElementType().Scope switch
{
    AssemblyNameReference name => gameNames.Contains(name.Name),
    ModuleDefinition module => gameNames.Contains(module.Assembly.Name.Name),
    _ => false,
};

MethodDefinition Resolve(MethodReference reference) => InGame(reference.DeclaringType) ? reference.Resolve() : null;

IEnumerable<MethodReference> Parents(MethodDefinition method)
{
    foreach (var explicitly in method.Overrides)
        yield return explicitly;

    var type = method.DeclaringType;
    if (method.IsReuseSlot)
        for (var parent = Base(type); parent != null; parent = Base(parent))
        {
            var match = parent.Methods.FirstOrDefault(m => m.IsVirtual && m.Name == method.Name && Same(m, method));
            if (match != null)
            {
                yield return match;
                break;
            }
        }

    if (method.IsPublic)
        for (var at = type; at != null; at = Base(at))
            foreach (var face in at.Interfaces)
            {
                var definition = ResolveType(face.InterfaceType);
                var match = definition?.Methods.FirstOrDefault(m => m.Name == method.Name && Same(m, method));
                if (match != null)
                    yield return match;
            }
}

TypeDefinition Base(TypeDefinition type) => type.BaseType == null ? null : ResolveType(type.BaseType);

TypeDefinition ResolveType(TypeReference reference) => InGame(reference) ? reference.Resolve() : null;

static bool Same(MethodDefinition a, MethodDefinition b)
    => a.Parameters.Count == b.Parameters.Count
    && a.Parameters.Select(p => p.ParameterType.FullName).SequenceEqual(b.Parameters.Select(p => p.ParameterType.FullName));

static bool IsVoid(MethodReference method) => method.ReturnType.MetadataType == MetadataType.Void;

// A value: something returned, or handed back through an out parameter.
static bool IsValue(MethodDefinition method) => !IsVoid(method) || method.Parameters.Any(p => p.IsOut);

static void Add(Dictionary<MethodDefinition, HashSet<MethodDefinition>> map, MethodDefinition key, MethodDefinition value)
{
    if (!map.TryGetValue(key, out var set))
        map[key] = set = [];
    set.Add(value);
}

static string Show(MethodDefinition method)
{
    var name = method.Name;
    // An explicit interface implementation carries the interface's whole name.
    name = name.Substring(name.LastIndexOf('.') + 1);
    if (name.StartsWith("get_") || name.StartsWith("set_"))
        name = name.Substring(4);
    return method.DeclaringType.Name + "." + (name.Length > 0 ? name : method.Name);
}

// The same id Roslyn gives with GetDocumentationCommentId, so a plugin's calls can be looked up directly.
static string Id(MethodDefinition method)
{
    var text = new StringBuilder("M:").Append(TypeId(method.DeclaringType, false)).Append('.').Append(method.Name.Replace('.', '#').Replace('<', '{').Replace('>', '}'));
    if (method.HasGenericParameters)
        text.Append("``").Append(method.GenericParameters.Count);
    if (method.HasParameters)
        text.Append('(').Append(string.Join(",", method.Parameters.Select(p => TypeId(p.ParameterType, true)))).Append(')');
    if (method.Name is "op_Implicit" or "op_Explicit")
        text.Append('~').Append(TypeId(method.ReturnType, true));
    return text.ToString();
}

static string TypeId(TypeReference type, bool signature)
{
    switch (type)
    {
        case ByReferenceType byReference: return TypeId(byReference.ElementType, signature) + "@";
        case PointerType pointer: return TypeId(pointer.ElementType, signature) + "*";
        case ArrayType array:
            return TypeId(array.ElementType, signature) + (array.Rank == 1 ? "[]" : "[" + string.Join(",", Enumerable.Repeat("0:", array.Rank)) + "]");
        case GenericParameter parameter:
            return (parameter.Owner is MethodReference ? "``" : "`") + parameter.Position;
        case RequiredModifierType required: return TypeId(required.ElementType, signature);
        case OptionalModifierType optional: return TypeId(optional.ElementType, signature);
        case GenericInstanceType generic:
            var arguments = generic.GenericArguments.ToList();
            return NameWithArguments(generic.ElementType, arguments, signature);
    }
    return NameWithArguments(type, null, signature);
}

static string NameWithArguments(TypeReference type, List<TypeReference> arguments, bool signature)
{
    var parts = new List<string>();
    var skip = arguments?.Count ?? 0;
    for (var at = type; at != null; at = at.DeclaringType)
    {
        var name = at.Name;
        var tick = name.IndexOf('`');
        var count = tick >= 0 ? int.Parse(name.Substring(tick + 1)) : 0;
        if (tick >= 0 && arguments != null)
        {
            skip -= count;
            name = name.Substring(0, tick) + "{" + string.Join(",", arguments.Skip(skip).Take(count).Select(a => TypeId(a, signature))) + "}";
        }
        parts.Insert(0, name);
        if (at.DeclaringType == null && at.Namespace.Length > 0)
            parts.Insert(0, at.Namespace);
    }
    return string.Join(".", parts);
}

record Entry(string Seed, int Depth, MethodDefinition Via, bool Value);

class SharedResolver : DefaultAssemblyResolver
{
    public void Keep(AssemblyDefinition assembly) => RegisterAssembly(assembly);
}
