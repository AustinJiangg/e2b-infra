#!/usr/bin/env python3
"""Set env keys inside a nomad job HCL's env block, idempotently.

The keys this test kit owns live between two markers, so switching schemes
rewrites exactly that region and nothing else. Pre-existing lines for the
same keys are removed (the whole file is backed up by 03-switch.sh first),
because a duplicate key in one env block has no defined winner.

Usage: hcl_env.py <file> KEY=VALUE [KEY=VALUE ...]
       hcl_env.py <file> --clear
"""
import re
import sys

BEGIN = "# >>> checkpoint-test >>>"
END = "# <<< checkpoint-test <<<"


def env_block(lines):
    """Return (start, end) line indices of the first env { ... } block body."""
    for i, ln in enumerate(lines):
        if re.match(r"\s*env\s*\{", ln):
            depth = ln.count("{") - ln.count("}")
            for j in range(i + 1, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                if depth <= 0:
                    return i + 1, j
            break
    raise SystemExit("no env { } block found in " + sys.argv[1])


def main():
    path = sys.argv[1]
    pairs = sys.argv[2:]
    clear = pairs == ["--clear"]
    if clear:
        pairs = []

    lines = open(path).read().splitlines(keepends=True)
    start, end = env_block(lines)

    body = lines[start:end]
    keys = [p.split("=", 1)[0] for p in pairs]

    # Drop any previous marked region, and any loose line setting a key we own.
    out, in_region = [], False
    for ln in body:
        if BEGIN in ln:
            in_region = True
            continue
        if END in ln:
            in_region = False
            continue
        if in_region:
            continue
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", ln)
        if m and m.group(1) in keys:
            continue
        out.append(ln)

    if pairs:
        indent = "        "
        for ln in out:
            m = re.match(r"(\s+)\S", ln)
            if m:
                indent = m.group(1)
                break
        block = [indent + BEGIN + "\n"]
        for p in pairs:
            k, v = p.split("=", 1)
            block.append('%s%s = "%s"\n' % (indent, k, v))
        block.append(indent + END + "\n")
        out = out + block

    open(path, "w").write("".join(lines[:start] + out + lines[end:]))
    print("env block updated: " + (", ".join(pairs) if pairs else "cleared"))


if __name__ == "__main__":
    main()
