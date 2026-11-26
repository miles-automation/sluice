export const MAX_TIMER_MS = 2_147_483_647;

export function longTimeout(fire: () => void, ms: number): () => void {
  const deadline = performance.now() + ms;
  let handle: ReturnType<typeof setTimeout> | undefined;
  const arm = (): void => {
    const remaining = deadline - performance.now();
    handle = remaining > MAX_TIMER_MS ? setTimeout(arm, MAX_TIMER_MS) : setTimeout(fire, Math.max(remaining, 0));
  };
  arm();
  return () => clearTimeout(handle);
}
