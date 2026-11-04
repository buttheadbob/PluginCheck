using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

// Finds game objects used off the game thread. Compiles the plugin with Roslyn, starts at everything that runs in the
// background, and follows calls through the plugin's own methods. A lambda handed back to the game thread is safe, so
// the walk stops there. Results are leads: reading a value can be harmless, and only a person can judge that.
//
// usage: threadcheck <file with one source path per line> <folder of references>...
var sources = File.ReadAllLines(args[0]).Where(l => l.Length > 0).ToArray();
var options = new CSharpParseOptions(LanguageVersion.Latest);
var trees = sources.Select(f => CSharpSyntaxTree.ParseText(File.ReadAllText(f), options, f)).ToList();

var references = new List<MetadataReference>();
var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
// Plugins target .NET Framework, so when its reference assemblies are given, the runtime's own must stay out.
var folders = args.Skip(1).ToList();
if (!folders.Any(f => File.Exists(Path.Combine(f, "mscorlib.dll"))))
    folders.Add(Path.GetDirectoryName(typeof(object).Assembly.Location));
foreach (var folder in folders)
    foreach (var dll in Directory.GetFiles(folder, "*.dll").Concat(Directory.GetFiles(folder, "*.exe")))
        if (seen.Add(Path.GetFileNameWithoutExtension(dll)) && IsManaged(dll))
            references.Add(MetadataReference.CreateFromFile(dll));

var compilation = CSharpCompilation.Create("plugin", trees, references, new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary).WithAllowUnsafe(true));

// Types that are safe anywhere, or plain data with no game state behind them.
string[] safeTypes =
[
    "MySandboxGame", "MyLog", "MyFileSystem", "MyDefinitionId", "MyObjectBuilderType", "MyStringId", "MyStringHash", "MyPromoteLevel",
    "MyFontEnum", "MyGameLogicUpdate", "Vector3", "Vector3D", "Vector3I", "Vector2", "Color", "MatrixD", "Matrix", "BoundingBoxD",
    "BoundingBox", "BoundingSphereD", "QuaternionD", "Quaternion",
];
string[] gameNamespaces = ["Sandbox", "SpaceEngineers", "VRage.Game", "VRage.ModAPI", "VRage.Entities"];

var methods = new Dictionary<ISymbol, SyntaxNode>(SymbolEqualityComparer.Default);
foreach (var tree in trees)
{
    var model = compilation.GetSemanticModel(tree);
    foreach (var node in tree.GetRoot().DescendantNodes())
        if (node is BaseMethodDeclarationSyntax or AccessorDeclarationSyntax or LocalFunctionStatementSyntax
            && model.GetDeclaredSymbol(node) is { } symbol)
            methods[symbol.OriginalDefinition] = node;
}

var findings = new Dictionary<string, object>();
var roots = 0;

foreach (var tree in trees)
{
    var model = compilation.GetSemanticModel(tree);
    foreach (var node in tree.GetRoot().DescendantNodes())
    {
        // Task.Run(...), new Thread(...), timers, Parallel and similar: whatever they are given runs in the background.
        if (node is InvocationExpressionSyntax or ObjectCreationExpressionSyntax && Background(node, model))
        {
            var arguments = node is InvocationExpressionSyntax i ? i.ArgumentList : ((ObjectCreationExpressionSyntax)node).ArgumentList;
            foreach (var argument in arguments?.Arguments ?? default)
            {
                roots++;
                if (argument.Expression is LambdaExpressionSyntax or AnonymousMethodExpressionSyntax)
                    Walk(argument.Expression, model, [Where(node)], new HashSet<SyntaxNode>());
                else if (model.GetSymbolInfo(argument.Expression).Symbol is IMethodSymbol target && methods.TryGetValue(target.OriginalDefinition, out var body))
                    Walk(body, compilation.GetSemanticModel(body.SyntaxTree), [Where(node), target.Name], new HashSet<SyntaxNode>());
            }
        }

        // A System.Timers.Timer fires on the thread pool, so its handler is background code.
        if (node is AssignmentExpressionSyntax assignment && assignment.IsKind(SyntaxKind.AddAssignmentExpression)
            && model.GetSymbolInfo(assignment.Left).Symbol is IEventSymbol { Name: "Elapsed" } timer
            && timer.ContainingType?.ToDisplayString() == "System.Timers.Timer")
        {
            roots++;
            if (assignment.Right is LambdaExpressionSyntax or AnonymousMethodExpressionSyntax)
                Walk(assignment.Right, model, [Where(node)], new HashSet<SyntaxNode>());
            else if (model.GetSymbolInfo(assignment.Right).Symbol is IMethodSymbol handler && methods.TryGetValue(handler.OriginalDefinition, out var handlerBody))
                Walk(handlerBody, compilation.GetSemanticModel(handlerBody.SyntaxTree), [Where(node), handler.Name], new HashSet<SyntaxNode>());
        }

        // After the first await, an async method carries on wherever the awaited task finished.
        if (node is BaseMethodDeclarationSyntax { Body: not null } method && method.Modifiers.Any(SyntaxKind.AsyncKeyword)
            && method.Body.DescendantNodes().OfType<AwaitExpressionSyntax>().FirstOrDefault() is { } first)
        {
            roots++;
            foreach (var statement in method.Body.DescendantNodes().OfType<StatementSyntax>().Where(s => s.SpanStart > first.Span.End && s.Parent is BlockSyntax))
                Walk(statement, model, ["after await in " + Where(method)], new HashSet<SyntaxNode>());
        }
    }
}

// XAML stubs and a net10 Harmony build under net48 references cost nothing here: no symbol the walk needs is lost.
var diagnostics = compilation.GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error
    && d.Id != "CS1705" && d.Id != "CS1509" && !d.GetMessage().Contains("InitializeComponent")).ToList();
var errorSample = diagnostics.GroupBy(d => d.Id).OrderByDescending(g => g.Count()).Take(6).Select(g => $"{g.Count()} {g.Key}: {g.First().GetMessage()}").ToArray();
Console.WriteLine(JsonSerializer.Serialize(new { files = sources.Length, roots, compileErrors = diagnostics.Count, errorSample, findings = findings.Values }));

void Walk(SyntaxNode start, SemanticModel model, string[] path, HashSet<SyntaxNode> visited)
{
    if (!visited.Add(start))
        return;

    var skip = new List<SyntaxNode>();
    foreach (var node in start.DescendantNodesAndSelf())
    {
        if (skip.Any(s => s.Span.Contains(node.Span)))
            continue;

        // Handed back to the game thread: nothing inside runs in the background.
        if (node is InvocationExpressionSyntax invocation && Marshals(invocation, model))
        {
            skip.Add(invocation);
            continue;
        }

        if (node is not (SimpleNameSyntax or ObjectCreationExpressionSyntax))
            continue;
        var symbol = model.GetSymbolInfo(node).Symbol;
        if (symbol == null)
            continue;

        if (symbol is IMethodSymbol or IPropertySymbol && methods.TryGetValue(symbol.OriginalDefinition, out var body))
            Walk(body, compilation.GetSemanticModel(body.SyntaxTree), [.. path, symbol.Name], visited);
        else if (symbol is IPropertySymbol property && property.GetMethod is { } getter && methods.TryGetValue(getter.OriginalDefinition, out var accessor))
            Walk(accessor, compilation.GetSemanticModel(accessor.SyntaxTree), [.. path, symbol.Name], visited);
        else if (symbol is IMethodSymbol or IPropertySymbol or IFieldSymbol or IEventSymbol && IsGame(symbol))
        {
            var line = node.GetLocation().GetLineSpan();
            var key = line.Path + ":" + (line.StartLinePosition.Line + 1) + ":" + symbol.Name;
            findings.TryAdd(key, new
            {
                file = line.Path,
                line = line.StartLinePosition.Line + 1,
                member = symbol.ContainingType.Name + "." + symbol.Name,
                path = string.Join(" > ", path),
            });
        }
    }
}

bool Background(SyntaxNode node, SemanticModel model)
{
    var symbol = model.GetSymbolInfo(node).Symbol as IMethodSymbol;
    var type = symbol?.ContainingType?.ToDisplayString() ?? "";
    return node is ObjectCreationExpressionSyntax
        ? type is "System.Threading.Thread" or "System.Threading.Timer" or "System.Threading.Tasks.Task"
        : (type, symbol?.Name) switch
        {
            ("System.Threading.Tasks.Task", "Run") => true,
            ("System.Threading.Tasks.TaskFactory", "StartNew") => true,
            ("System.Threading.ThreadPool", "QueueUserWorkItem") => true,
            ("System.Threading.ThreadPool", "UnsafeQueueUserWorkItem") => true,
            ("System.Threading.Tasks.Parallel", _) => true,
            ("ParallelTasks.Parallel", _) => true,
            ("VRage.Game.ModAPI.IMyParallelTask", _) => true,
            _ => false,
        };
}

bool Marshals(InvocationExpressionSyntax invocation, SemanticModel model)
{
    if (model.GetSymbolInfo(invocation).Symbol is not IMethodSymbol method)
        return false;
    var type = method.ContainingType?.Name ?? "";
    return method.Name is "InvokeSafe" or "RunOnGameThread" or "InvokeOnGameThread" or "InvokeBlocking" or "InvokeAsync" or "ExecuteOnGameThread"
        || (method.Name == "Invoke" && type is "MySandboxGame" or "ITorchBase" or "TorchBase" or "MyGameService" or "IMyUtilities")
        || method.Name == "InvokeOnGameThread";
}

bool IsGame(ISymbol symbol)
{
    var type = symbol.ContainingType;
    if (type == null)
        return false;
    for (var outer = type; outer != null; outer = outer.ContainingType)
        if (safeTypes.Contains(outer.Name))
            return false;
    if (symbol is IMethodSymbol { MethodKind: MethodKind.BuiltinOperator or MethodKind.UserDefinedOperator })
        return false;
    var name = type.ContainingNamespace?.ToDisplayString() ?? "";
    return gameNamespaces.Any(n => name == n || name.StartsWith(n + "."));
}

static string Where(SyntaxNode node)
{
    var line = node.GetLocation().GetLineSpan();
    return Path.GetFileName(line.Path) + ":" + (line.StartLinePosition.Line + 1);
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
