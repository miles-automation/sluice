export const THREADPOOL_ENV = "UV_THREADPOOL_SIZE";
export const LIBUV_DEFAULT_THREADPOOL_SIZE = 4;
export const LIBUV_MAX_THREADPOOL_SIZE = 1024;
export const RECOMMENDED_THREADPOOL_SIZE = 8;
export const THREADPOOL_RESERVE = 2;

type Environment = Readonly<Record<string, string | undefined>>;

export function threadpoolSize(environment: Environment = process.env): number {
  const raw = environment[THREADPOOL_ENV];
  const parsed = raw === undefined ? Number.NaN : Number.parseInt(raw, 10);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    return LIBUV_DEFAULT_THREADPOOL_SIZE;
  }
  return Math.min(parsed, LIBUV_MAX_THREADPOOL_SIZE);
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
