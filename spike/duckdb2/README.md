# DuckDB 2.x from Node: primitive check (task 1074)

Not part of Sluice. Findings: `review/duckdb2-node-check.md`.

    npm ci
    node check.mjs      # PASS or FAIL table, results-<platform>.json, exit 1 on failure

Linux on Docker:

    docker run --rm --platform linux/arm64 -v "$PWD":/src:ro node:24-slim bash -c \
      'mkdir /w; cp /src/package*.json /src/check.mjs /src/statements.json /w; cd /w; npm ci; node check.mjs'

AST comparison against Python DuckDB 1.5.5:

    uv run --no-project --python 3.14 --with duckdb==1.5.5 python ast_dump_python.py ast-python-1.5.5.json
    python3 compare_ast.py ast-python-1.5.5.json ast-node-darwin-arm64.json
