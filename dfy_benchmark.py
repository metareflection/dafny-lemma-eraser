#!/usr/bin/env python3
"""
Dafny Benchmark Generator

Creates benchmark files from Dafny source files by:
- Kind 1 (bodies_erased): Erasing all lemma bodies
- Kind 2 (helpers_removed): Also removing helper lemmas (lemmas called by other lemmas)

Hybrid approach:
- Uses LemmaExtractor (C#) on original files for accurate lemma detection
- Uses Python for include inlining and position finding in inlined content

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
import subprocess
from collections import defaultdict

# Path to the LemmaExtractor
LEMMA_EXTRACTOR = os.path.join(os.path.dirname(__file__), "bin/Debug/net8.0/LemmaExtractor.dll")


def resolve_includes(file_path, visited=None):
    """Recursively resolve and inline includes, returning the full content."""
    if visited is None:
        visited = set()

    file_path = os.path.abspath(file_path)
    if file_path in visited:
        return ""  # Avoid circular includes
    visited.add(file_path)

    if not os.path.exists(file_path):
        return f"// [File not found: {file_path}]\n"

    with open(file_path, 'r') as f:
        content = f.read()

    base_dir = os.path.dirname(file_path)

    # Find all include statements
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
    """Run LemmaExtractor on original file and return parsed JSON result."""
    result = subprocess.run(
        ["dotnet", LEMMA_EXTRACTOR, file_path],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        # Check if it's just parse errors but we still got output
        if result.stdout.strip():
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                pass
        print(f"    LemmaExtractor error: {result.stderr}", file=sys.stderr)
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"    JSON parse error: {e}", file=sys.stderr)
        return None


def find_brace_end(content, start):
    """Find the matching closing brace, handling nested braces, strings, and comments."""
    depth = 0
    i = start
    in_string = False
    in_line_comment = False
    in_block_comment = False
    escape_next = False

    while i < len(content):
        c = content[i]

        if escape_next:
            escape_next = False
            i += 1
            continue

        if c == '\\' and in_string:
            escape_next = True
            i += 1
            continue

        if in_line_comment:
            if c == '\n':
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if content[i:i+2] == '*/':
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if not in_string:
            if content[i:i+2] == '//':
                in_line_comment = True
                i += 2
                continue
            if content[i:i+2] == '/*':
                in_block_comment = True
                i += 2
                continue

        if c == '"':
            in_string = not in_string
            i += 1
            continue

        if in_string:
            i += 1
            continue

        # Track braces (Dafny uses ' in identifiers like s', not for char literals)
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def find_lemma_in_content(content, lemma_name, module_name, start_from=0):
    """
    Find a lemma by name in the content, returning (decl_start, body_start, body_end).
    Returns None if not found or if lemma has no body.
    """
    # Pattern to find lemma declaration
    # Handles: lemma, twostate lemma, ghost lemma
    pattern = rf'\b((?:twostate\s+)?(?:ghost\s+)?lemma)\s+{re.escape(lemma_name)}\s*(?:<[^>]*>)?\s*\('

    for match in re.finditer(pattern, content[start_from:]):
        decl_start = start_from + match.start()

        # Find the closing paren of parameters
        paren_start = start_from + match.end() - 1
        paren_depth = 1
        i = paren_start + 1
        while i < len(content) and paren_depth > 0:
            if content[i] == '(':
                paren_depth += 1
            elif content[i] == ')':
                paren_depth -= 1
            i += 1

        if paren_depth != 0:
            continue

        # Now find body start - scan for { while skipping signature keywords
        body_start = -1
        j = i
        while j < len(content):
            c = content[j]

            if c in ' \t\n\r':
                j += 1
                continue

            if content[j:j+2] == '//':
                newline = content.find('\n', j)
                j = newline + 1 if newline != -1 else len(content)
                continue

            if content[j:j+2] == '/*':
                end = content.find('*/', j)
                j = end + 2 if end != -1 else len(content)
                continue

            if c == '{':
                # Check if attribute
                if j + 1 < len(content) and content[j+1] == ':':
                    brace_end = find_brace_end(content, j)
                    j = brace_end + 1 if brace_end != -1 else len(content)
                    continue
                body_start = j
                break

            if c == ';':
                # No body
                break

            # Check for new declaration keywords
            if c.isalpha():
                word_end = j
                while word_end < len(content) and (content[word_end].isalnum() or content[word_end] == '_'):
                    word_end += 1
                word = content[j:word_end]

                new_decl = ['lemma', 'function', 'method', 'predicate', 'datatype',
                           'module', 'import', 'class', 'trait', 'const', 'type']
                if word in new_decl:
                    break

                j = word_end
                continue

            j += 1

        if body_start == -1:
            return (decl_start, -1, -1)  # No body

        body_end = find_brace_end(content, body_start)
        if body_end == -1:
            continue

        return (decl_start, body_start, body_end)

    return None


def find_all_lemmas_in_content(content, lemma_infos):
    """
    Find all lemmas from lemma_infos in the content.
    Returns list of dicts with name, hasBody, declStart, bodyStart, bodyEnd.
    """
    results = []
    used_positions = set()

    for info in lemma_infos:
        name = info['name']
        module = info['moduleName']
        has_body_from_ast = info['hasBody']

        # Find all occurrences of this lemma name
        start_from = 0
        while True:
            found = find_lemma_in_content(content, name, module, start_from)
            if found is None:
                break

            decl_start, body_start, body_end = found

            # Skip if we've already used this position
            if decl_start in used_positions:
                start_from = decl_start + 1
                continue

            has_body = body_start >= 0

            results.append({
                'name': name,
                'moduleName': module,
                'hasBody': has_body,
                'declStart': decl_start,
                'bodyStart': body_start,
                'bodyEnd': body_end
            })
            used_positions.add(decl_start)
            start_from = (body_end if body_end >= 0 else decl_start) + 1

    return results


def find_lemma_calls(content, lemmas):
    """Find which lemmas call which other lemmas."""
    lemma_names = {l['name'] for l in lemmas if l['hasBody']}
    calls = defaultdict(set)

    for lemma in lemmas:
        if not lemma['hasBody']:
            continue

        body_start = lemma['bodyStart']
        body_end = lemma['bodyEnd']
        if body_start < 0 or body_end < 0:
            continue

        body = content[body_start:body_end]
        caller = lemma['name']

        for other_name in lemma_names:
            if other_name == caller:
                continue
            call_pattern = rf'\b{re.escape(other_name)}\s*[\(<;]'
            if re.search(call_pattern, body):
                calls[caller].add(other_name)

    return calls


def find_helper_lemmas(content, lemmas):
    """Find lemmas that are called by other lemmas (helpers)."""
    calls = find_lemma_calls(content, lemmas)
    helpers = set()
    for callees in calls.values():
        helpers.update(callees)
    return helpers


def erase_lemma_bodies(content, lemmas):
    """Replace all lemma bodies with empty bodies."""
    with_bodies = [l for l in lemmas if l['hasBody'] and l['bodyStart'] >= 0]
    with_bodies.sort(key=lambda l: l['bodyStart'], reverse=True)

    for lemma in with_bodies:
        body_start = lemma['bodyStart']
        body_end = lemma['bodyEnd']

        # Find indentation
        line_start = content.rfind('\n', 0, body_start)
        if line_start == -1:
            indent = ''
        else:
            indent_match = re.match(r'^(\s*)', content[line_start+1:body_start])
            indent = indent_match.group(1) if indent_match else ''

        content = content[:body_start] + '{\n' + indent + '}' + content[body_end+1:]

    return content


def remove_helper_lemmas(content, lemmas):
    """Remove lemmas that are called by other lemmas (helpers)."""
    helpers = find_helper_lemmas(content, lemmas)

    if not helpers:
        return content

    to_remove = [l for l in lemmas if l['name'] in helpers and l['hasBody']]
    to_remove.sort(key=lambda l: l['declStart'], reverse=True)

    for lemma in to_remove:
        start = lemma['declStart']
        end = lemma['bodyEnd'] + 1

        # Consume trailing whitespace
        while end < len(content) and content[end] in ' \t\n\r':
            end += 1

        content = content[:start] + content[end:]

    return content


def process_file(file_path, output_dir_kind1, output_dir_kind2, processed_files):
    """Process a single Dafny file and create both benchmark versions."""
    # Run LemmaExtractor on original file to get lemma info
    result = run_lemma_extractor(file_path)

    if result is None:
        print(f"    ERROR: Could not extract lemmas from {file_path}")
        return False

    lemma_infos = result.get('lemmas', [])

    # Resolve includes to create standalone content
    content = resolve_includes(file_path)

    # Find all lemmas in the inlined content
    lemmas = find_all_lemmas_in_content(content, lemma_infos)

    # Generate output filename
    original_name = os.path.basename(file_path)
    dir_name = os.path.basename(os.path.dirname(file_path))

    output_name = original_name
    if output_name in processed_files:
        output_name = f"{dir_name}_{original_name}"

    processed_files.add(output_name)

    kind1_path = os.path.join(output_dir_kind1, output_name)
    kind2_path = os.path.join(output_dir_kind2, output_name)

    # Count stats
    total_lemmas = len([l for l in lemmas if l['hasBody']])
    helpers = find_helper_lemmas(content, lemmas)

    # Kind 1: Just erase lemma bodies
    kind1_content = erase_lemma_bodies(content, lemmas)

    # Kind 2: Remove helpers, then erase remaining lemma bodies
    kind2_content = remove_helper_lemmas(content, lemmas)
    # Re-find lemmas in modified content
    remaining_infos = [i for i in lemma_infos if i['name'] not in helpers]
    remaining_lemmas = find_all_lemmas_in_content(kind2_content, remaining_infos)
    kind2_content = erase_lemma_bodies(kind2_content, remaining_lemmas)

    # Write outputs
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

    # Check LemmaExtractor exists
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
