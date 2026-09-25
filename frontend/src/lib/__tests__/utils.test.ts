import { afterEach, describe, expect, test, vi } from 'vitest';

import { formatDuration, num, pct, relativeTime } from '@/lib/utils';

/**
 * Number formatting.
 *
 * The guarantee under test is narrower than it looks: a value that could not be
 * measured must never be rendered as zero. On this console the difference is
 * operational - "error rate 0%" invites an engineer to close the incident,
 * whereas "error rate —" tells them Prometheus never answered and the judgement
 * is still theirs to make. This is invariant 6 at the smallest possible scale.
 */

afterEach(() => {
  vi.useRealTimers();
});

describe('pct', () => {
  test('an unmeasured percentage renders as a dash, never as zero', () => {
    for (const absent of [null, undefined, Number.NaN]) {
      expect(pct(absent)).toBe('—');
      expect(pct(absent)).not.toBe('0%');
    }
  });

  test('a measured zero still renders as zero', () => {
    // The mirror of the rule above. Suppressing a genuine zero would hide a
    // healthy signal behind the same dash used for a missing one.
    expect(pct(0)).toBe('0%');
  });

  test('percentages carry only the precision asked for', () => {
    expect(pct(0.5)).toBe('50%');
    expect(pct(0.12345, 1)).toBe('12.3%');
    expect(pct(1)).toBe('100%');
  });
});

describe('num', () => {
  test('an unmeasured number renders as a dash, never as zero', () => {
    for (const absent of [null, undefined, Number.NaN]) {
      expect(num(absent)).toBe('—');
      expect(num(absent)).not.toBe('0.00');
    }
  });

  test('a measured zero still renders as zero', () => {
    expect(num(0)).toBe('0.00');
    expect(num(0, 0)).toBe('0');
  });

  test('numbers carry only the precision asked for', () => {
    expect(num(1.005, 2)).toBe('1.00');
    expect(num(42, 0)).toBe('42');
  });
});

describe('formatDuration', () => {
  test('a duration that is not a real measurement renders as a dash', () => {
    // A negative or non-finite span is arithmetic on a missing timestamp.
    // Rendering "-1s" or "NaNs" in a timeline would be read as data.
    expect(formatDuration(Number.NaN)).toBe('—');
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBe('—');
    expect(formatDuration(-1)).toBe('—');
  });

  test('durations step up through the units a dense table can fit', () => {
    expect(formatDuration(0)).toBe('0s');
    expect(formatDuration(48_000)).toBe('48s');
    expect(formatDuration(763_000)).toBe('12m 43s');
    expect(formatDuration(7_800_000)).toBe('2h 10m');
    expect(formatDuration(93_600_000)).toBe('1d 2h');
  });
});

describe('relativeTime', () => {
  test('an absent or unparseable timestamp renders as a dash, never as "0s ago"', () => {
    // "0s ago" on an incident that has no recorded activity would read as
    // "happening right now" - the most misleading value the field could take.
    expect(relativeTime(null)).toBe('—');
    expect(relativeTime(undefined)).toBe('—');
    expect(relativeTime('')).toBe('—');
    expect(relativeTime('not a timestamp')).toBe('—');
  });

  test('recent activity is reported in seconds and older activity in the compact units', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T12:00:00.000Z'));

    expect(relativeTime('2026-01-01T11:59:50.000Z')).toBe('10s ago');
    expect(relativeTime('2026-01-01T11:47:17.000Z')).toBe('12m 43s ago');
    expect(relativeTime('2026-01-01T09:50:00.000Z')).toBe('2h 10m ago');
  });

  test('a clock-skewed future timestamp is clamped rather than shown as negative', () => {
    // Container clocks drift. "-8s ago" is a bug report; "0s ago" is a
    // rounding decision.
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T12:00:00.000Z'));

    expect(relativeTime('2026-01-01T12:00:08.000Z')).toBe('0s ago');
  });
});
