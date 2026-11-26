import { TomlDate } from "smol-toml";
import { pythonFloatRepr } from "./json.ts";

const ESCAPES: Readonly<Record<string, string>> = { "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t" };

function pad(value: number, width = 2): string {
  return String(value).padStart(width, "0");
}

function stringRepr(text: string): string {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let out = quote;
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0;
    if (char in ESCAPES) {
      out += ESCAPES[char];
    } else if (char === quote) {
      out += `\\${quote}`;
    } else if (code < 0x20 || code === 0x7f) {
      out += `\\x${code.toString(16).padStart(2, "0")}`;
    } else {
      out += char;
    }
  }
  return `${out}${quote}`;
}

function floatText(value: number): string {
  if (Number.isNaN(value)) {
    return "nan";
  }
  if (!Number.isFinite(value)) {
    return value > 0 ? "inf" : "-inf";
  }
  return pythonFloatRepr(value);
}

interface Parts {
  readonly year: number;
  readonly month: number;
  readonly day: number;
  readonly hour: number;
  readonly minute: number;
  readonly second: number;
  readonly micro: number;
  readonly offsetMinutes: number | null;
}

const ISO = /^(?:(\d{4})-(\d{2})-(\d{2}))?T?(?:(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?)?(Z|[+-]\d{2}:\d{2})?$/u;

function parts(date: TomlDate): Parts {
  const found = ISO.exec(date.toISOString());
  if (found === null) {
    throw new TypeError(`unrecognized TOML date ${date.toISOString()}`);
  }
  const number = (index: number): number => Number(found[index] ?? "0");
  const zone = found[8];
  let offsetMinutes: number | null = null;
  if (zone === "Z") {
    offsetMinutes = 0;
  } else if (zone !== undefined) {
    const sign = zone.startsWith("-") ? -1 : 1;
    offsetMinutes = sign * (Number(zone.slice(1, 3)) * 60 + Number(zone.slice(4, 6)));
  }
  return {
    year: number(1),
    month: number(2),
    day: number(3),
    hour: number(4),
    minute: number(5),
    second: number(6),
    micro: Number((found[7] ?? "").padEnd(6, "0").slice(0, 6)),
    offsetMinutes,
  };
}

function timeText(value: Parts): string {
  const base = `${pad(value.hour)}:${pad(value.minute)}:${pad(value.second)}`;
  return value.micro === 0 ? base : `${base}.${pad(value.micro, 6)}`;
}

function offsetText(minutes: number): string {
  const sign = minutes < 0 ? "-" : "+";
  const total = Math.abs(minutes);
  return `${sign}${pad(Math.floor(total / 60))}:${pad(total % 60)}`;
}

function dateStr(date: TomlDate): string {
  const value = parts(date);
  const day = `${value.year}-${pad(value.month)}-${pad(value.day)}`;
  if (date.isDate()) {
    return day;
  }
  if (date.isTime()) {
    return timeText(value);
  }
  const local = `${day} ${timeText(value)}`;
  return value.offsetMinutes === null ? local : `${local}${offsetText(value.offsetMinutes)}`;
}

function timeFields(value: Parts): number[] {
  const fields = [value.hour, value.minute];
  if (value.second !== 0 || value.micro !== 0) {
    fields.push(value.second);
  }
  if (value.micro !== 0) {
    fields.push(value.micro);
  }
  return fields;
}

function timezoneRepr(minutes: number): string {
  if (minutes === 0) {
    return "datetime.timezone.utc";
  }
  const seconds = minutes * 60;
  const days = Math.floor(seconds / 86400);
  const rest = seconds - days * 86400;
  const fields = [days !== 0 ? `days=${days}` : "", rest !== 0 ? `seconds=${rest}` : ""].filter((field) => field !== "");
  return `datetime.timezone(datetime.timedelta(${fields.join(", ")}))`;
}

function dateRepr(date: TomlDate): string {
  const value = parts(date);
  if (date.isDate()) {
    return `datetime.date(${value.year}, ${value.month}, ${value.day})`;
  }
  if (date.isTime()) {
    return `datetime.time(${timeFields(value).join(", ")})`;
  }
  const fields = [value.year, value.month, value.day, ...timeFields(value)].join(", ");
  return value.offsetMinutes === null
    ? `datetime.datetime(${fields})`
    : `datetime.datetime(${fields}, tzinfo=${timezoneRepr(value.offsetMinutes)})`;
}

export function pyRepr(value: unknown): string {
  if (typeof value === "string") {
    return stringRepr(value);
  }
  if (value instanceof TomlDate) {
    return dateRepr(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(pyRepr).join(", ")}]`;
  }
  if (typeof value === "object" && value !== null) {
    return `{${Object.entries(value).map(([key, item]) => `${stringRepr(key)}: ${pyRepr(item)}`).join(", ")}}`;
  }
  return pyStr(value);
}

export function pyStr(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "boolean") {
    return value ? "True" : "False";
  }
  if (typeof value === "bigint") {
    return value.toString();
  }
  if (typeof value === "number") {
    return floatText(value);
  }
  if (value instanceof TomlDate) {
    return dateStr(value);
  }
  return pyRepr(value);
}
