# dafny-lemma-eraser

Generate benchmark files from Dafny source files by erasing lemma bodies. Creates two variants:

- **Kind 1 (bodies_erased)**: All lemma bodies replaced with empty bodies `{ }`
- **Kind 2 (helpers_removed)**: Helper lemmas removed entirely, remaining bodies erased

Helper lemmas are those called by other lemmas. Non-helper lemmas (the "main" lemmas that aren't used by others) are kept.

## Prerequisites

- Python 3.8+
- [.NET 8.0 SDK](https://dotnet.microsoft.com/download/dotnet/8.0)
- A Dafny installation (the extractor links against the prebuilt `DafnyCore.dll` / `DafnyDriver.dll`)

## Build

```bash
dotnet build LemmaExtractor.csproj
```

The project references the Dafny DLLs from a Homebrew install (`/opt/homebrew/Cellar/dafny/4.11.0/libexec`) by default. Override via env var or build property if Dafny lives elsewhere:

```bash
DAFNY_LIB_DIR=/path/to/dafny/libexec dotnet build LemmaExtractor.csproj
# or
dotnet build LemmaExtractor.csproj -p:DafnyLibDir=/path/to/dafny/libexec
```

The first build takes ~1 second — no Dafny source clone needed.

## Usage

```bash
python3 dfy_benchmark.py <pattern> <output_dir_kind1> <output_dir_kind2>
```

**Arguments:**
- `pattern` - Glob pattern for .dfy files (e.g., `../dafny-replay/*/*.dfy`)
- `output_dir_kind1` - Output directory for Kind 1 benchmarks (bodies erased)
- `output_dir_kind2` - Output directory for Kind 2 benchmarks (helpers removed)

**Example:**

```bash
python3 dfy_benchmark.py "../dafny-replay/*/*.dfy" bench/bodies_erased bench/helpers_removed
```

## Output

The tool creates standalone .dfy files with all includes inlined:

```
bench/
├── bodies_erased/      # All lemma bodies erased
│   ├── Replay.dfy
│   ├── CounterDomain.dfy
│   └── ...
└── helpers_removed/    # Helper lemmas removed, bodies erased
    ├── Replay.dfy
    ├── CounterDomain.dfy
    └── ...
```

**Example transformation (Kind 1):**

Before:
```dafny
lemma DoPreservesInv(h: History, a: D.Action)
  requires D.Inv(h.present)
  ensures  D.Inv(Do(h, a).present)
{
  D.StepPreservesInv(h.present, a);
}
```

After:
```dafny
lemma DoPreservesInv(h: History, a: D.Action)
  requires D.Inv(h.present)
  ensures  D.Inv(Do(h, a).present)
{
}
```

## How It Works

1. **Python** inlines all `include` statements into one standalone string and writes it to a temp `.dfy` file.
2. **LemmaExtractor (C#)** parses that temp file using Dafny's official AST and returns exact byte offsets for each lemma's declaration and body.
3. **Python** uses those AST offsets to erase bodies (Kind 1) or remove helpers + erase remaining bodies (Kind 2). Mutations are applied right-to-left so positions stay valid.

The C# tool is the single source of truth for "where is the body" — the Python doesn't try to re-find bodies via regex, which previously corrupted spec clauses containing set literals like `m - {p}`.

## File Naming

Output files use the original filename. If there's a collision (same filename in different directories), the parent directory name is prefixed:

- `counter/CounterDomain.dfy` → `CounterDomain.dfy`
- `kanban/KanbanDomain.dfy` → `KanbanDomain.dfy`
- If both had `Domain.dfy` → `counter_Domain.dfy`, `kanban_Domain.dfy`
