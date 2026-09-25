import { describe, expect, test } from 'vitest';

import { backoffMs, parseSse } from '@/lib/war-room/sse';
import { isTypingTarget, resolveShortcut } from '@/lib/war-room/shortcuts';

/** Framing rules a live stream will exercise within minutes of a demo. */
describe('parseSse', () => {
  test('complete frames are returned and a partial tail is kept for the next read', () => {
    const { frames, rest } = parseSse('id: 4\nevent: horizon\ndata: {"seq":4}\n\nid: 5\nevent: hor');
    expect(frames).toEqual([{ id: '4', event: 'horizon', data: '{"seq":4}' }]);
    expect(rest).toBe('id: 5\nevent: hor');
  });

  test('CRLF, comments and multi-line data are handled', () => {
    const { frames } = parseSse(': keepalive\r\n\r\nevent: snapshot\r\ndata: {"a":\r\ndata: 1}\r\n\r\n');
    expect(frames).toEqual([{ id: null, event: 'snapshot', data: '{"a":\n1}' }]);
  });

  test('an unnamed frame is a "message"', () => {
    expect(parseSse('data: x\n\n').frames[0]?.event).toBe('message');
  });
});

describe('backoffMs', () => {
  test('grows exponentially and is capped', () => {
    const mid = () => 0.5; // no jitter
    expect(backoffMs(0, mid)).toBe(1000);
    expect(backoffMs(3, mid)).toBe(8000);
    expect(backoffMs(20, mid)).toBe(15000);
  });

  test('jitter stays within 20 %', () => {
    expect(backoffMs(2, () => 0)).toBe(3200);
    expect(backoffMs(2, () => 1)).toBe(4800);
  });
});

describe('resolveShortcut', () => {
  const body = document.body;

  test('each demo key maps to its action, case-insensitively', () => {
    expect(resolveShortcut({ key: 'i', target: body })).toBe('inject');
    expect(resolveShortcut({ key: 'K', target: body })).toBe('kill');
    expect(resolveShortcut({ key: 'r', target: body })).toBe('reset');
    expect(resolveShortcut({ key: 'a', target: body })).toBe('approve');
    expect(resolveShortcut({ key: 'd', target: body })).toBe('deny');
    expect(resolveShortcut({ key: '?', target: body })).toBe('help');
    expect(resolveShortcut({ key: 'x', target: body })).toBeNull();
  });

  test('never fires while typing, with a modifier, or on key repeat', () => {
    const input = document.createElement('input');
    const textarea = document.createElement('textarea');
    const editable = document.createElement('div');
    editable.setAttribute('contenteditable', 'true');
    for (const target of [input, textarea, editable]) {
      expect(isTypingTarget(target)).toBe(true);
      expect(resolveShortcut({ key: 'a', target })).toBeNull();
    }
    expect(resolveShortcut({ key: 'r', target: body, ctrlKey: true })).toBeNull();
    expect(resolveShortcut({ key: 'r', target: body, metaKey: true })).toBeNull();
    expect(resolveShortcut({ key: 'a', target: body, repeat: true })).toBeNull();
  });
});
