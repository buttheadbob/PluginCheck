using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

// Finds player-level commands that can show a player data about other players. Compiles the plugin with Roslyn, takes every command
// any player can run, and follows calls through the plugin's own methods. It looks for three things, whatever the data is:
//
//   game data    a game call keyed by a player who is not the caller, or a read from someone else's player, grid or faction
//   reply data   anything traced from a named player, or a loop over players, into a reply. This finds a plugin's own stored data,
//                which no list of game members could
//   takesPlayer  an argument that names a player, with nothing traced from it, so a person can read what it does
//
// `eco top` takes no argument and lists everyone, so the argument alone is not enough. All of these are leads, because a public
// leaderboard can be intended. A person or AI must judge each one.
//
// usage: commandcheck <file with one source path per line> <folder of references>...
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

string[] gameNamespaces = ["Sandbox", "SpaceEngineers", "VRage.Game", "VRage.ModAPI", "VRage.Entities"];
string[] playerTypes = ["MyPlayer", "IMyPlayer", "MyIdentity", "IMyIdentity", "MyCharacter", "IMyCharacter"];
// Things a player owns. Only what says where they are, who owns them or what they hold is about a player.
string[] ownedTypes = ["MyCubeGrid", "IMyCubeGrid", "MyCubeBlock", "IMyCubeBlock", "MySlimBlock", "IMySlimBlock", "MyTerminalBlock", "IMyTerminalBlock", "MyFaction", "IMyFaction", "MyEntity", "IMyEntity"];
var ownedMembers = new Regex("^(GetPosition|WorldMatrix|PositionComp|BigOwners|SmallOwners|OwnerId|GetOwner.*|GetInventory|Members|FactionId|Tag)$");
var sinks = new Regex("^(Respond|Reply|Show|ShowMessage|SendMessage|SendChatMessage|SendChatMessageColored|Print|AppendLine|Append|Add)$");
var playerLists = new Regex("^(GetAllIdentities|GetOnlinePlayers|GetAllPlayers|GetPlayers|Identities|Players|Members)$");
// A name is public on any server, and an id only says who, so neither is data about a player by itself.
string[] harmlessMembers = ["DisplayName", "Name", "IdentityId", "Id", "PlayerID", "SteamUserId", "Identity", "ToString", "GetHashCode", "Equals", "Client", "IsBot", "IsRealPlayer"];
var argumentName = new Regex("player|target|user|identity|steam|owner|who|member|victim", RegexOptions.IgnoreCase);
// `name` could be anything: !vote takes a vote and !info takes a topic. It counts once the code uses it to find a player.
var playerFinder = new Regex("Player|Identity|Steam|Member|User|Character");
var keyName = new Regex("identi|player|steam|owner|client|account", RegexOptions.IgnoreCase);
// Finding who someone is comes before reading anything about them, so it is not listed by itself.
var lookups = new Regex("^(TryGet(Identity|IdentityId|IdentityFromSteamId|SteamId|PlayerId|PlayerById|PlayerBySteamId|SerialId|Player)|GetPlayerBy.*|GetPlayerById|FindPlayer.*|IdentityIsNpc|TryGetPlayerIdentity|GetAllIdentities|GetAllPlayers|GetOnlinePlayers|IsPlayerOnline|HasIdentity)$");

var writes = new Regex("^(Change|Set|Add|Remove|Take|Give|Transfer|Clear|Delete|Kill|Kick|Ban)");
// What a warning calls the data. Anything else is named by the game member, so it is still reported.
(Regex Member, string Data)[] dataNames =
[
    (new Regex("Bank|Balance|Account|Currency", RegexOptions.IgnoreCase), "balance"),
    (new Regex("Gps", RegexOptions.IgnoreCase), "GPS list"),
    (new Regex("Inventor", RegexOptions.IgnoreCase), "inventory"),
    (new Regex("Position|WorldMatrix|WorldAABB|Location|Coord", RegexOptions.IgnoreCase), "position"),
    (new Regex("BigOwners|SmallOwners|OwnerId|GetOwner"), "owner"),
    (new Regex("Faction|Members"), "faction"),
    (new Regex("Logout|Login|LastSeen", RegexOptions.IgnoreCase), "last login time"),
    (new Regex("Reputation", RegexOptions.IgnoreCase), "reputation"),
    (new Regex("PCU|BlocksCount"), "block count"),
    (new Regex("Character"), "character"),
];
string[] vagueNames = ["Value", "Key", "Item", "Current", "Count", "Length", "ToString"];

var methods = new Dictionary<ISymbol, SyntaxNode>(SymbolEqualityComparer.Default);
foreach (var tree in trees)
{
    var model = compilation.GetSemanticModel(tree);
    foreach (var node in tree.GetRoot().DescendantNodes())
        if (node is BaseMethodDeclarationSyntax or AccessorDeclarationSyntax or LocalFunctionStatementSyntax
            && model.GetDeclaredSymbol(node) is { } symbol)
            methods[symbol.OriginalDefinition] = node;
}

var commands = new List<object>();
var everyCommand = new List<object>();
var playerCommands = 0;
var allCommands = 0;
foreach (var tree in trees)
{
    var model = compilation.GetSemanticModel(tree);
    foreach (var declaration in tree.GetRoot().DescendantNodes().OfType<MethodDeclarationSyntax>())
    {
        if (model.GetDeclaredSymbol(declaration) is not IMethodSymbol method)
            continue;
        var command = method.GetAttributes().FirstOrDefault(a => a.AttributeClass?.Name == "CommandAttribute"
            && a.AttributeClass.ContainingNamespace?.ToDisplayString() is "PluginSdk.Commands" or "Torch.Commands");
        if (command == null)
            continue;
        allCommands++;
        var place = declaration.GetLocation().GetLineSpan();
        everyCommand.Add(new
        {
            command = "!" + string.Join(" ", Root(method.ContainingType).Append(command.ConstructorArguments.FirstOrDefault().Value as string ?? WrittenName(declaration) ?? method.Name)).Trim(),
            file = place.Path,
            line = place.StartLinePosition.Line + 1,
        });
        if (!IsPlayerLevel(method))
            continue;
        playerCommands++;

        var walk = new CommandWalk();
        foreach (var parameter in method.Parameters)
            if (argumentName.IsMatch(parameter.Name) && parameter.Type.SpecialType is SpecialType.System_String or SpecialType.System_Int64 or SpecialType.System_UInt64)
            {
                walk.TakesPlayer.Add(parameter.Name);
                walk.Tainted.Add(parameter.OriginalDefinition);
            }
            else if (parameter.Name.EndsWith("name", StringComparison.OrdinalIgnoreCase) && parameter.Type.SpecialType == SpecialType.System_String)
                walk.MaybePlayer[parameter.OriginalDefinition] = parameter.Name;
        Walk(declaration, model, [method.Name], walk);
        // Now known to name a player, so it is followed again, this time as one.
        if (walk.FoundPlayer != null)
        {
            walk.TakesPlayer.Add(walk.FoundPlayer);
            foreach (var parameter in method.Parameters.Where(p => p.Name == walk.FoundPlayer))
                walk.Tainted.Add(parameter.OriginalDefinition);
            walk.Visited.Clear();
            Walk(declaration, model, [method.Name], walk);
        }
        if (walk.TakesPlayer.Count == 0 && walk.Reads.Count == 0)
            continue;

        var line = declaration.GetLocation().GetLineSpan();
        commands.Add(new
        {
            command = "!" + string.Join(" ", Root(method.ContainingType).Append(command.ConstructorArguments.FirstOrDefault().Value as string ?? WrittenName(declaration) ?? method.Name)).Trim()
                + string.Concat(method.Parameters.Select(p => p.IsOptional ? $" [{p.Name}]" : $" <{p.Name}>")),
            file = line.Path,
            line = line.StartLinePosition.Line + 1,
            endLine = line.EndLinePosition.Line + 1,
            takesPlayer = walk.TakesPlayer,
            reads = walk.Reads.Values,
        });
    }
}

var diagnostics = compilation.GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error
    && d.Id != "CS1705" && d.Id != "CS1509" && !d.GetMessage().Contains("InitializeComponent")
    // A control named in a XAML page, which this analysis does not compile. The lint stage does cover these.
    && !(d.Id == "CS0103" && (d.Location.SourceTree?.FilePath.EndsWith(".xaml.cs", StringComparison.OrdinalIgnoreCase) ?? false))).ToList();
var errorSample = diagnostics.GroupBy(d => d.Id).OrderByDescending(g => g.Count()).Take(6).Select(g => $"{g.Count()} {g.Key}: {g.First().GetMessage()}").ToArray();
Console.WriteLine(JsonSerializer.Serialize(new { files = sources.Length, allCommands, playerCommands, compileErrors = diagnostics.Count, errorSample, commands, everyCommand }));

void Walk(SyntaxNode start, SemanticModel model, string[] path, CommandWalk walk)
{
    if (!walk.Visited.Add(start))
        return;
    foreach (var returned in start.DescendantNodesAndSelf().OfType<ReturnStatementSyntax>().Where(r => r.Expression != null && model.GetTypeInfo(r.Expression).Type?.SpecialType == SpecialType.System_String))
        Reply(returned.Expression, model, path, walk);
    foreach (var node in start.DescendantNodesAndSelf().OfType<SimpleNameSyntax>())
    {
        var symbol = model.GetSymbolInfo(node).Symbol;
        if (symbol == null)
            continue;

        NoteFinder(node, symbol as IMethodSymbol, model, walk);
        if (sinks.IsMatch(node.Identifier.Text) && Invocation(node) is { } reply)
            foreach (var argument in reply.ArgumentList.Arguments)
                Reply(argument.Expression, model, path, walk);

        if (symbol is IMethodSymbol or IPropertySymbol && methods.TryGetValue(symbol.OriginalDefinition, out var body))
        {
            MarkSelfArguments(node, symbol as IMethodSymbol, model, walk);
            Walk(body, compilation.GetSemanticModel(body.SyntaxTree), [.. path, symbol.Name], walk);
        }
        else if (symbol is IPropertySymbol property && property.GetMethod is { } getter && methods.TryGetValue(getter.OriginalDefinition, out var accessor))
            Walk(accessor, compilation.GetSemanticModel(accessor.SyntaxTree), [.. path, symbol.Name], walk);
        else if (IsGame(symbol) && OtherPlayersData(node, symbol, model, walk))
        {
            var line = node.GetLocation().GetLineSpan();
            walk.Reads.TryAdd(line.Path + ":" + (line.StartLinePosition.Line + 1) + ":" + symbol.Name, new
            {
                member = symbol.ContainingType.Name + "." + symbol.Name,
                data = Describe(symbol),
                verb = writes.IsMatch(symbol.Name) ? "change" : "read",
                file = line.Path,
                line = line.StartLinePosition.Line + 1,
                path = string.Join(" > ", path),
            });
        }
    }
}

// Follows an argument called `name`, to see whether the code goes on to find a player with it.
void NoteFinder(SyntaxNode node, IMethodSymbol method, SemanticModel model, CommandWalk walk)
{
    if (method == null || walk.MaybePlayer.Count == 0 || Invocation(node) is not { } invocation)
        return;
    var definition = (method.ReducedFrom ?? method).OriginalDefinition;
    for (var i = 0; i < invocation.ArgumentList.Arguments.Count && i < definition.Parameters.Length; i++)
    {
        if (model.GetSymbolInfo(invocation.ArgumentList.Arguments[i].Expression).Symbol is not IParameterSymbol parameter
            || !walk.MaybePlayer.TryGetValue(parameter.OriginalDefinition, out var name))
            continue;
        if (playerFinder.IsMatch(method.Name))
            walk.FoundPlayer ??= name;
        else if (methods.ContainsKey(definition))
            walk.MaybePlayer[definition.Parameters[i]] = name;
    }
}

// Anything traced from a named player or a list of players into what the caller is told. This needs no list of data, so it finds a
// plugin's own records as well as the game's.
void Reply(ExpressionSyntax expression, SemanticModel model, string[] path, CommandWalk walk)
{
    var parts = expression is InterpolatedStringExpressionSyntax text
        ? text.Contents.OfType<InterpolationSyntax>().Select(i => i.Expression).ToList()
        : expression is BinaryExpressionSyntax ? expression.DescendantNodes().OfType<ExpressionSyntax>().Where(e => e is not BinaryExpressionSyntax && e.Parent is BinaryExpressionSyntax).ToList()
        : [expression];
    foreach (var part in parts)
    {
        // Saying back what the caller typed tells them nothing.
        if (model.GetSymbolInfo(part).Symbol is IParameterSymbol || FromCaller(part, model, walk, 0) || !FromPlayer(part, model, walk, 0))
            continue;
        // A local says less than what it was set from, so `var v = Homes[player].Visits` is reported as visits.
        var source = part;
        if (model.GetSymbolInfo(part).Symbol is ILocalSymbol local && local.DeclaringSyntaxReferences.FirstOrDefault()?.GetSyntax() is VariableDeclaratorSyntax { Initializer: { } set })
            source = set.Value;
        var name = source.DescendantNodesAndSelf().OfType<SimpleNameSyntax>().Select(n => n.Identifier.Text).LastOrDefault(n => !vagueNames.Contains(n) && !sinks.IsMatch(n));
        if (name == null || harmlessMembers.Contains(name) || name.EndsWith("Name", StringComparison.Ordinal))
            continue;
        var line = part.GetLocation().GetLineSpan();
        walk.Reads.TryAdd(line.Path + ":" + (line.StartLinePosition.Line + 1) + ":" + name, new
        {
            member = name,
            data = dataNames.FirstOrDefault(d => d.Member.IsMatch(name)).Data ?? Words(name),
            verb = "read",
            file = line.Path,
            line = line.StartLinePosition.Line + 1,
            path = string.Join(" > ", path),
        });
    }
}

static string Words(string name)
    => Regex.Replace(name.TrimStart('_').Replace("m_", ""), "(?<=[a-z])(?=[A-Z])", " ").ToLowerInvariant();

// Whether a value traces back to a player the caller named, or to a list of players.
bool FromPlayer(ExpressionSyntax expression, SemanticModel model, CommandWalk walk, int depth)
{
    if (depth > 8)
        return false;
    foreach (var name in expression.DescendantNodesAndSelf().OfType<SimpleNameSyntax>())
    {
        if (playerLists.IsMatch(name.Identifier.Text))
            return true;
        switch (model.GetSymbolInfo(name).Symbol)
        {
            case IParameterSymbol parameter when walk.Tainted.Contains(parameter.OriginalDefinition):
                return true;
            case ILocalSymbol local:
                foreach (var reference in local.DeclaringSyntaxReferences)
                {
                    var declaredIn = compilation.GetSemanticModel(reference.SyntaxTree);
                    if (ValueOf(reference.GetSyntax()) is { } value && FromPlayer(value, declaredIn, walk, depth + 1))
                        return true;
                    if (reference.GetSyntax() is ForEachStatementSyntax loop && FromPlayer(loop.Expression, declaredIn, walk, depth + 1))
                        return true;
                }
                break;
        }
    }
    return false;
}

// A value the plugin passes down from the caller stays the caller's, so a helper taking an identity is not a lead for that path.
void MarkSelfArguments(SyntaxNode node, IMethodSymbol method, SemanticModel model, CommandWalk walk)
{
    if (method == null || Invocation(node) is not { } invocation)
        return;
    for (var i = 0; i < invocation.ArgumentList.Arguments.Count && i < method.Parameters.Length; i++)
    {
        if (FromCaller(invocation.ArgumentList.Arguments[i].Expression, model, walk, 0))
            walk.Self.Add(method.Parameters[i].OriginalDefinition);
        else if (FromPlayer(invocation.ArgumentList.Arguments[i].Expression, model, walk, 0))
            walk.Tainted.Add(method.Parameters[i].OriginalDefinition);
    }
}

bool OtherPlayersData(SimpleNameSyntax node, ISymbol symbol, SemanticModel model, CommandWalk walk)
{
    if (harmlessMembers.Contains(symbol.Name) || lookups.IsMatch(symbol.Name))
        return false;

    if ((playerTypes.Contains(symbol.ContainingType?.Name) || (ownedTypes.Contains(symbol.ContainingType?.Name) && ownedMembers.IsMatch(symbol.Name)))
        && symbol is IMethodSymbol or IPropertySymbol or IFieldSymbol
        && node.Parent is MemberAccessExpressionSyntax access && access.Name == node)
        return !FromCaller(access.Expression, model, walk, 0) && !FilteredToCaller(access.Expression, model, walk);

    // A method that returns nothing sends to a player rather than reading about them, unless it hands data back through an argument.
    if (symbol is not IMethodSymbol method || Invocation(node) is not { } invocation
        || (method.ReturnsVoid && !method.Parameters.Any(p => p.RefKind is RefKind.Out or RefKind.Ref)))
        return false;
    var keyed = false;
    var parameters = (method.ReducedFrom ?? method).Parameters.Skip(method.ReducedFrom != null ? 1 : 0).ToList();
    for (var i = 0; i < invocation.ArgumentList.Arguments.Count && i < parameters.Count; i++)
    {
        var parameter = parameters[i];
        var isKey = (keyName.IsMatch(parameter.Name) && parameter.Type.SpecialType is SpecialType.System_Int64 or SpecialType.System_UInt64)
            || playerTypes.Contains(parameter.Type.Name);
        if (!isKey)
            continue;
        keyed = true;
        if (FromCaller(invocation.ArgumentList.Arguments[i].Expression, model, walk, 0))
            return false;
    }
    return keyed;
}

// Whether a value traces back to whoever ran the command: Context.Player, Context.Caller and so on, through locals.
bool FromCaller(ExpressionSyntax expression, SemanticModel model, CommandWalk walk, int depth)
{
    if (depth > 8)
        return false;
    foreach (var name in expression.DescendantNodesAndSelf().OfType<IdentifierNameSyntax>())
    {
        var symbol = model.GetSymbolInfo(name).Symbol;
        switch (symbol)
        {
            case IPropertySymbol or IFieldSymbol when symbol.Name is "Player" or "Caller" or "Sender" && (symbol.ContainingType?.Name.Contains("Context") ?? false):
                return true;
            case IParameterSymbol parameter when walk.Self.Contains(parameter.OriginalDefinition):
                return true;
            case ILocalSymbol local:
                foreach (var reference in local.DeclaringSyntaxReferences)
                    if (ValueOf(reference.GetSyntax()) is { } value && FromCaller(value, compilation.GetSemanticModel(reference.SyntaxTree), walk, depth + 1))
                        return true;
                break;
        }
    }
    return false;
}

string Describe(ISymbol symbol)
{
    var full = symbol.ContainingType.Name + "." + symbol.Name;
    return dataNames.FirstOrDefault(d => d.Member.IsMatch(full)).Data ?? Words(symbol.Name);
}

// What a local was set from: `var x = y`, or `y is { } x`.
static ExpressionSyntax ValueOf(SyntaxNode declaration)
    => declaration is VariableDeclaratorSyntax { Initializer: { } initializer } ? initializer.Value
        : declaration is SingleVariableDesignationSyntax ? declaration.Ancestors().OfType<IsPatternExpressionSyntax>().FirstOrDefault()?.Expression
        : null;

// Whether code near a local narrows it down to the caller: `if (!grid.BigOwners.Contains(myId)) continue;` leaves only the caller's grids.
bool FilteredToCaller(ExpressionSyntax receiver, SemanticModel model, CommandWalk walk)
{
    var subject = receiver.DescendantNodesAndSelf().OfType<IdentifierNameSyntax>().Select(n => model.GetSymbolInfo(n).Symbol).FirstOrDefault(s => s is ILocalSymbol or IParameterSymbol);
    if (subject == null)
        return false;
    var conditions = receiver.Ancestors().OfType<MemberDeclarationSyntax>().First().DescendantNodes().Select(n => n switch
    {
        IfStatementSyntax test => test.Condition,
        ConditionalExpressionSyntax test => test.Condition,
        InvocationExpressionSyntax { Expression: MemberAccessExpressionSyntax { Name.Identifier.Text: "Where" or "First" or "FirstOrDefault" or "Any" or "Single" or "SingleOrDefault" } } query => query.ArgumentList.Arguments.FirstOrDefault()?.Expression,
        _ => null,
    }).Where(c => c != null);
    return conditions.Any(c => c.DescendantNodesAndSelf().OfType<IdentifierNameSyntax>().Any(n => SymbolEqualityComparer.Default.Equals(model.GetSymbolInfo(n).Symbol, subject))
        && c.DescendantNodesAndSelf().OfType<IdentifierNameSyntax>().Any(n => FromCaller(n, model, walk, 0)));
}

static InvocationExpressionSyntax Invocation(SyntaxNode node)
    => node.Parent switch
    {
        InvocationExpressionSyntax direct when direct.Expression == node => direct,
        MemberAccessExpressionSyntax access when access.Name == node && access.Parent is InvocationExpressionSyntax outer && outer.Expression == access => outer,
        _ => null,
    };

// On both hosts a command with no [Permission] is admin only.
static bool IsPlayerLevel(IMethodSymbol method)
{
    var permission = method.GetAttributes().Concat(method.ContainingType.GetAttributes()).FirstOrDefault(a => a.AttributeClass?.Name == "PermissionAttribute");
    return permission != null && permission.ConstructorArguments.FirstOrDefault().Value is int and 0;
}

// The name as written in [Command("...")], for when the attribute is ambiguous because both hosts are compiled together, so the
// symbol has no constructor arguments. Without it the method's own name is used, which is not what Torch or Magnetar matches.
static string? WrittenName(MethodDeclarationSyntax declaration) =>
    declaration.AttributeLists.SelectMany(l => l.Attributes).FirstOrDefault(a => a.Name.ToString() is "Command" or "CommandAttribute")
        ?.ArgumentList?.Arguments.FirstOrDefault()?.Expression is LiteralExpressionSyntax literal ? literal.Token.ValueText : null;

static IEnumerable<string> Root(INamedTypeSymbol type)
{
    foreach (var attribute in type.GetAttributes())
    {
        if (attribute.AttributeClass?.Name == "CommandRootAttribute")
            return [attribute.ConstructorArguments.First().Value as string];
        if (attribute.AttributeClass?.Name == "CategoryAttribute")
        {
            var argument = attribute.ConstructorArguments.First();
            return argument.Kind == TypedConstantKind.Array ? argument.Values.Select(v => v.Value as string) : attribute.ConstructorArguments.Select(v => v.Value as string);
        }
    }
    return [];
}

bool IsGame(ISymbol symbol)
{
    var name = symbol.ContainingType?.ContainingNamespace?.ToDisplayString() ?? "";
    return gameNamespaces.Any(n => name == n || name.StartsWith(n + "."));
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

sealed class CommandWalk
{
    public readonly HashSet<SyntaxNode> Visited = [];
    public readonly HashSet<ISymbol> Self = new(SymbolEqualityComparer.Default);
    public readonly HashSet<ISymbol> Tainted = new(SymbolEqualityComparer.Default);
    public readonly Dictionary<ISymbol, string> MaybePlayer = new(SymbolEqualityComparer.Default);
    public string FoundPlayer;
    public readonly List<string> TakesPlayer = [];
    public readonly Dictionary<string, object> Reads = [];
}
