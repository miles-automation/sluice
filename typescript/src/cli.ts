#!/usr/bin/env node
import { configureThreadpool } from "./threadpool.ts";

configureThreadpool();
const { main } = await import("./main.ts");
const code = await main(process.argv.slice(2));
process.exit(code);
