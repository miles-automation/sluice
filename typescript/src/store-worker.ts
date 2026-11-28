import { configureThreadpool } from "./threadpool.ts";

configureThreadpool();
const { serveStore } = await import("./store-server.ts");
serveStore(Number(process.argv[2]), Number(process.argv[3]));
