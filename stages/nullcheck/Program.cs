using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

// The game's assemblies have no nullable annotations, so the compiler stays silent about values that really can be null.
// This lists uses of game members known to return null, directly or through a local variable, with no check before them.
// It compares text, so it can miss a check and report one that is fine. Results are leads to read.
//
// usage: nullcheck <file with one source path per line> <folder of references>...
var sources = File.ReadAllLines(args[0]).Where(l => l.Length > 0).ToArray();
var options = new CSharpParseOptions(LanguageVersion.Latest);
var trees = sources.Select(f => CSharpSyntaxTree.ParseText(File.ReadAllText(f), options, f)).ToList();

var references = new List<MetadataReference>();
var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
var folders = args.Skip(1).ToList();
if (!folders.Any(f => File.Exists(Path.Combine(f, "mscorlib.dll"))))
    folders.Add(Path.GetDirectoryName(typeof(object).Assembly.Location));
foreach (var folder in folders)
    foreach (var dll in Directory.GetFiles(folder, "*.dll").Concat(Directory.GetFiles(folder, "*.exe")))
        if (seen.Add(Path.GetFileNameWithoutExtension(dll)) && IsManaged(dll))
            references.Add(MetadataReference.CreateFromFile(dll));

var compilation = CSharpCompilation.Create("plugin", trees, references, new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary).WithAllowUnsafe(true));

// Why each one can be null, for the report. Add to this as real cases turn up.
var nullable = new Dictionary<string, string>
{
    ["Character"] = "null while the player is dead or on the respawn screen",
    ["Identity"] = "null until the player has one",
    ["Client"] = "null for a player who is not connected",
    ["Controller"] = "can be null while a player is joining or leaving",
    ["ControlledEntity"] = "null when nothing is controlled",
    ["FatBlock"] = "null for armor and other blocks with no entity",
    ["Physics"] = "null for grids and entities with physics off, such as projections",
    ["GetInventory"] = "null for entities with no inventory",
    ["StatComp"] = "null when the character has no stats",
    ["ResourceDistributor"] = "null for grids with no power systems",
    ["GridSystems"] = "can be null while a grid is closing",
    ["GetCubeBlock"] = "null when no block is at that position",
    ["GetFaction"] = "null when there is no such faction",
    ["GetPlayerFaction"] = "null for a player in no faction",
    ["TryGetPlayerFaction"] = "null for a player in no faction",
    ["GetFactionById"] = "null when there is no such faction",
    ["TryGetFactionById"] = "null when there is no such faction",
    ["TryGetPlayerIdentity"] = "null when there is no such player",
    ["TryGetIdentity"] = "null when there is no such identity",
    ["GetIdentity"] = "null when there is no such identity",
    ["GetEntityById"] = "null when the entity is gone",
    ["GetEntityByIdOrDefault"] = "null when the entity is gone",
    ["GetEntityByName"] = "null when there is no such entity",
    ["GetOnlinePlayerByName"] = "null when no player has that name",
    ["GetPlayerById"] = "null when there is no such player",
    ["GetOwnerFaction"] = "null for an unowned block",
    ["FirstOrDefault"] = "null when nothing matches",
    ["LastOrDefault"] = "null when nothing matches",
    ["SingleOrDefault"] = "null when nothing matches",
};
string[] gameNamespaces = ["Sandbox", "SpaceEngineers", "VRage"];

var findings = new Dictionary<string, object>();
var seenRoots = 0;

foreach (var tree in trees)
{
    var model = compilation.GetSemanticModel(tree);
    foreach (var access in tree.GetRoot().DescendantNodes().OfType<MemberAccessExpressionSyntax>())
    {
        var target = Unwrap(access.Expression);
        var why = Reason(target, model, out var label);
        if (why == null || model.GetTypeInfo(target).Type?.IsValueType == true)
            continue;
        seenRoots++;

        var body = access.Ancestors().FirstOrDefault(a => a is BaseMethodDeclarationSyntax or AccessorDeclarationSyntax or LocalFunctionStatementSyntax
            or LambdaExpressionSyntax or PropertyDeclarationSyntax) ?? access;
        var outer = access.Ancestors().FirstOrDefault(a => a is BaseMethodDeclarationSyntax or AccessorDeclarationSyntax or PropertyDeclarationSyntax) ?? body;
        var before = outer.ToString()[..Math.Max(0, access.SpanStart - outer.SpanStart)];
        if (Guarded(target.ToString(), before))
            continue;

        var line = access.GetLocation().GetLineSpan();
        var key = line.Path + ":" + (line.StartLinePosition.Line + 1) + ":" + target;
        findings.TryAdd(key, new { file = line.Path, line = line.StartLinePosition.Line + 1, expression = target.ToString().Trim(), member = label, why });
    }
}

var errors = compilation.GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error
    && d.Id != "CS1705" && d.Id != "CS1509" && !d.GetMessage().Contains("InitializeComponent")).ToList();
var errorSample = errors.GroupBy(d => d.Id).OrderByDescending(g => g.Count()).Take(5).Select(g => $"{g.Count()} {g.Key}: {g.First().GetMessage()}").ToArray();
Console.WriteLine(JsonSerializer.Serialize(new { files = sources.Length, uses = seenRoots, compileErrors = errors.Count, errorSample, findings = findings.Values }));

ExpressionSyntax Unwrap(ExpressionSyntax e) => e is ParenthesizedExpressionSyntax p ? Unwrap(p.Expression) : e;

string Reason(ExpressionSyntax e, SemanticModel model, out string label)
{
    label = null;
    // A plain cast with `as`.
    if (e is BinaryExpressionSyntax b && b.IsKind(SyntaxKind.AsExpression))
    {
        label = "as";
        return "null when the cast fails";
    }

    var symbol = model.GetSymbolInfo(e).Symbol;
    // A local that was given a nullable value.
    if (symbol is ILocalSymbol local && local.DeclaringSyntaxReferences.FirstOrDefault()?.GetSyntax() is VariableDeclaratorSyntax { Initializer.Value: { } value })
    {
        var inner = Unwrap(value);
        if (inner is ConditionalAccessExpressionSyntax)
        {
            label = local.Name + " (from ?.)";
            return "null when the value before the ?. was null";
        }
        return Reason(inner is InvocationExpressionSyntax i ? i : inner, model, out label);
    }

    if (e is InvocationExpressionSyntax invocation)
        symbol = model.GetSymbolInfo(invocation).Symbol;
    if (symbol is not (IMethodSymbol or IPropertySymbol or IFieldSymbol) || !nullable.TryGetValue(symbol.Name, out var why))
        return null;

    var type = symbol.ContainingType;
    var space = (symbol is IMethodSymbol { IsExtensionMethod: true, ReducedFrom: not null } ? "" : type?.ContainingNamespace?.ToDisplayString()) ?? "";
    var linq = type?.Name == "Enumerable" || symbol is IMethodSymbol { IsExtensionMethod: true } && symbol.Name.EndsWith("OrDefault");
    if (!linq && !gameNamespaces.Any(n => space == n || space.StartsWith(n + ".")))
        return null;

    // FirstOrDefault on value types gives a default value, not null.
    if (linq && symbol is IMethodSymbol { ReturnType.IsValueType: true })
        return null;

    label = (type?.Name ?? "") + "." + symbol.Name;
    return why;
}

static bool Guarded(string expression, string before)
{
    var name = Regex.Escape(Regex.Replace(expression, @"\s+", ""));
    var text = Regex.Replace(before, @"\s+", "");
    return Regex.IsMatch(text, name + @"(==null|!=null|isnull|isnotnull|is\{|is\w|\?\.|\?\?)") || Regex.IsMatch(text, @"(null==|null!=)" + name);
}

static bool IsManaged(string path)
{
    try
    {
        using var stream = File.OpenRead(path);
        using var reader = new System.Reflection.PortableExecutable.PEReader(stream);
        return reader.HasMetadata;
    }
    catch (Exception) { return false; }
}
