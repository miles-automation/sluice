export const THREADPOOL_ENV = "UV_THREADPOOL_SIZE";
export const LIBUV_DEFAULT_THREADPOOL_SIZE = 4;
export const LIBUV_MAX_THREADPOOL_SIZE = 1024;
export const RECOMMENDED_THREADPOOL_SIZE = 8;
export const THREADPOOL_RESERVE = 2;

type Environment = Readonly<Record<string, string | undefined>>;

const LONG_MAX = 2n ** 63n - 1n;
const LONG_MIN = -(2n ** 63n);
const ATOI_PREFIX = /^[ \t\n\v\f\r]*([+-]?[0-9]+)/u;

export function threadpoolSize(environment: Environment = process.env): number {
  const raw = environment[THREADPOOL_ENV];
  if (raw === undefined) {
    return LIBUV_DEFAULT_THREADPOOL_SIZE;
  }
  const digits = ATOI_PREFIX.exec(raw)?.[1];
  const parsed = digits === undefined ? 0n : BigInt(digits);
  const long = parsed > LONG_MAX ? LONG_MAX : parsed < LONG_MIN ? LONG_MIN : parsed;
  const threads = BigInt.asUintN(32, long);
  if (threads === 0n) {
    return 1;
  }
  return threads > BigInt(LIBUV_MAX_THREADPOOL_SIZE) ? LIBUV_MAX_THREADPOOL_SIZE : Number(threads);
}

export function queryConcurrency(poolSize: number = threadpoolSize()): number {
  return Math.max(1, poolSize - THREADPOOL_RESERVE);
}

export function configureThreadpool(
  size: number = RECOMMENDED_THREADPOOL_SIZE,
  environment: Record<string, string | undefined> = process.env,
): number {
  if (environment[THREADPOOL_ENV] === undefined) {
    environment[THREADPOOL_ENV] = String(size);
  }
  return threadpoolSize(environment);
}
