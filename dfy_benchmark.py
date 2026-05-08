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


def resolve_includes(file_path):
    """Recursively resolve and inline includes, returning standalone content.

    Each unique file is inlined at most once across the whole tree (Dafny's
    own `include` directive dedupes the same way). Without this, a file
    reached via two include paths produces duplicate module definitions and
    the inlined output won't parse.
    """
    visited = set()

    def _resolve(path):
        path = os.path.abspath(path)
        if path in visited:
            return None  # Already inlined elsewhere in the tree.
        visited.add(path)

        if not os.path.exists(path):
            return f"// [File not found: {path}]\n"

        with open(path, 'r') as f:
            content = f.read()

        base_dir = os.path.dirname(path)
        include_pattern = r'^(\s*)include\s+"([^"]+)"'

        def replace_include(match):
            indent = match.group(1)
            include_path = match.group(2)
            full_path = os.path.normpath(os.path.join(base_dir, include_path))
            if not os.path.exists(full_path):
                return f"{indent}// [Include not found: {include_path}]"
            inlined = _resolve(full_path)
            if inlined is None:
                return f"{indent}// === {include_path} (already inlined elsewhere) ==="
            return f"{indent}// === Inlined from {include_path} ===\n{inlined}\n{indent}// === End {include_path} ==="

        return re.sub(include_pattern, replace_include, content, flags=re.MULTILINE)

    result = _resolve(file_path)
    return result if result is not None else ""


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

    Lemmas are deduped by source position. Dafny's AST reports inherited
    lemmas under both the base module and any refining module ("module B
    refines A"), with identical offsets. Without dedup we'd mutate the same
    span twice, corrupting trailing content on the second pass.
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
        lemmas = result.get('lemmas', [])

        # Correct the body span. Dafny's AST can mis-report it for bodies
        # with no statements (only comments).
        for l in lemmas:
            if not l['hasBody']:
                continue
            real_start, real_end = correct_body_span(content, l['bodyStartPos'])
            if real_start < real_end:
                l['bodyStartPos'] = real_start
                l['bodyEndPos'] = real_end
            else:
                # Could not locate body; treat as no body so we skip it.
                l['hasBody'] = False
                l['bodyStartPos'] = -1
                l['bodyEndPos'] = -1

        # Dedupe by source position. Refinement causes Dafny to report the
        # same lemma under multiple module names with identical offsets.
        seen = {}
        for l in lemmas:
            key = (l['startPos'], l['endPos'])
            if key not in seen or (l['hasBody'] and not seen[key]['hasBody']):
                seen[key] = l
        return list(seen.values())
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _balance_braces_to_close(content, brace_open):
    """Starting at content[brace_open] == '{', return the index AFTER the
    matching '}'. Ignores braces inside strings, line comments, and block
    comments. Returns len(content) if unbalanced (defensive)."""
    depth = 0
    i = brace_open
    in_str = False
    in_line_comment = False
    in_block_comment = False
    escape_next = False
    while i < len(content):
        c = content[i]
        if escape_next:
            escape_next = False
            i += 1
            continue
        if in_line_comment:
            if c == '\n':
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if content[i:i + 2] == '*/':
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if not in_str:
            if content[i:i + 2] == '//':
                in_line_comment = True
                i += 2
                continue
            if content[i:i + 2] == '/*':
                in_block_comment = True
                i += 2
                continue
        if c == '"':
            in_str = not in_str
            i += 1
            continue
        if c == '\\' and in_str:
            escape_next = True
            i += 1
            continue
        if in_str:
            i += 1
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(content)


def correct_body_span(content, hint_start):
    """Dafny's BlockStmt.StartToken can point a few chars INTO the body
    (skipping the `{`) when the body has only comments and no statements.
    EndToken can also overshoot to an enclosing module's `}`. We use
    hint_start only as a locator: search backward (within a small window
    and skipping whitespace/comments) for the actual `{`, then balance
    forward to find the matching `}`.

    Returns (real_start, real_end) where real_end is the index AFTER `}`.
    Falls back to the hint pair if we can't locate a `{` nearby.
    """
    # Dafny can also report a hint_start past EOF for some empty bodies. Clamp.
    clamped = min(hint_start, len(content) - 1) if len(content) > 0 else 0
    if 0 <= clamped < len(content) and content[clamped] == '{':
        return clamped, _balance_braces_to_close(content, clamped)

    i = clamped - 1
    floor = max(0, clamped - 200)
    while i >= floor:
        c = content[i]
        if c == '{':
            return i, _balance_braces_to_close(content, i)
        if c.isspace():
            i -= 1
            continue
        # Skip backward through a // line comment by stepping to its start.
        line_nl = content.rfind('\n', 0, i)
        line_text = content[line_nl + 1:i + 1] if line_nl >= 0 else content[:i + 1]
        comment_idx = line_text.find('//')
        if comment_idx >= 0 and (line_nl + 1 + comment_idx) <= i:
            i = line_nl
            continue
        i -= 1
    return hint_start, hint_start  # give up — caller treats as no body


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
