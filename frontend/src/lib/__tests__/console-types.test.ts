import { describe, expect, test } from 'vitest';

import { type Availability, isAvailable } from '@/lib/console-types';

/**
 * The availability discriminator.
 *
 * `isAvailable` is what keeps "Aegis could not consult the topology graph" from
 * being rendered by the same code path as "this service has no dependencies".
 * Both of the assertions below are load-bearing at compile time as well as at
 * run time: if the type predicate stopped narrowing, `tsc` would fail on the
 * payload access, and every call site would quietly start accepting `unknown`.
 */

interface Topology {
  services: string[];
}

const present: Availability<Topology> = {
  available: true,
  reason: '',
  services: ['checkout', 'payments'],
};

const absent: Availability<Topology> = {
  available: false,
  reason: 'Neo4j did not answer within the traversal budget.',
};

describe('isAvailable', () => {
  test('the available branch exposes the payload', () => {
    if (!isAvailable(present)) throw new Error('a payload-bearing value must narrow to available');

    expect(present.services).toEqual(['checkout', 'payments']);
  });

  test('the unavailable branch exposes a reason and no payload', () => {
    // The reason is the whole point: an operator needs to know that the answer
    // is missing *and why*, so they can decide whether to trust the rest of the
    // page.
    if (isAvailable(absent)) throw new Error('an unavailable value must not narrow to available');

    expect(absent.reason).toContain('Neo4j');
    // @ts-expect-error the unavailable branch must never offer the payload, or
    // a caller could read an empty array out of a source that was never reached.
    expect(absent.services).toBeUndefined();
  });

  test('an unavailable value yields no payload rather than an empty one', () => {
    // The failure mode this guards: `const services = value.services ?? []`.
    // That line turns an unreachable graph into "no dependencies" and is
    // exactly what the discriminator exists to make impossible to write.
    // A caller-shaped helper, so the narrowing under test is the one a real
    // component performs on a value it was handed.
    const payload = (value: Availability<Topology>): string[] | null =>
      isAvailable(value) ? value.services : null;

    expect(payload(absent)).toBeNull();
    expect(payload(absent)).not.toEqual([]);
    expect(payload(present)).toEqual(['checkout', 'payments']);
  });
});
