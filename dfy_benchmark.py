#!/usr/bin/env python3
"""
Dafny Benchmark Generator

Creates benchmark files from Dafny source files by:
- Kind 1 (bodies_erased): Erasing all lemma bodies
- Kind 2 (helpers_removed): Also removing helper lemmas (lemmas called by other lemmas)

Pipeline:
1. Resolve includes textually -> standalone content
2. Write content to a temp .dfy file
3. Run the C# LemmaExtractor on the temp file. The extractor uses Dafny's
   official AST and returns exact byte offsets for each lemma's declaration
   and body. We trust those positions verbatim.
4. Erase / remove from right to left so positions stay valid.

Usage:
    python dfy_benchmark.py <pattern> <output_dir_kind1> <output_dir_kind2>

Example:
    python dfy_benchmark.py "../dafny-replay/*/*.dfy" benchmarks/bodies_erased benchmarks/helpers_removed
"""

import sys
import os
import re
import glob
import json
import tempfile
import subprocess
from collections import defaultdict

LEMMA_EXTRACTOR = os.path.join(
    os.path.dirname(__file__), "bin/Debug/net8.0/LemmaExtractor.dll"
)


def resolve_includes(file_path, visited=None):
    """Recursively resolve and inline includes, returning the full content."""
    if visited is None:
        visited = set()

    file_path = os.path.abspath(file_path)
    if file_path in visited:
        return ""
    visited.add(file_path)

    if not os.path.exists(file_path):
        return f"// [File not found: {file_path}]\n"

    with open(file_path, 'r') as f:
        content = f.read()

    base_dir = os.path.dirname(file_path)
    include_pattern = r'^(\s*)include\s+"([^"]+)"'

    def replace_include(match):
        indent = match.group(1)
        include_path = match.group(2)
        full_path = os.path.normpath(os.path.join(base_dir, include_path))
        if os.path.exists(full_path):
            included_content = resolve_includes(full_path, visited.copy())
            return f"{indent}// === Inlined from {include_path} ===\n{included_content}\n{indent}// === End {include_path} ==="
        else:
            return f"{indent}// [Include not found: {include_path}]"

    content = re.sub(include_pattern, replace_include, content, flags=re.MULTILINE)
    return content


def run_lemma_extractor(file_path):
    """Run LemmaExtractor on a file and return parsed JSON result, or None."""
    result = subprocess.run(
        ["dotnet", LEMMA_EXTRACTOR, file_path],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0 and not result.stdout.strip():
        print(f"    LemmaExtractor error: {result.stderr}", file=sys.stderr)
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"    JSON parse error: {e}", file=sys.stderr)
        return None


def extract_lemmas_from_inlined(content):
    """Write content to a temp .dfy file and run LemmaExtractor on it.

    Returns the list of lemma dicts (with AST-correct bodyStartPos/bodyEndPos
    relative to `content`), or None on failure.
    """
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.dfy', delete=False
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = run_lemma_extractor(tmp_path)
        if result is None:
            return None
        return result.get('lemmas', [])
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def find_helper_lemmas(content, lemmas):
    """Return the set of lemma names that are called by other lemmas."""
    lemma_names = {l['name'] for l in lemmas if l['hasBody']}
    helpers = set()
    for lemma in lemmas:
        if not lemma['hasBody']:
            continue
        body = content[lemma['bodyStartPos']:lemma['bodyEndPos']]
        caller = lemma['name']
        for other in lemma_names:
            if other == caller:
                continue
            if re.search(rf'\b{re.escape(other)}\s*[\(<;]', body):
                helpers.add(other)
    return helpers


def _empty_body_at(content, body_start, body_end):
    """Replace the body span [body_start, body_end) with `{\\n  }` indented
    to match the line of the opening brace."""
    line_start = content.rfind('\n', 0, body_start)
    indent = ''
    if line_start != -1:
        indent_match = re.match(r'^(\s*)', content[line_start + 1:body_start])
        if indent_match:
            indent = indent_match.group(1)
    return content[:body_start] + '{\n' + indent + '}' + content[body_end:]


def _remove_decl_at(content, start, body_end):
    """Cut the declaration span [start, body_end) plus trailing whitespace."""
    end = body_end
    while end < len(content) and content[end] in ' \t\n\r':
        end += 1
    return content[:start] + content[end:]


def erase_lemma_bodies(content, lemmas):
    """Replace all lemma bodies with empty bodies. Right-to-left."""
    targets = [l for l in lemmas if l['hasBody'] and l['bodyStartPos'] >= 0]
    targets.sort(key=lambda l: l['bodyStartPos'], reverse=True)
    for lemma in targets:
        content = _empty_body_at(
            content, lemma['bodyStartPos'], lemma['bodyEndPos']
        )
    return content


def remove_helpers_and_erase(content, lemmas, helpers):
    """One pass: helpers are cut entirely; remaining lemma bodies are emptied.
    Operations are sorted right-to-left so earlier positions stay valid."""
    ops = []
    for lemma in lemmas:
        if not lemma['hasBody']:
            continue
        if lemma['name'] in helpers:
            ops.append(('remove', lemma['startPos'], lemma['bodyEndPos'], lemma))
        else:
            ops.append(('erase', lemma['bodyStartPos'], lemma['bodyEndPos'], lemma))

    ops.sort(key=lambda o: o[1], reverse=True)

    for op, start, end, _ in ops:
        if op == 'remove':
            content = _remove_decl_at(content, start, end)
        else:
            content = _empty_body_at(content, start, end)
    return content


def process_file(file_path, output_dir_kind1, output_dir_kind2, processed_files):
    """Process a single Dafny file and create both benchmark versions."""
    content = resolve_includes(file_path)
    lemmas = extract_lemmas_from_inlined(content)

    if lemmas is None:
        print(f"    ERROR: Could not extract lemmas from {file_path}")
        return False

    original_name = os.path.basename(file_path)
    dir_name = os.path.basename(os.path.dirname(file_path))

    output_name = original_name
    if output_name in processed_files:
        output_name = f"{dir_name}_{original_name}"
    processed_files.add(output_name)

    kind1_path = os.path.join(output_dir_kind1, output_name)
    kind2_path = os.path.join(output_dir_kind2, output_name)

    helpers = find_helper_lemmas(content, lemmas)
    total_lemmas = sum(1 for l in lemmas if l['hasBody'])

    kind1_content = erase_lemma_bodies(content, lemmas)
    kind2_content = remove_helpers_and_erase(content, lemmas, helpers)

    os.makedirs(output_dir_kind1, exist_ok=True)
    os.makedirs(output_dir_kind2, exist_ok=True)

    with open(kind1_path, 'w') as f:
        f.write(kind1_content)
    with open(kind2_path, 'w') as f:
        f.write(kind2_content)

    print(f"  {file_path}")
    print(f"    -> {output_name} ({total_lemmas} lemmas, {len(helpers)} helpers)")
    return True


def main():
    if len(sys.argv) != 4:
        print("Dafny Benchmark Generator")
        print()
        print("Usage: python dfy_benchmark.py <pattern> <output_dir_kind1> <output_dir_kind2>")
        print()
        print("Arguments:")
        print("  pattern          Glob pattern for .dfy files")
        print("  output_dir_kind1 Directory for Kind 1 benchmarks (lemma bodies erased)")
        print("  output_dir_kind2 Directory for Kind 2 benchmarks (helpers removed + bodies erased)")
        print()
        print("Example:")
        print("  python dfy_benchmark.py '../dafny-replay/*/*.dfy' bench/bodies_erased bench/helpers_removed")
        sys.exit(1)

    if not os.path.exists(LEMMA_EXTRACTOR):
        print(f"Error: LemmaExtractor not found at {LEMMA_EXTRACTOR}")
        print("Please run: dotnet build LemmaExtractor.csproj")
        sys.exit(1)

    pattern = sys.argv[1]
    output_dir_kind1 = sys.argv[2]
    output_dir_kind2 = sys.argv[3]

    files = sorted(glob.glob(pattern))
    if not files:
        print(f"No files found matching pattern: {pattern}")
        sys.exit(1)

    print(f"Found {len(files)} files matching '{pattern}'")
    print(f"Kind 1 output: {output_dir_kind1}")
    print(f"Kind 2 output: {output_dir_kind2}")
    print()

    processed_files = set()
    success_count = 0

    for file_path in files:
        try:
            if process_file(file_path, output_dir_kind1, output_dir_kind2, processed_files):
                success_count += 1
        except Exception as e:
            print(f"  ERROR: {file_path}: {e}")
            import traceback
            traceback.print_exc()

    print()
    print(f"Processed {success_count}/{len(files)} files successfully")


if __name__ == "__main__":
    main()
