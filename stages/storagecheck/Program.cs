using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

// Finds how a plugin treats its files and its state, which only starts to matter on a cluster or a network mount: a plugin that
// never saw a mount error on one server can stop a node on a shared folder.
//
// A file call is any call into System.IO that touches the disk. From each thing the game calls on its update thread, calls are
// followed through the plugin's own methods, and an exception only counts as caught if some try on the way has a catch that takes it.
//   UNGUARDED     reached from the game's update with no catch on the way, so a file error goes up into the game
//   GAME-THREAD   reached from the game's update, so a hung mount stops the simulation until it answers
//   READ-DEFAULT  a read whose catch goes on with an empty or default value, in a type that also writes files: the next write replaces the real file
//   NODE-FILE     a path built from the game's user data folder, which each node of a cluster has for itself
//   NODE-STATE    a field keyed by a player, identity, grid or block, which stays on one node when a grid or player moves to another
//
// usage: storagecheck <file with one source path per line> <folder of references>...
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

string[] diskTypes = ["System.IO.File", "System.IO.Directory", "System.IO.FileInfo", "System.IO.DirectoryInfo", "System.IO.FileStream", "System.IO.StreamReader", "System.IO.StreamWriter"];
string[] reads = ["ReadAllText", "ReadAllBytes", "ReadAllLines", "ReadLines", "OpenRead", "OpenText", "Open"];
string[] writes = ["WriteAllText", "WriteAllBytes", "WriteAllLines", "AppendAllText", "Move", "Replace", "Copy", "Create", "CreateText", "OpenWrite", "Delete"];
string[] keyNames = ["player", "identity", "steam", "owner", "grid", "block", "entity", "client", "character"];

var methods = new Dictionary<ISymbol, SyntaxNode>(SymbolEqualityComparer.Default);
foreach (var tree in trees)
{
    var model = compilation.GetSemanticModel(tree);
    foreach (var node in tree.GetRoot().DescendantNodes())
        if (node is BaseMethodDeclarationSyntax or AccessorDeclarationSyntax or LocalFunctionStatementSyntax && model.GetDeclaredSymbol(node) is { } symbol)
            methods[symbol.OriginalDefinition] = node;
}

var findings = new Dictionary<string, object>();
var roots = 0;

foreach (var tree in trees)
{
    var model = compilation.GetSemanticModel(tree);
    foreach (var node in tree.GetRoot().DescendantNodes())
    {
        switch (node)
        {
            case MethodDeclarationSyntax method when GameCallsIt(method, model):
                roots++;
                Walk(method, model, [Where(method)], false, new HashSet<SyntaxNode>());
                break;
            // MySandboxGame.Static.Invoke(...) runs on the update thread, with nothing around it.
            case InvocationExpressionSyntax { Expression: MemberAccessExpressionSyntax { Name.Identifier.Text: "Invoke" } } invoke
                when model.GetTypeInfo(((MemberAccessExpressionSyntax)invoke.Expression).Expression).Type?.Name == "MySandboxGame":
                foreach (var argument in invoke.ArgumentList.Arguments.Where(a => a.Expression is LambdaExpressionSyntax or AnonymousMethodExpressionSyntax))
                {
                    roots++;
                    Walk(argument.Expression, model, [Where(node)], false, new HashSet<SyntaxNode>());
                }
                break;
            case CatchClauseSyntax handler when GoesOnWithDefault(handler) && ReadsFile(handler, model) && WritesFiles(handler, compilation):
                Add("READ-DEFAULT", handler, "a failed read carries on with defaults, and this type also writes files, so the next write can replace the real data", null);
                break;
            case MemberAccessExpressionSyntax { Name.Identifier.Text: "UserDataPath" } path when model.GetTypeInfo(path.Expression).Type?.Name == "MyFileSystem":
                Add("NODE-FILE", path, "each node of a cluster has its own user data folder, so this does not follow a player or a setting to another node", null);
                break;
            case VariableDeclaratorSyntax field when field.Parent?.Parent is FieldDeclarationSyntax && model.GetDeclaredSymbol(field) is IFieldSymbol symbol && HoldsPerThing(symbol):
                Add("NODE-STATE", field, "kept in memory by a key that can move between nodes, so it stays behind in a handover", symbol.Name);
                break;
            case PropertyDeclarationSyntax property when model.GetDeclaredSymbol(property) is { } symbol2 && HoldsPerThing(symbol2):
                Add("NODE-STATE", property, "kept in memory by a key that can move between nodes, so it stays behind in a handover", symbol2.Name);
                break;
        }
    }
}

// The classes of XAML pages, whose generated half this analysis does not compile.
static HashSet<string> XamlClasses(Compilation compilation) => compilation.SyntaxTrees
    .Where(t => t.FilePath.EndsWith(".xaml.cs", StringComparison.OrdinalIgnoreCase))
    .SelectMany(t => t.GetRoot().DescendantNodes().OfType<ClassDeclarationSyntax>()).Select(c => c.Identifier.Text).ToHashSet();

// A control named in a XAML page, which this analysis does not compile.
var errors = compilation.GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error && d.Id != "CS1705" && d.Id != "CS1509" && !d.GetMessage().Contains("InitializeComponent")
    && !(d.Id == "CS0103" && (d.Location.SourceTree?.FilePath.EndsWith(".xaml.cs", StringComparison.OrdinalIgnoreCase) ?? false))
    // A XAML page's class takes its base type from the page, so code using it as a control does not convert here.
    && !(d.Id is "CS0029" or "CS0266" or "CS1503" && XamlClasses(compilation).Any(name => d.GetMessage().Contains("." + name + "'") || d.GetMessage().Contains("'" + name + "'")))
    // A plugin's projects, such as its Magnetar and Torch builds, each declare the assembly's version, and are compiled together here.
    && d.Id != "CS0579").ToList();
Console.WriteLine(JsonSerializer.Serialize(new
{
    files = sources.Length,
    roots,
    findings = findings.Values,
    compileErrors = errors.Count,
    errorSample = errors.Take(5).Select(e => e.ToString()).ToList(),
}));

// What the game itself calls on the update thread: a plugin's Update, a session component's update and a patch.
bool GameCallsIt(MethodDeclarationSyntax method, SemanticModel model)
{
    if (model.GetDeclaredSymbol(method) is not { } symbol || method.Body == null && method.ExpressionBody == null)
        return false;
    var type = symbol.ContainingType;
    if (type.GetAttributes().Concat(symbol.GetAttributes()).Any(a => a.AttributeClass?.Name.StartsWith("Harmony") == true))
        return true;
    bool plugin = type.AllInterfaces.Any(i => i.Name is "IPlugin" or "ITorchPlugin") || Bases(type).Any(t => t.Name is "TorchPluginBase");
    if (plugin && symbol.Name == "Update")
        return true;
    // Chat commands run on the update thread, on Magnetar and on Torch.
    if (symbol.GetAttributes().Any(a => a.AttributeClass?.Name == "CommandAttribute") && Bases(type).Any(t => t.Name == "CommandModule"))
        return true;
    return Bases(type).Any(t => t.Name == "MySessionComponentBase") && symbol.Name is "UpdateBeforeSimulation" or "UpdateAfterSimulation" or "Simulate";
}

IEnumerable<INamedTypeSymbol> Bases(INamedTypeSymbol type)
{
    for (var t = type.BaseType; t != null; t = t.BaseType)
        yield return t;
}

void Walk(SyntaxNode body, SemanticModel model, List<string> path, bool caught, HashSet<SyntaxNode> visited)
{
    if (!visited.Add(body))
        return;
    foreach (var node in body.DescendantNodesAndSelf(n => n == body || n is not (LambdaExpressionSyntax or AnonymousMethodExpressionSyntax or LocalFunctionStatementSyntax)))
    {
        // A method handed over as a delegate, such as GetOrAdd(key, Load), runs inside the call it is handed to.
        bool handedOver = node is IdentifierNameSyntax or MemberAccessExpressionSyntax && node.Parent is ArgumentSyntax;
        if (!(node is InvocationExpressionSyntax or ObjectCreationExpressionSyntax || handedOver))
            continue;
        if (model.GetSymbolInfo(node).Symbol is not IMethodSymbol target)
            continue;
        bool safe = caught || InsideCatchingTry(node, body, model);
        if (IsDiskCall(target))
        {
            Add(safe ? "GAME-THREAD" : "UNGUARDED", node,
                safe ? "a file call on the game thread: a hung mount stops the simulation until it answers"
                     : "a file call on the game thread with no catch on the way up, so a mount or disk error goes into the game and can stop the server",
                string.Join(" > ", path.Append(target.ContainingType.Name + "." + target.Name)));
        }
        else if (methods.TryGetValue(target.OriginalDefinition, out var next))
        {
            Walk(next, compilation.GetSemanticModel(next.SyntaxTree), [.. path, target.Name], safe, visited);
        }
    }
}

// A try counts when a catch takes an IOException, which every file error is or is treated as, or anything wider.
bool InsideCatchingTry(SyntaxNode node, SyntaxNode stop, SemanticModel model)
{
    for (var at = node; at != null && at != stop.Parent; at = at.Parent)
        if (at.Parent is TryStatementSyntax attempt && attempt.Block == at && attempt.Catches.Any(c => Catches(c, model)))
            return true;
    return false;
}

bool Catches(CatchClauseSyntax handler, SemanticModel model)
{
    if (handler.Declaration == null)
        return true;
    var type = model.GetTypeInfo(handler.Declaration.Type).Type;
    return type?.Name is "Exception" or "SystemException" or "IOException" && handler.Filter == null;
}

bool IsDiskCall(IMethodSymbol target)
{
    var type = target.ContainingType?.ToDisplayString();
    return type != null && diskTypes.Contains(type);
}

// The handler returns, or sets something up, without passing the failure on.
bool GoesOnWithDefault(CatchClauseSyntax handler)
{
    if (handler.Block.DescendantNodes().OfType<ThrowStatementSyntax>().Any())
        return false;
    return handler.Block.DescendantNodes().Any(n => n is ObjectCreationExpressionSyntax or ImplicitObjectCreationExpressionSyntax or DefaultExpressionSyntax
        || n is ReturnStatementSyntax { Expression: LiteralExpressionSyntax });
}

bool ReadsFile(CatchClauseSyntax handler, SemanticModel model)
{
    return ((TryStatementSyntax)handler.Parent).Block.DescendantNodes().OfType<InvocationExpressionSyntax>()
        .Select(i => model.GetSymbolInfo(i).Symbol as IMethodSymbol)
        .Any(m => m != null && IsDiskCall(m) && reads.Contains(m.Name));
}

bool WritesFiles(CatchClauseSyntax handler, CSharpCompilation all)
{
    var type = handler.Ancestors().OfType<TypeDeclarationSyntax>().FirstOrDefault();
    if (type == null)
        return false;
    var model = all.GetSemanticModel(handler.SyntaxTree);
    return type.DescendantNodes().OfType<InvocationExpressionSyntax>()
        .Select(i => model.GetSymbolInfo(i).Symbol as IMethodSymbol)
        .Any(m => m != null && IsDiskCall(m) && writes.Contains(m.Name));
}

// A dictionary with a number for a key, under a name that says what the number is. A list of its own game objects is a block's own business.
bool HoldsPerThing(ISymbol symbol)
{
    var type = symbol is IFieldSymbol field ? field.Type : ((IPropertySymbol)symbol).Type;
    if (symbol.IsImplicitlyDeclared || type is not INamedTypeSymbol { IsGenericType: true } named || !named.Name.Contains("Dictionary") || named.TypeArguments.Length != 2)
        return false;
    if (named.TypeArguments[0].SpecialType is not (SpecialType.System_Int64 or SpecialType.System_UInt64))
        return false;
    string name = symbol.Name.ToLowerInvariant() + " " + named.TypeArguments[1].Name.ToLowerInvariant();
    return keyNames.Any(name.Contains);
}

void Add(string kind, SyntaxNode node, string why, string via)
{
    var line = node.GetLocation().GetLineSpan();
    findings.TryAdd(kind + line.Path + ":" + line.StartLinePosition.Line, new { kind, file = line.Path, line = line.StartLinePosition.Line + 1, why, via, code = node.ToString().Split('\n')[0].Trim() });
}

static string Where(SyntaxNode node)
{
    var line = node.GetLocation().GetLineSpan();
    return Path.GetFileName(line.Path) + ":" + (line.StartLinePosition.Line + 1);
}

static bool IsManaged(string file)
{
    try
    {
        System.Reflection.AssemblyName.GetAssemblyName(file);
        return true;
    }
    catch (Exception)
    {
        return false;
    }
}
