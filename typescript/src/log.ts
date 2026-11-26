export const LEVELS = { NOTSET: 0, DEBUG: 10, INFO: 20, WARN: 30, WARNING: 30, ERROR: 40, FATAL: 50, CRITICAL: 50 } as const;

export type LevelName = keyof typeof LEVELS;

let threshold: number = LEVELS.INFO;

export function isLevelName(value: string): value is LevelName {
  return Object.hasOwn(LEVELS, value);
}

export function setLevel(name: LevelName): void {
  threshold = LEVELS[name];
}

export interface Logger {
  debug(message: string): void;
  info(message: string): void;
  warning(message: string): void;
  error(message: string): void;
}

export function logger(name: string): Logger {
  const emit = (level: LevelName, message: string): void => {
    if (LEVELS[level] >= threshold) {
      process.stderr.write(`${level} ${name}: ${message}\n`);
    }
  };
  return {
    debug: (message) => emit("DEBUG", message),
    info: (message) => emit("INFO", message),
    warning: (message) => emit("WARNING", message),
    error: (message) => emit("ERROR", message),
  };
}
