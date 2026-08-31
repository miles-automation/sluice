# M0 verification spike

Status: completed
Date: 2026-08-28
Implements: plan/001-scratch-db.md §1 M0

All observations below came from executed code. No production Sluice code
existed during the spike.

## Environment

    $ pwd
    /Users/richmiles/Source/platform
    $ uv --version
    uv 0.10.11 (006b56b12 2026-03-16)
    $ uname -a
    Darwin Richs-MacBook-Pro.local 25.6.0 Darwin Kernel Version 25.6.0: Fri Jul 31 19:17:26 PDT 2026; root:xnu-12377.161.14~5/RELEASE_ARM64_T6041 arm64
    $ sw_vers
    ProductName:            macOS
    ProductVersion:         26.6.2
    BuildVersion:           25G83

The temporary experiment directory was /tmp/sluice-m0.X210IN. Script SHA-256
digests are recorded with the relevant steps.

## 1. CPython 3.14 binary-wheel installation

Result: **pass**. A fresh CPython 3.14.2 environment installed all packages with
binary distributions only. DuckDB resolved to 1.5.5 with a native cp314 arm64
wheel. MCP resolved to 2.1.1 with a py3-none-any wheel.

Command:

    uv venv --python 3.14 /tmp/sluice-m0.X210IN/.venv &&
      uv pip install +        --python /tmp/sluice-m0.X210IN/.venv/bin/python +        --only-binary=:all: duckdb mcp &&
      /tmp/sluice-m0.X210IN/.venv/bin/python -VV &&
      /tmp/sluice-m0.X210IN/.venv/bin/python -c +        'import duckdb, importlib.metadata as m; print("duckdb", duckdb.__version__); print("mcp", m.version("mcp"))'

Raw output:

    Using CPython 3.14.2 interpreter at: /opt/homebrew/opt/python@3.14/bin/python3.14
    Creating virtual environment at: /tmp/sluice-m0.X210IN/.venv
    Activate with: source /tmp/sluice-m0.X210IN/.venv/bin/activate
    Using Python 3.14.2 environment at: /private/tmp/sluice-m0.X210IN/.venv
    Resolved 29 packages in 353ms
    Downloading pydantic-core (1.8MiB)
    Downloading duckdb (14.8MiB)
    Downloading cryptography (3.8MiB)
     Downloaded pydantic-core
     Downloaded cryptography
     Downloaded duckdb
    Prepared 15 packages in 429ms
    Installed 29 packages in 25ms
     + annotated-types==0.8.0
     + anyio==4.14.2
     + attrs==26.1.0
     + cffi==2.1.1
     + click==8.5.0
     + cryptography==50.0.1
     + duckdb==1.5.5
     + h11==0.16.0
     + httpcore2==2.12.0
     + httpx2==2.12.0
     + idna==3.19
     + jsonschema==4.26.0
     + jsonschema-specifications==2025.9.1
     + mcp==2.1.1
     + mcp-types==2.1.1
     + opentelemetry-api==1.44.0
     + pycparser==3.0
     + pydantic==2.13.5
     + pydantic-core==2.46.5
     + pyjwt==2.13.0
     + python-multipart==0.0.32
     + referencing==0.37.0
     + rpds-py==2026.6.3
     + sse-starlette==3.4.8
     + starlette==1.6.0
     + truststore==0.10.4
     + typing-extensions==4.16.0
     + typing-inspection==0.4.4
     + uvicorn==0.52.4
    Python 3.14.2 (main, Dec  5 2025, 16:49:16) [Clang 17.0.0 (clang-1700.4.4.1)]
    duckdb 1.5.5
    mcp 2.1.1

Wheel metadata command:

    /tmp/sluice-m0.X210IN/.venv/bin/python - <<'PY'
    from importlib.metadata import distribution
    for name in ('duckdb', 'mcp'):
        dist = distribution(name)
        print(f'{name} {dist.version}')
        print(dist.read_text('WHEEL').strip())
    PY

Raw output:

    duckdb 1.5.5
    Wheel-Version: 1.0
    Generator: scikit-build-core 1.0.3
    Root-Is-Purelib: false
    Tag: cp314-cp314-macosx_11_0_arm64
    Generator: delocate 0.13.0
    mcp 2.1.1
    Wheel-Version: 1.0
    Generator: hatchling 1.29.0
    Root-Is-Purelib: true
    Tag: py3-none-any

## 2. DuckDB statement extraction

Result: **pass**. extract_statements exists. It returned StatementType.SELECT
for SELECT and WITH ... SELECT, StatementType.INSERT for INSERT, and two parsed
statements for a two-statement input.

## 3. DuckDB engine lockdown

Result: **pass for the two M0 assertions**. After external access was disabled
and configuration locked, read_csv could not read /etc/hosts and the setting
could not be re-enabled.

Steps 2 and 3 used this script
(SHA-256 fb19572d41f4bd1945a6064655c70dc75b8edd168134fca9289c9e6071aef55a):

    import duckdb


    def report_extract(con: duckdb.DuckDBPyConnection, sql: str) -> None:
        statements = con.extract_statements(sql)
        print(f"sql={sql!r} count={len(statements)}")
        for index, statement in enumerate(statements, start=1):
            print(
                f"  statement[{index}] type={statement.type!r} "
                f"name={getattr(statement.type, 'name', None)!r} "
                f"query={statement.query!r}"
            )


    print("STEP 2: extract_statements")
    connection = duckdb.connect(":memory:")
    for candidate in (
        "SELECT 1",
        "INSERT INTO t VALUES (1)",
        "WITH x AS (SELECT 1 AS n) SELECT n FROM x",
        "SELECT 1; SELECT 2",
    ):
        report_extract(connection, candidate)
    connection.close()

    print("\nSTEP 3: engine lockdown")
    connection = duckdb.connect(":memory:")
    for setting in (
        "SET enable_external_access = false",
        "SET lock_configuration = true",
    ):
        connection.execute(setting)
        print(f"succeeded: {setting}")

    for candidate in (
        "SELECT * FROM read_csv('/etc/hosts')",
        "SET enable_external_access = true",
    ):
        try:
            connection.execute(candidate).fetchall()
        except Exception as exc:
            print(f"failed as required: {candidate}")
            print(f"  {type(exc).__name__}: {exc}")
        else:
            print(f"UNEXPECTED SUCCESS: {candidate}")
    connection.close()

Command:

    /tmp/sluice-m0.X210IN/.venv/bin/python /tmp/sluice-m0.X210IN/step2_3.py

Raw output:

    STEP 2: extract_statements
    sql='SELECT 1' count=1
      statement[1] type=<StatementType.SELECT: 1> name='SELECT' query='SELECT 1'
    sql='INSERT INTO t VALUES (1)' count=1
      statement[1] type=<StatementType.INSERT: 2> name='INSERT' query='INSERT INTO t VALUES (1)'
    sql='WITH x AS (SELECT 1 AS n) SELECT n FROM x' count=1
      statement[1] type=<StatementType.SELECT: 1> name='SELECT' query='WITH x AS (SELECT 1 AS n) SELECT n FROM x'
    sql='SELECT 1; SELECT 2' count=2
      statement[1] type=<StatementType.SELECT: 1> name='SELECT' query='SELECT 1'
      statement[2] type=<StatementType.SELECT: 1> name='SELECT' query=' SELECT 2'

    STEP 3: engine lockdown
    succeeded: SET enable_external_access = false
    succeeded: SET lock_configuration = true
    failed as required: SELECT * FROM read_csv('/etc/hosts')
      PermissionException: Permission Error: Cannot access file "/etc/hosts" - file system operations are disabled by configuration

    LINE 1: SELECT * FROM read_csv('/etc/hosts')
                          ^
    failed as required: SET enable_external_access = true
      InvalidInputException: Invalid Input Error: Cannot change configuration option "enable_external_access" - the configuration has been locked

## 4. Interrupt behavior and isolation

Result: **mixed, with an implementation constraint**.

- Calling interrupt() on a parent connection did not interrupt work executing on
  a cursor returned by parent.cursor(). The query completed normally in 5.249
  seconds.
- Calling interrupt() on the exact DuckDBPyConnection object executing the query
  stopped it in about 0.205 seconds, with delivery less than one millisecond
  after the interrupt call.
- Two cursor objects derived from one parent were isolated when interrupt() was
  called on the query cursor itself. The concurrent transactional write
  committed, its uncommitted value was retained through commit, and all three
  connection objects remained usable.
- Three separately opened connections to the same named in-memory database
  behaved the same way: the query connection was interrupted, the writer
  committed, and every connection remained usable.

The observed scope is the DuckDBPyConnection object, not the parent/child family
created by cursor(). Spec §6.2 is ambiguous where it says to execute on a
dedicated cursor and call interrupt() on "the connection": interrupting the
parent is ineffective. The safe default already written in spec §9 works if the
worker executes directly on the dedicated query connection object and the
watchdog interrupts that same object.

Script
(SHA-256 0571952c3273aba98029d4ff1c4580bb149aadd567b6613ac3d9a55e37471a84):

    import threading
    import time
    from collections.abc import Callable

    import duckdb


    LONG_QUERY = "SELECT sum(sin(i)) FROM range(1000000000) t(i)"
    WRITE_QUERY = "UPDATE writes SET checksum = (SELECT sum(sin(i)) FROM range(300000000) t(i))"


    def run_interrupt_case(
        label: str,
        executor: duckdb.DuckDBPyConnection,
        interrupter: duckdb.DuckDBPyConnection,
    ) -> None:
        started = threading.Event()
        outcome: dict[str, object] = {}

        def run_query() -> None:
            started.set()
            began = time.monotonic()
            try:
                outcome["result"] = executor.execute(LONG_QUERY).fetchone()
            except Exception as exc:
                outcome["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                outcome["elapsed_seconds"] = round(time.monotonic() - began, 6)

        worker = threading.Thread(target=run_query, name=f"query-{label}")
        worker.start()
        started.wait(timeout=2)
        time.sleep(0.2)
        interrupt_at = time.monotonic()
        interrupter.interrupt()
        worker.join(timeout=15)
        print(f"\n{label}")
        print(f"  worker_alive={worker.is_alive()}")
        print(f"  seconds_after_interrupt={time.monotonic() - interrupt_at:.6f}")
        print(f"  outcome={outcome}")
        print(f"  executor_usable={executor.execute('SELECT 41').fetchone()}")
        print(f"  interrupter_usable={interrupter.execute('SELECT 42').fetchone()}")


    def run_isolation_case(
        label: str,
        make_connections: Callable[[], tuple[
            duckdb.DuckDBPyConnection,
            duckdb.DuckDBPyConnection,
            duckdb.DuckDBPyConnection,
        ]],
    ) -> None:
        observer, query_connection, writer_connection = make_connections()
        observer.execute("CREATE TABLE writes(id INTEGER, checksum DOUBLE)")
        observer.execute("INSERT INTO writes VALUES (1, 0.0)")

        barrier = threading.Barrier(3)
        query_outcome: dict[str, object] = {}
        write_outcome: dict[str, object] = {}

        def run_query() -> None:
            barrier.wait()
            began = time.monotonic()
            try:
                query_outcome["result"] = query_connection.execute(LONG_QUERY).fetchone()
            except Exception as exc:
                query_outcome["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                query_outcome["elapsed_seconds"] = round(
                    time.monotonic() - began, 6
                )

        def run_write() -> None:
            writer_connection.execute("BEGIN TRANSACTION")
            barrier.wait()
            began = time.monotonic()
            try:
                writer_connection.execute(WRITE_QUERY)
                write_outcome["value_before_commit"] = writer_connection.execute(
                    "SELECT checksum FROM writes WHERE id = 1"
                ).fetchone()
                writer_connection.execute("COMMIT")
                write_outcome["committed"] = True
            except Exception as exc:
                write_outcome["error"] = f"{type(exc).__name__}: {exc}"
                try:
                    writer_connection.execute("ROLLBACK")
                except Exception as rollback_exc:
                    write_outcome["rollback_error"] = (
                        f"{type(rollback_exc).__name__}: {rollback_exc}"
                    )
            finally:
                write_outcome["elapsed_seconds"] = round(
                    time.monotonic() - began, 6
                )

        query_worker = threading.Thread(target=run_query, name=f"query-{label}")
        write_worker = threading.Thread(target=run_write, name=f"write-{label}")
        query_worker.start()
        write_worker.start()
        barrier.wait()
        time.sleep(0.2)
        interrupt_at = time.monotonic()
        query_connection.interrupt()
        query_worker.join(timeout=15)
        write_worker.join(timeout=15)

        print(f"\n{label}")
        print(f"  query_worker_alive={query_worker.is_alive()}")
        print(f"  write_worker_alive={write_worker.is_alive()}")
        print(f"  seconds_until_both_done={time.monotonic() - interrupt_at:.6f}")
        print(f"  query_outcome={query_outcome}")
        print(f"  write_outcome={write_outcome}")
        print(
            "  committed_value_seen_by_observer="
            f"{observer.execute('SELECT checksum FROM writes WHERE id = 1').fetchone()}"
        )
        print(f"  query_connection_usable={query_connection.execute('SELECT 43').fetchone()}")
        print(f"  writer_connection_usable={writer_connection.execute('SELECT 44').fetchone()}")
        print(f"  observer_connection_usable={observer.execute('SELECT 45').fetchone()}")


    print("STEP 4: interrupt behavior")

    parent = duckdb.connect(":memory:parent_interrupt")
    child = parent.cursor()
    run_interrupt_case(
        "parent interrupt() while derived cursor executes",
        executor=child,
        interrupter=parent,
    )
    child.close()
    parent.close()

    direct = duckdb.connect(":memory:direct_interrupt")
    run_interrupt_case(
        "interrupt() on the same connection object that executes",
        executor=direct,
        interrupter=direct,
    )
    direct.close()


    def derived_connections() -> tuple[
        duckdb.DuckDBPyConnection,
        duckdb.DuckDBPyConnection,
        duckdb.DuckDBPyConnection,
    ]:
        base = duckdb.connect(":memory:derived_isolation")
        return base, base.cursor(), base.cursor()


    run_isolation_case("two cursors derived from one parent", derived_connections)


    def separate_connections() -> tuple[
        duckdb.DuckDBPyConnection,
        duckdb.DuckDBPyConnection,
        duckdb.DuckDBPyConnection,
    ]:
        database = ":memory:separate_isolation"
        return (
            duckdb.connect(database),
            duckdb.connect(database),
            duckdb.connect(database),
        )


    run_isolation_case("separate connections to one named in-memory database", separate_connections)

Command:

    /tmp/sluice-m0.X210IN/.venv/bin/python /tmp/sluice-m0.X210IN/step4.py

Raw output:

    STEP 4: interrupt behavior

    parent interrupt() while derived cursor executes
      worker_alive=False
      seconds_after_interrupt=5.048247
      outcome={'result': (-0.12454896270322086,), 'elapsed_seconds': 5.248558}
      executor_usable=(41,)
      interrupter_usable=(42,)

    interrupt() on the same connection object that executes
      worker_alive=False
      seconds_after_interrupt=0.000357
      outcome={'error': 'InterruptException: INTERRUPT Error: Interrupted!', 'elapsed_seconds': 0.204774}
      executor_usable=(41,)
      interrupter_usable=(42,)

    two cursors derived from one parent
      query_worker_alive=False
      write_worker_alive=False
      seconds_until_both_done=1.380256
      query_outcome={'error': 'InterruptException: INTERRUPT Error: Interrupted!', 'elapsed_seconds': 0.204032}
      write_outcome={'value_before_commit': (0.31293218183628124,), 'committed': True, 'elapsed_seconds': 1.584003}
      committed_value_seen_by_observer=(0.31293218183628124,)
      query_connection_usable=(43,)
      writer_connection_usable=(44,)
      observer_connection_usable=(45,)

    separate connections to one named in-memory database
      query_worker_alive=False
      write_worker_alive=False
      seconds_until_both_done=1.393966
      query_outcome={'error': 'InterruptException: INTERRUPT Error: Interrupted!', 'elapsed_seconds': 0.202882}
      write_outcome={'value_before_commit': (0.31293218183628124,), 'committed': True, 'elapsed_seconds': 1.596601}
      committed_value_seen_by_observer=(0.31293218183628124,)
      query_connection_usable=(43,)
      writer_connection_usable=(44,)
      observer_connection_usable=(45,)

## 5. MCP SDK dynamic tools and result construction

Result: **pass**, using the low-level mcp.server.Server API in MCP 2.1.1.

The v2 API registers async handlers through Server constructor arguments:

    Server(
        "sluice-m0",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )

The list callback has the shape:

    async def on_list_tools(
        context: ServerRequestContext,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult

The call callback has the shape:

    async def on_call_tool(
        context: ServerRequestContext,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult

Returning types.CallToolResult(content=[...], structuredContent={...},
isError=...) set both fields per call. A real in-memory client/server round trip
saw an initially empty tool list, then saw six tools added to runtime state
after the server and client were already running. The low-level API therefore
does not require import-time decorators.

In MCP 2.1.1, mcp.server.fastmcp does not exist; the former FastMCP surface was
renamed to mcp.server.mcpserver.MCPServer. This spike did not need that higher
level API.

## 6. MCP payload channels

Result: **pass**. After a real client/server SDK round trip:

- text-only JSON remained one TextContent block with structured_content=None;
- structured data and prose remained independently visible;
- both populated channels remained independently visible; and
- two valid JSON text blocks remained two ordered TextContent objects rather
  than being merged.

Steps 5 and 6 used this script
(SHA-256 559c1af1f9de3de1c28e67534b1957bd1fe18f3eadfdedff14a3816e1f07a0ca):

    import asyncio
    import inspect
    import json
    from typing import Any

    from mcp import types
    from mcp.client import Client
    from mcp.server import Server


    dynamic_tools: list[types.Tool] = []


    async def on_list_tools(
        _context: Any,
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=list(dynamic_tools))


    def channel_result(name: str) -> types.CallToolResult:
        structured = {"items": [{"id": 1}, {"id": 2}]}
        serialized = json.dumps(structured, separators=(",", ":"))
        if name == "text_only_json":
            return types.CallToolResult(
                content=[types.TextContent(text=serialized)],
            )
        if name == "structured_with_prose":
            return types.CallToolResult(
                content=[types.TextContent(text="Returned two records.")],
                structuredContent=structured,
            )
        if name == "both_channels":
            return types.CallToolResult(
                content=[types.TextContent(text=serialized)],
                structuredContent=structured,
            )
        if name == "two_text_blocks":
            return types.CallToolResult(
                content=[
                    types.TextContent(text='{"left":1}'),
                    types.TextContent(text='{"right":2}'),
                ],
            )
        raise ValueError(name)


    async def on_call_tool(
        _context: Any,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        if params.name in {
            "text_only_json",
            "structured_with_prose",
            "both_channels",
            "two_text_blocks",
        }:
            return channel_result(params.name)
        return types.CallToolResult(
            content=[types.TextContent(text=f"called {params.name}")],
            structuredContent={"called": params.name, "arguments": params.arguments},
            isError=params.name == "runtime_error",
        )


    server = Server(
        "sluice-m0",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


    def tool(name: str) -> types.Tool:
        return types.Tool(
            name=name,
            description=f"runtime tool {name}",
            inputSchema={"type": "object", "additionalProperties": True},
        )


    async def main() -> None:
        print("STEP 5: dynamic tools and per-call result fields")
        print(f"Server signature: {inspect.signature(Server)}")
        print(f"on_list_tools signature: {inspect.signature(on_list_tools)}")
        print(f"on_call_tool signature: {inspect.signature(on_call_tool)}")
        print(f"registered tools/list: {server.get_request_handler('tools/list')}")
        print(f"registered tools/call: {server.get_request_handler('tools/call')}")

        async with Client(server) as client:
            initial = await client.list_tools()
            print(f"initial tools: {[item.name for item in initial.tools]}")

            dynamic_tools.extend(
                [
                    tool("runtime_ok"),
                    tool("runtime_error"),
                    tool("text_only_json"),
                    tool("structured_with_prose"),
                    tool("both_channels"),
                    tool("two_text_blocks"),
                ]
            )
            discovered = await client.list_tools()
            print(f"after runtime discovery: {[item.name for item in discovered.tools]}")

            ok = await client.call_tool("runtime_ok", {"n": 7})
            error = await client.call_tool("runtime_error", {"n": 8})
            print(
                "runtime_ok result: "
                f"{ok.model_dump(by_alias=True, exclude_none=True)}"
            )
            print(
                "runtime_error result: "
                f"{error.model_dump(by_alias=True, exclude_none=True)}"
            )

            print("\nSTEP 6: payload channel shapes after SDK round trip")
            for name in (
                "text_only_json",
                "structured_with_prose",
                "both_channels",
                "two_text_blocks",
            ):
                result = await client.call_tool(name, {})
                print(f"{name}:")
                print(f"  content={result.content!r}")
                print(f"  structured_content={result.structured_content!r}")
                print(f"  is_error={result.is_error!r}")
                print(
                    "  wire_alias_dump="
                    f"{result.model_dump(by_alias=True, exclude_none=True)!r}"
                )


    asyncio.run(main())

Command:

    /tmp/sluice-m0.X210IN/.venv/bin/python /tmp/sluice-m0.X210IN/step5_6.py

Raw output:

    STEP 5: dynamic tools and per-call result fields
    Server signature: (name: 'str', *, version: 'str' = '', title: 'str | None' = None, description: 'str | None' = None, instructions: 'str | None' = None, website_url: 'str | None' = None, icons: 'list[types.Icon] | None' = None, cache_hints: 'Mapping[CacheableMethod, CacheHint] | None' = None, lifespan: 'Callable[[Server[LifespanResultT]], AbstractAsyncContextManager[LifespanResultT]]' = <function lifespan at 0x10c6e8f60>, on_list_tools: 'Callable[[ServerRequestContext[LifespanResultT], types.PaginatedRequestParams | None], Awaitable[types.ListToolsResult]] | None' = None, on_call_tool: 'Callable[[ServerRequestContext[LifespanResultT], types.CallToolRequestParams], Awaitable[types.CallToolResult | types.InputRequiredResult]] | None' = None, on_list_resources: 'Callable[[ServerRequestContext[LifespanResultT], types.PaginatedRequestParams | None], Awaitable[types.ListResourcesResult]] | None' = None, on_list_resource_templates: 'Callable[[ServerRequestContext[LifespanResultT], types.PaginatedRequestParams | None], Awaitable[types.ListResourceTemplatesResult]] | None' = None, on_read_resource: 'Callable[[ServerRequestContext[LifespanResultT], types.ReadResourceRequestParams], Awaitable[types.ReadResourceResult | types.InputRequiredResult]] | None' = None, on_subscribe_resource: 'Callable[[ServerRequestContext[LifespanResultT], types.SubscribeRequestParams], Awaitable[types.EmptyResult]] | None' = None, on_unsubscribe_resource: 'Callable[[ServerRequestContext[LifespanResultT], types.UnsubscribeRequestParams], Awaitable[types.EmptyResult]] | None' = None, on_subscriptions_listen: 'Callable[[ServerRequestContext[LifespanResultT], types.SubscriptionsListenRequestParams], Awaitable[types.SubscriptionsListenResult]] | None' = None, on_list_prompts: 'Callable[[ServerRequestContext[LifespanResultT], types.PaginatedRequestParams | None], Awaitable[types.ListPromptsResult]] | None' = None, on_get_prompt: 'Callable[[ServerRequestContext[LifespanResultT], types.GetPromptRequestParams], Awaitable[types.GetPromptResult | types.InputRequiredResult]] | None' = None, on_completion: 'Callable[[ServerRequestContext[LifespanResultT], types.CompleteRequestParams], Awaitable[types.CompleteResult]] | None' = None, on_set_logging_level: 'Callable[[ServerRequestContext[LifespanResultT], types.SetLevelRequestParams], Awaitable[types.EmptyResult]] | None' = None, on_ping: 'Callable[[ServerRequestContext[LifespanResultT], types.RequestParams | None], Awaitable[types.EmptyResult]]' = <function _ping_handler at 0x10c6e90c0>, on_roots_list_changed: 'Callable[[ServerRequestContext[LifespanResultT], types.NotificationParams | None], Awaitable[None]] | None' = None, on_progress: 'Callable[[ServerRequestContext[LifespanResultT], types.ProgressNotificationParams], Awaitable[None]] | None' = None) -> 'None'
    on_list_tools signature: (_context: Any, _params: mcp_types._types.PaginatedRequestParams | None) -> mcp_types._types.ListToolsResult
    on_call_tool signature: (_context: Any, params: mcp_types._types.CallToolRequestParams) -> mcp_types._types.CallToolResult
    registered tools/list: HandlerEntry(params_type=<class 'mcp_types._types.PaginatedRequestParams'>, handler=<function on_list_tools at 0x10a79f530>)
    registered tools/call: HandlerEntry(params_type=<class 'mcp_types._types.CallToolRequestParams'>, handler=<function on_call_tool at 0x10cda4510>)
    initial tools: []
    after runtime discovery: ['runtime_ok', 'runtime_error', 'text_only_json', 'structured_with_prose', 'both_channels', 'two_text_blocks']
    runtime_ok result: {'_meta': {'io.modelcontextprotocol/serverInfo': {'name': 'sluice-m0', 'version': ''}}, 'content': [{'type': 'text', 'text': 'called runtime_ok'}], 'structuredContent': {'called': 'runtime_ok', 'arguments': {'n': 7}}, 'isError': False, 'resultType': 'complete'}
    runtime_error result: {'_meta': {'io.modelcontextprotocol/serverInfo': {'name': 'sluice-m0', 'version': ''}}, 'content': [{'type': 'text', 'text': 'called runtime_error'}], 'structuredContent': {'called': 'runtime_error', 'arguments': {'n': 8}}, 'isError': True, 'resultType': 'complete'}

    STEP 6: payload channel shapes after SDK round trip
    text_only_json:
      content=[TextContent(type='text', text='{"items":[{"id":1},{"id":2}]}', annotations=None, meta=None)]
      structured_content=None
      is_error=False
      wire_alias_dump={'_meta': {'io.modelcontextprotocol/serverInfo': {'name': 'sluice-m0', 'version': ''}}, 'content': [{'type': 'text', 'text': '{"items":[{"id":1},{"id":2}]}'}], 'isError': False, 'resultType': 'complete'}
    structured_with_prose:
      content=[TextContent(type='text', text='Returned two records.', annotations=None, meta=None)]
      structured_content={'items': [{'id': 1}, {'id': 2}]}
      is_error=False
      wire_alias_dump={'_meta': {'io.modelcontextprotocol/serverInfo': {'name': 'sluice-m0', 'version': ''}}, 'content': [{'type': 'text', 'text': 'Returned two records.'}], 'structuredContent': {'items': [{'id': 1}, {'id': 2}]}, 'isError': False, 'resultType': 'complete'}
    both_channels:
      content=[TextContent(type='text', text='{"items":[{"id":1},{"id":2}]}', annotations=None, meta=None)]
      structured_content={'items': [{'id': 1}, {'id': 2}]}
      is_error=False
      wire_alias_dump={'_meta': {'io.modelcontextprotocol/serverInfo': {'name': 'sluice-m0', 'version': ''}}, 'content': [{'type': 'text', 'text': '{"items":[{"id":1},{"id":2}]}'}], 'structuredContent': {'items': [{'id': 1}, {'id': 2}]}, 'isError': False, 'resultType': 'complete'}
    two_text_blocks:
      content=[TextContent(type='text', text='{"left":1}', annotations=None, meta=None), TextContent(type='text', text='{"right":2}', annotations=None, meta=None)]
      structured_content=None
      is_error=False
      wire_alias_dump={'_meta': {'io.modelcontextprotocol/serverInfo': {'name': 'sluice-m0', 'version': ''}}, 'content': [{'type': 'text', 'text': '{"left":1}'}, {'type': 'text', 'text': '{"right":2}'}], 'isError': False, 'resultType': 'complete'}

## 7. Peak memory and temporary-file cleanup

Result: **measured; cleanup passed**.

The representative pre-implementation pipeline processed a 52,435,371-byte
(50.006 MiB) JSON array containing 6,506 rows. It retained the incoming text,
parsed object graph, projected rows, an in-memory NDJSON rendering, and the
DuckDB table, matching the simultaneous representations named by spec §5.4.

- Python-tracked peak: 216,123,055 bytes, or 4.122 times payload size.
- Process RSS high-water peak: 509,673,472 bytes, or 9.720 times payload size.
- RSS high-water increase after the input payload had been built: 289,112,064
  bytes, or 5.514 times payload size.

resource.getrusage reports a process high-water mark rather than current RSS.
The "increment after payload" number is therefore conservative: constructing
the input from fragments had already raised the earlier high-water mark.
tracemalloc was reset after input construction and gives the cleaner Python
allocation multiple, but it does not see DuckDB's native allocations. Both
measurements are retained rather than treating either as the whole process.

This was the discarded file-based pipeline and is superseded by the file-free
benchmark in `benchmarks/results/memory-2026-08-30.md`. That benchmark measured
the current path across four shapes and two-call concurrency, and lowered the
product default to 24 MiB for the target assumption of a 1 GiB process-memory
budget. Neither measurement is a cross-platform guarantee; `ru_maxrss` is a
process high-water mark and the SDK may decode `structuredContent` before the
Sluice ceiling is applied.

The success-path NDJSON file was absent after loading. A deliberately malformed
NDJSON file raised InvalidInputException and was also absent after the finally
block.

Script
(SHA-256 6b9fdb09f2cc1fcd6a7a8d33fc813fce9f21c33876b35e648b31399c1a2f6781):

    import gc
    import json
    import os
    import platform
    import resource
    import tempfile
    import tracemalloc
    import uuid
    from pathlib import Path

    import duckdb


    TARGET_BYTES = 50 * 1024 * 1024
    BLOB_BYTES = 8_000


    def rss_peak_bytes() -> int:
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes; Linux reports KiB.
        return int(raw if platform.system() == "Darwin" else raw * 1024)


    def memory_line(stage: str) -> None:
        current, peak = tracemalloc.get_traced_memory()
        print(
            f"{stage}: traced_current={current} traced_peak={peak} "
            f"rss_peak={rss_peak_bytes()}"
        )


    def load_with_cleanup(
        connection: duckdb.DuckDBPyConnection,
        temp_dir: Path,
        table_name: str,
        text: str,
    ) -> tuple[Path, str]:
        path: Path | None = None
        outcome = "success"
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".ndjson",
                dir=temp_dir,
                delete=False,
            ) as stream:
                path = Path(stream.name)
                stream.write(text)
            connection.execute(
                f"""
                CREATE TABLE {table_name} AS
                SELECT * FROM read_json(
                    ?,
                    format = 'newline_delimited',
                    union_by_name = true,
                    sample_size = -1
                )
                """,
                [str(path)],
            )
        except Exception as exc:
            outcome = f"{type(exc).__name__}: {exc}"
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
        assert path is not None
        return path, outcome


    print("STEP 7: 50 MiB materialization memory and temp-file cleanup")
    print(f"platform={platform.platform()}")
    print(f"target_bytes={TARGET_BYTES}")
    tracemalloc.start()

    # Build an incoming JSON text result without retaining a second object graph.
    fragments: list[str] = []
    index = 0
    estimated = 2
    while estimated < TARGET_BYTES:
        row = json.dumps(
            {
                "id": index,
                "score": index % 1000,
                "text": "x" * BLOB_BYTES,
                "nested": {"ordinal": index},
            },
            separators=(",", ":"),
        )
        fragments.append(row)
        estimated += len(row.encode("utf-8")) + (1 if index else 0)
        index += 1
    payload = "[" + ",".join(fragments) + "]"
    del fragments
    gc.collect()
    payload_bytes = len(payload.encode("utf-8"))
    print(f"rows={index}")
    print(f"payload_bytes={payload_bytes}")
    print(f"payload_mib={payload_bytes / 1024 / 1024:.6f}")
    tracemalloc.reset_peak()
    rss_after_payload = rss_peak_bytes()
    memory_line("baseline_after_payload")

    # The representative pre-code pipeline follows spec sections 5.1, 5.3, and 5.4.
    parsed = json.loads(payload)
    memory_line("after_json_loads")
    call_id = str(uuid.uuid4())
    projected = [
        {
            "id": row["id"],
            "score": row["score"],
            "text": row["text"],
            "nested": json.dumps(row["nested"], separators=(",", ":")),
            "_row": ordinal,
            "_call_id": call_id,
        }
        for ordinal, row in enumerate(parsed)
    ]
    memory_line("after_projection")
    ndjson = "\n".join(
        json.dumps(row, separators=(",", ":"), ensure_ascii=False)
        for row in projected
    )
    memory_line("after_ndjson_buffer")

    connection = duckdb.connect(":memory:step7")
    with tempfile.TemporaryDirectory(prefix="sluice-m0-step7-") as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)
        success_path, success_outcome = load_with_cleanup(
            connection,
            temp_dir,
            "materialized",
            ndjson,
        )
        memory_line("after_duckdb_load")
        print(f"loaded_rows={connection.execute('SELECT count(*) FROM materialized').fetchone()[0]}")
        print(f"success_outcome={success_outcome}")
        print(f"success_temp_exists_after_finally={success_path.exists()}")

        failure_path, failure_outcome = load_with_cleanup(
            connection,
            temp_dir,
            "must_fail",
            '{"valid":1}\n{"broken":',
        )
        print(f"failure_outcome={failure_outcome}")
        print(f"failure_temp_exists_after_finally={failure_path.exists()}")

    current, traced_peak = tracemalloc.get_traced_memory()
    rss_final_peak = rss_peak_bytes()
    print(f"traced_current_bytes={current}")
    print(f"traced_peak_bytes={traced_peak}")
    print(f"traced_peak_multiple={traced_peak / payload_bytes:.6f}")
    print(f"rss_peak_bytes={rss_final_peak}")
    print(f"rss_peak_absolute_multiple={rss_final_peak / payload_bytes:.6f}")
    print(f"rss_peak_increment_after_payload={rss_final_peak - rss_after_payload}")
    print(
        "rss_increment_multiple_after_payload="
        f"{(rss_final_peak - rss_after_payload) / payload_bytes:.6f}"
    )

Command:

    /tmp/sluice-m0.X210IN/.venv/bin/python /tmp/sluice-m0.X210IN/step7.py

Raw output:

    STEP 7: 50 MiB materialization memory and temp-file cleanup
    platform=macOS-26.6.2-arm64-arm-64bit-Mach-O
    target_bytes=52428800
    rows=6506
    payload_bytes=52435371
    payload_mib=50.006267
    baseline_after_payload: traced_current=52444063 traced_peak=52444239 rss_peak=220561408
    after_json_loads: traced_current=107688694 traced_peak=107689876 rss_peak=281001984
    after_projection: traced_current=110081731 traced_peak=110082249 rss_peak=286769152
    after_ndjson_buffer: traced_current=162945895 traced_peak=216123055 rss_peak=339984384
    after_duckdb_load: traced_current=163113909 traced_peak=216123055 rss_peak=508624896
    loaded_rows=6506
    success_outcome=success
    success_temp_exists_after_finally=False
    failure_outcome=InvalidInputException: Invalid Input Error: Malformed JSON in file "/var/folders/1d/9l26_7015fv6gxzk9w3by19w0000gn/T/sluice-m0-step7-2xqu51re/tmpfr5nlmgt.ndjson", at byte 11 in line 3: unexpected end of data.

    LINE 3:             SELECT * FROM read_json(
                                      ^
    failure_temp_exists_after_finally=False
    traced_current_bytes=163118973
    traced_peak_bytes=216123055
    traced_peak_multiple=4.121704
    rss_peak_bytes=509673472
    rss_peak_absolute_multiple=9.720032
    rss_peak_increment_after_payload=289112064
    rss_increment_multiple_after_payload=5.513684

## 8. Full-scan NDJSON inference

Result: **mixed**.

- The heterogeneous 301-row input loaded successfully. Missing keys became
  NULLs, and the column holding 300 integers followed by one string was inferred
  as JSON. Full-scan inference therefore removed the late-type load failure for
  this case.
- ISO timestamp-shaped JSON strings were inferred as TIMESTAMP. The returned
  Python datetime values were naive even though the inputs ended in Z.
- Values from 2^63 through 2^64-1 loaded exactly as HUGEINT.
- Values above the unsigned 64-bit range were inferred as DOUBLE. A large run of
  nines was returned as 1e+29, demonstrating precision loss.
- JSON decimal numbers were inferred as DOUBLE.
- Quoted decimal-shaped JSON strings remained VARCHAR; they were not inferred as
  DOUBLE.
- Every test NDJSON file was removed after its load attempt.

Script
(SHA-256 3e97d19c20595a98ef8785f40f4010bc3584dc2c68913e1b424e2d526a57013a):

    import json
    import tempfile
    from pathlib import Path
    from typing import Any

    import duckdb


    def load_case(
        connection: duckdb.DuckDBPyConnection,
        temp_dir: Path,
        table_name: str,
        rows: list[dict[str, Any]],
    ) -> None:
        path = temp_dir / f"{table_name}.ndjson"
        try:
            path.write_text(
                "\n".join(json.dumps(row, separators=(",", ":")) for row in rows),
                encoding="utf-8",
            )
            connection.execute(
                f"""
                CREATE TABLE {table_name} AS
                SELECT * FROM read_json(
                    ?,
                    format = 'newline_delimited',
                    union_by_name = true,
                    sample_size = -1
                )
                """,
                [str(path)],
            )
            schema = connection.execute(f"DESCRIBE {table_name}").fetchall()
            print(f"{table_name}: load=success rows={len(rows)}")
            print(f"  schema={schema}")
            print(
                "  duckdb_columns="
                f"{connection.execute('SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = ? ORDER BY column_index', [table_name]).fetchall()}"
            )
            print(
                "  values="
                f"{connection.execute(f'SELECT * FROM {table_name} LIMIT 3').fetchall()}"
            )
            if len(rows) > 3:
                print(
                    "  tail="
                    f"{connection.execute(f'SELECT * FROM {table_name} ORDER BY rowid DESC LIMIT 2').fetchall()}"
                )
        except Exception as exc:
            print(f"{table_name}: load=failure rows={len(rows)}")
            print(f"  {type(exc).__name__}: {exc}")
        finally:
            path.unlink(missing_ok=True)
            print(f"  temp_exists_after_finally={path.exists()}")


    print("STEP 8: full-scan JSON inference")
    connection = duckdb.connect(":memory:step8")
    with tempfile.TemporaryDirectory(prefix="sluice-m0-step8-") as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)

        heterogeneous: list[dict[str, Any]] = []
        for index in range(300):
            row: dict[str, Any] = {"drift": index}
            row["even_only" if index % 2 == 0 else "odd_only"] = index
            heterogeneous.append(row)
        heterogeneous.append({"drift": "late-string", "late_only": True})
        load_case(connection, temp_dir, "heterogeneous", heterogeneous)

        load_case(
            connection,
            temp_dir,
            "timestamp_strings",
            [
                {"candidate": "2026-08-28T12:34:56Z"},
                {"candidate": "2025-01-02T03:04:05Z"},
            ],
        )

        load_case(
            connection,
            temp_dir,
            "beyond_int64",
            [
                {"candidate": 9_223_372_036_854_775_808},
                {"candidate": 18_446_744_073_709_551_615},
            ],
        )

        load_case(
            connection,
            temp_dir,
            "beyond_uint64",
            [
                {"candidate": 18_446_744_073_709_551_616},
                {"candidate": 99_999_999_999_999_999_999_999_999_999},
            ],
        )

        load_case(
            connection,
            temp_dir,
            "decimal_numbers",
            [
                {"candidate": 0.1},
                {"candidate": 1234567890.1234567},
            ],
        )

        load_case(
            connection,
            temp_dir,
            "decimal_strings",
            [
                {"candidate": "0.10"},
                {"candidate": "1234567890.123456789"},
            ],
        )

    connection.close()

Command:

    /tmp/sluice-m0.X210IN/.venv/bin/python /tmp/sluice-m0.X210IN/step8.py

Raw output:

    STEP 8: full-scan JSON inference
    heterogeneous: load=success rows=301
      schema=[('drift', 'JSON', 'YES', None, None, None), ('even_only', 'BIGINT', 'YES', None, None, None), ('odd_only', 'BIGINT', 'YES', None, None, None), ('late_only', 'BOOLEAN', 'YES', None, None, None)]
      duckdb_columns=[('drift', 'JSON'), ('even_only', 'BIGINT'), ('odd_only', 'BIGINT'), ('late_only', 'BOOLEAN')]
      values=[('0', 0, None, None), ('1', None, 1, None), ('2', 2, None, None)]
      tail=[('"late-string"', None, None, True), ('299', None, 299, None)]
      temp_exists_after_finally=False
    timestamp_strings: load=success rows=2
      schema=[('candidate', 'TIMESTAMP', 'YES', None, None, None)]
      duckdb_columns=[('candidate', 'TIMESTAMP')]
      values=[(datetime.datetime(2026, 8, 28, 12, 34, 56),), (datetime.datetime(2025, 1, 2, 3, 4, 5),)]
      temp_exists_after_finally=False
    beyond_int64: load=success rows=2
      schema=[('candidate', 'HUGEINT', 'YES', None, None, None)]
      duckdb_columns=[('candidate', 'HUGEINT')]
      values=[(9223372036854775808,), (18446744073709551615,)]
      temp_exists_after_finally=False
    beyond_uint64: load=success rows=2
      schema=[('candidate', 'DOUBLE', 'YES', None, None, None)]
      duckdb_columns=[('candidate', 'DOUBLE')]
      values=[(1.8446744073709552e+19,), (1e+29,)]
      temp_exists_after_finally=False
    decimal_numbers: load=success rows=2
      schema=[('candidate', 'DOUBLE', 'YES', None, None, None)]
      duckdb_columns=[('candidate', 'DOUBLE')]
      values=[(0.1,), (1234567890.1234567,)]
      temp_exists_after_finally=False
    decimal_strings: load=success rows=2
      schema=[('candidate', 'VARCHAR', 'YES', None, None, None)]
      duckdb_columns=[('candidate', 'VARCHAR')]
      values=[('0.10',), ('1234567890.123456789',)]
      temp_exists_after_finally=False

## Spec sections contradicted by observed results

1. **Spec §6.2, timeout mechanism wording.** Calling interrupt() on a parent
   connection does not interrupt a query executing on a derived cursor in
   DuckDB 1.5.5. The statement that the worker executes on a dedicated cursor
   while the event loop interrupts "the connection" is only correct if that
   means the exact DuckDBPyConnection object returned as the cursor, not its
   parent. Spec §9's current dedicated query-connection default is supported by
   the isolation results and should not collapse to a parent-plus-child
   implementation whose watchdog interrupts the parent.

2. **Spec §5.3, integer inference hazard.** The statement that integers outside
   int64 either fail or widen to DOUBLE is too broad for DuckDB 1.5.5. Values
   through unsigned 64-bit max were assigned HUGEINT and preserved exactly.
   Values above that tested boundary widened to DOUBLE.

3. **Spec §5.3, decimal-shaped string inference hazard.** Quoted decimal-shaped
   JSON strings were assigned VARCHAR, not DOUBLE. JSON numeric decimals were
   assigned DOUBLE and retain the stated precision hazard, but they are numbers,
   not strings.

No other M0 result contradicted the current spec. In particular, the binary
wheel gate passed; the statement parser and two tested lockdown assertions
behaved as specified; dynamic MCP tools and per-call result fields worked; the
SDK preserved payload channels distinctly; and full-scan inference loaded the
late-type heterogeneous input.

    separate connections to one named in-memory database
      query_worker_alive=False
      write_worker_alive=False
      seconds_until_both_done=1.393966
      query_outcome={'error': 'InterruptException: INTERRUPT Error: Interrupted!', 'elapsed_seconds': 0.202882}
      write_outcome={'value_before_commit': (0.31293218183628124,), 'committed': True, 'elapsed_seconds': 1.596601}
      committed_value_seen_by_observer=(0.31293218183628124,)
      query_connection_usable=(43,)
      writer_connection_usable=(44,)
      observer_connection_usable=(45,)

## 9. Supplementary checks (Claude, same day)

M0 step 3 asserted two of the six lockdown cases the plan listed. The remaining
four were run, along with follow-ups the results implied. Environment: the same
`/tmp/sluice-m0.X210IN/.venv`, DuckDB 1.5.5, CPython 3.14.2.

### 9.1 The lockdown blocks Sluice's own materialization (blocking)

`read_json` over a temp NDJSON file, after `SET enable_external_access = false`
and `SET lock_configuration = true`:

    PermissionException: Permission Error: Cannot access file
    ".../rows.ndjson" - file system operations are disabled by configuration

Spec §6.1 and the then-current §5.4 were mutually exclusive. Resolved by making
materialization file-free; see spec §5.4 and §5.5.

### 9.2 Remaining lockdown assertions

Under the full lockdown, all blocked with `PermissionException`:
`read_parquet`, `ATTACH`, `COPY ... TO`, `INSTALL httpfs`, `LOAD httpfs`,
`read_json` on an outside path, `glob('/etc/*')`.

`PRAGMA database_list` **succeeded**. The engine lockdown does not stop PRAGMA;
only the layer-1 statement gate does. Noted in spec §6.1.

### 9.3 Two rejected workarounds

- `SET allowed_directories = ['<session tmp>']` does not confine access. Under
  it, `read_csv('/etc/hosts')`, `COPY ... TO '/tmp/...'`, `ATTACH`,
  `INSTALL httpfs`, and `glob('/etc/*')` all succeeded.
- `enable_external_access` is database-global. Two connections to one named
  in-memory database, with the setting locked off on the first, were both
  blocked. A writer-with-access plus query-without-access split is not possible.

### 9.4 File-free materialization works

`CREATE TABLE ... (explicit column types)` plus `executemany`, under
`enable_external_access = false` and `lock_configuration = true`: succeeded,
with `JSON` columns still queryable through `json_extract` and castable for
aggregation.

### 9.5 JSON columns are aggregation traps

A column holding 300 integers then one string, loaded by DuckDB's own inference,
became `JSON`. On that column:

    count(v)                   -> 301
    sum(v)                     -> BinderException: no function sum(JSON)
    avg(v)                     -> BinderException: no function avg(JSON)
    median(v)                  -> '232'          <-- lexicographic, silently wrong
    sum(TRY_CAST(v AS DOUBLE)) -> 44850.0

`median()` returning a plausible number that is wrong is the failure mode this
project exists to prevent. Spec §5.5 rule 2 assigns `VARCHAR` to mixed scalar
columns so the same query fails loudly instead.

### 9.6 Aggregate equality against Python (the correctness criterion)

    n=400  median duck=339.15               py=339.15   equal=True
    n=400  avg    duck=339.1499999999999    py=339.15   equal=False
    n=401  median duck=340.0                py=340.0    equal=True
    n=401  avg    duck=339.99999999999994   py=340.0    equal=False
    n=400  median over BIGINT duck=199.5    py=199.5    equal=True

`median` is exactly equal for integer and float columns at even and odd row
counts. `avg` is not, because summation order differs. The property test
therefore needs two classes of assertion; see plan §3.

### 9.7 Statement gate, trailing semicolons

    'SELECT 1;'                             -> count=1 ['SELECT']
    'SELECT 1; '                            -> count=1 ['SELECT']
    'SELECT 1;\n'                           -> count=1 ['SELECT']
    'SELECT 1; SELECT 2'                    -> count=2 ['SELECT', 'SELECT']
    'SELECT 1 -- ; comment'                 -> count=1 ['SELECT']
    'WITH x AS (SELECT 1) SELECT * FROM x;' -> count=1 ['SELECT']

The layer-1 gate handles trailing semicolons, a semicolon inside a comment, and
CTEs without special-casing. Spec §6.3 drops the `SELECT * FROM (<sql>)` wrapper
for unrelated reasons, but this confirms the gate itself needs no preprocessing.
