# dafny-lemma-eraser

Generate benchmark files from Dafny source files by erasing lemma bodies. Creates two variants:

- **Kind 1 (bodies_erased)**: All lemma bodies replaced with empty bodies `{ }`
- **Kind 2 (helpers_removed)**: Helper lemmas removed entirely, remaining bodies erased

Helper lemmas are those called by other lemmas. Non-helper lemmas (the "main" lemmas that aren't used by others) are kept.

## Prerequisites

- Python 3.8+
- [.NET 8.0 SDK](https://dotnet.microsoft.com/download/dotnet/8.0)

## Setup

Clone the Dafny compiler source (required for AST parsing):

```bash
git clone --depth 1 https://github.com/dafny-lang/dafny.git
```

## Build

Build the LemmaExtractor tool:

```bash
dotnet build LemmaExtractor.csproj
```

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

1. **LemmaExtractor (C#)** parses original Dafny files using the Dafny compiler's AST to accurately identify lemmas
2. **Python** inlines all `include` statements to create standalone files
3. **Python** finds lemmas by name in the inlined content and locates their body positions
4. **Python** erases bodies or removes helper lemmas based on the benchmark type

This hybrid approach ensures accurate lemma detection while handling the complexity of include resolution.

## File Naming

Output files use the original filename. If there's a collision (same filename in different directories), the parent directory name is prefixed:

- `counter/CounterDomain.dfy` → `CounterDomain.dfy`
- `kanban/KanbanDomain.dfy` → `KanbanDomain.dfy`
- If both had `Domain.dfy` → `counter_Domain.dfy`, `kanban_Domain.dfy`
