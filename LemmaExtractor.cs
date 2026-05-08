// LemmaExtractor.cs - Extract lemma positions from Dafny files
// Outputs JSON for use by Python benchmark generator

using System.Text.Json;
using Microsoft.Dafny;
using static Microsoft.Dafny.DafnyMain;

namespace LemmaExtractor;

public record LemmaInfo(
    string Name,
    string ModuleName,
    int StartPos,       // Start of "lemma" keyword (byte offset in file)
    int EndPos,         // End of lemma (after closing brace or declaration)
    int BodyStartPos,   // Start of body '{' (-1 if no body)
    int BodyEndPos,     // End of body '}' (-1 if no body)
    bool HasBody
);

public record FileResult(
    string FilePath,
    List<LemmaInfo> Lemmas,
    string? Error
);

public static class Extractor
{
    public static FileResult ExtractLemmas(Microsoft.Dafny.Program program, string filePath)
    {
        var lemmas = new List<LemmaInfo>();

        // When parsing produces resolution errors, Dafny may return a partial
        // AST with null entries. Iterate defensively so we still emit what we
        // could parse instead of crashing the whole run.
        IEnumerable<ModuleDefinition> modules;
        try { modules = program.Modules().Where(m => m != null).ToList(); }
        catch { modules = new List<ModuleDefinition>(); }

        foreach (var module in modules)
        {
            if (module.TopLevelDecls == null) continue;
            foreach (var decl in module.TopLevelDecls)
            {
                if (decl == null) continue;
                try { ExtractFromDecl(decl, module.Name ?? "", lemmas); }
                catch { /* skip malformed decl */ }
            }
        }

        return new FileResult(filePath, lemmas, null);
    }

    static void ExtractFromDecl(TopLevelDecl decl, string moduleName, List<LemmaInfo> lemmas)
    {
        if (decl is TopLevelDeclWithMembers memberDecl)
        {
            foreach (var member in memberDecl.Members)
            {
                if (member is Lemma lemma)
                {
                    var info = ExtractLemmaInfo(lemma, moduleName);
                    if (info != null)
                        lemmas.Add(info);
                }
            }
        }
    }

    static LemmaInfo? ExtractLemmaInfo(Lemma lemma, string moduleName)
    {
        // Get source positions from tokens
        var startTok = lemma.StartToken;
        var endTok = lemma.EndToken;

        if (startTok == null || endTok == null)
            return null;

        int startPos = startTok.pos;
        int endPos = endTok.pos + (endTok.val?.Length ?? 1);

        // Body position
        int bodyStartPos = -1;
        int bodyEndPos = -1;
        bool hasBody = lemma.Body != null;

        if (lemma.Body != null)
        {
            var bodyTok = lemma.Body.StartToken;
            var bodyEndTok = lemma.Body.EndToken;
            if (bodyTok != null && bodyEndTok != null)
            {
                bodyStartPos = bodyTok.pos;
                bodyEndPos = bodyEndTok.pos + (bodyEndTok.val?.Length ?? 1);
            }
        }

        return new LemmaInfo(
            lemma.Name,
            moduleName,
            startPos,
            endPos,
            bodyStartPos,
            bodyEndPos,
            hasBody
        );
    }
}

class Program
{
    static async Task<int> Main(string[] args)
    {
        if (args.Length < 1)
        {
            Console.Error.WriteLine("Usage: LemmaExtractor <file.dfy> [--output <file.json>]");
            Console.Error.WriteLine("       LemmaExtractor --batch <file1.dfy> <file2.dfy> ... [--output <file.json>]");
            return 1;
        }

        bool batchMode = args[0] == "--batch";
        string? outputPath = null;
        var filePaths = new List<string>();

        // Parse arguments
        for (int i = batchMode ? 1 : 0; i < args.Length; i++)
        {
            if (args[i] == "--output" && i + 1 < args.Length)
            {
                outputPath = args[++i];
            }
            else if (!args[i].StartsWith("--"))
            {
                filePaths.Add(args[i]);
            }
        }

        if (filePaths.Count == 0)
        {
            Console.Error.WriteLine("No input files specified");
            return 1;
        }

        var results = new List<FileResult>();

        foreach (var filePath in filePaths)
        {
            var result = await ProcessFile(filePath);
            results.Add(result);
        }

        // Output JSON
        var jsonOptions = new JsonSerializerOptions
        {
            WriteIndented = true,
            PropertyNamingPolicy = JsonNamingPolicy.CamelCase
        };

        string json;
        if (batchMode || filePaths.Count > 1)
        {
            json = JsonSerializer.Serialize(results, jsonOptions);
        }
        else
        {
            json = JsonSerializer.Serialize(results[0], jsonOptions);
        }

        if (outputPath != null)
        {
            await File.WriteAllTextAsync(outputPath, json);
            Console.Error.WriteLine($"Wrote: {outputPath}");
        }
        else
        {
            Console.WriteLine(json);
        }

        return results.Any(r => r.Error != null) ? 1 : 0;
    }

    static async Task<FileResult> ProcessFile(string filePath)
    {
        var program = await ParseDafnyFileAsync(filePath);
        if (program == null)
        {
            return new FileResult(filePath, new List<LemmaInfo>(), $"Failed to parse {filePath}");
        }

        return Extractor.ExtractLemmas(program, filePath);
    }

    static async Task<Microsoft.Dafny.Program?> ParseDafnyFileAsync(string filePath)
    {
        var options = DafnyOptions.Default;
        var reporter = new ConsoleErrorReporter(options);

        var dafnyFile = DafnyFile.HandleDafnyFile(
            OnDiskFileSystem.Instance,
            reporter,
            options,
            new Uri(Path.GetFullPath(filePath)),
            Token.NoToken
        );

        if (dafnyFile == null)
        {
            await Console.Error.WriteLineAsync($"Failed to load Dafny file: {filePath}");
            return null;
        }

        var files = new List<DafnyFile> { dafnyFile };
        var (program, error) = await ParseCheck(
            TextReader.Null,
            files,
            "lemma-extractor",
            options
        );

        if (error != null)
        {
            await Console.Error.WriteLineAsync($"Parse error in {filePath}: {error}");
        }

        return program;
    }
}
