export class Semaphore {
  #available: number;
  readonly #waiters: (() => void)[] = [];

  constructor(permits: number) {
    if (!Number.isSafeInteger(permits) || permits < 1) {
      throw new RangeError(`a semaphore needs at least one permit, got ${permits}`);
    }
    this.#available = permits;
  }

  get available(): number {
    return this.#available;
  }

  get waiting(): number {
    return this.#waiters.length;
  }

  acquire(): Promise<() => void> {
    if (this.#available > 0) {
      this.#available -= 1;
      return Promise.resolve(this.#releaser());
    }
    return new Promise((resolve) => {
      this.#waiters.push(() => resolve(this.#releaser()));
    });
  }

  async run<T>(work: () => Promise<T>): Promise<T> {
    const release = await this.acquire();
    try {
      return await work();
    } finally {
      release();
    }
  }

  #releaser(): () => void {
    let released = false;
    return () => {
      if (released) {
        return;
      }
      released = true;
      const next = this.#waiters.shift();
      if (next === undefined) {
        this.#available += 1;
      } else {
        next();
      }
    };
  }
}

export class Mutex {
  readonly #semaphore = new Semaphore(1);

  run<T>(work: () => Promise<T>): Promise<T> {
    return this.#semaphore.run(work);
  }
}
