import { render, screen } from '@testing-library/react';
import { describe, expect, test, vi } from 'vitest';

import { ApiError, NetworkError } from '@/lib/api';
import { EmptyState, ErrorState, QueryFailure, SourceUnavailableState } from '@/components/ui/states';

/**
 * The UI half of invariant 6: "no evidence found" is not "source unavailable".
 *
 * Backend, API and types all keep the two states distinct; the guarantee dies
 * here if someone decides the two components look similar enough to merge, or
 * reaches for `EmptyState` when a fetch fails because it is the nearer import.
 * These tests are what makes that a failing build rather than a review comment.
 */

describe('SourceUnavailableState', () => {
  test('the reason a source could not be reached is shown, not swallowed', () => {
    // Without the reason the operator cannot tell a credential problem from an
    // outage, and so cannot tell whether the rest of the page is trustworthy.
    render(
      <SourceUnavailableState
        source="Prometheus"
        reason="The query timed out after 20 seconds."
        consequence="Latency claims could not be verified; confidence is reduced."
      />,
    );

    expect(screen.getByText('Prometheus unavailable')).toBeInTheDocument();
    expect(screen.getByText('The query timed out after 20 seconds.')).toBeInTheDocument();
    expect(
      screen.getByText('Latency claims could not be verified; confidence is reduced.'),
    ).toBeInTheDocument();
  });

  test('an unavailable source is textually distinguishable from an empty result', () => {
    // The assertion is deliberately about the words on screen rather than the
    // markup: an operator scanning a page distinguishes these by reading them.
    const unavailable = render(
      <SourceUnavailableState
        source="Neo4j"
        reason="The topology store did not answer."
        consequence="Blast radius is unknown."
      />,
    );
    const unavailableText = unavailable.container.textContent ?? '';
    unavailable.unmount();

    const empty = render(
      <EmptyState title="No dependencies recorded for this service." detail="The graph was consulted." />,
    );
    const emptyText = empty.container.textContent ?? '';

    expect(unavailableText).not.toBe(emptyText);
    expect(unavailableText).toMatch(/unavailable/i);
    expect(emptyText).not.toMatch(/unavailable/i);
    // The reverse direction matters just as much: an unavailable source must
    // never claim there was nothing to find.
    expect(unavailableText).not.toMatch(/\bno dependencies\b/i);
  });

  test('retry is offered only when a caller supplies a way to retry', () => {
    // A dead Retry button on an unreachable source teaches operators to
    // distrust every control on the page.
    const { container, unmount } = render(
      <SourceUnavailableState source="Loki" reason="Connection refused." consequence="Log evidence is missing." />,
    );
    expect(container.querySelector('button')).toBeNull();
    unmount();

    const onRetry = vi.fn();
    render(
      <SourceUnavailableState
        source="Loki"
        reason="Connection refused."
        consequence="Log evidence is missing."
        onRetry={onRetry}
      />,
    );
    screen.getByRole('button', { name: /retry/i }).click();

    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe('EmptyState', () => {
  test('an empty result states what was searched rather than "No data."', () => {
    // The UX spec rejects bare "No data." precisely because it does not say
    // whether the search happened.
    render(
      <EmptyState
        title="No incidents match these filters."
        detail="All sources were reachable."
        hint="widening the time window"
      />,
    );

    expect(screen.getByText('No incidents match these filters.')).toBeInTheDocument();
    expect(screen.getByText('All sources were reachable.')).toBeInTheDocument();
    expect(screen.getByText(/widening the time window/)).toBeInTheDocument();
  });
});

describe('ErrorState', () => {
  test('a failure announces itself to assistive technology', () => {
    // An operator using a screen reader must not have to poll the page to
    // discover that the request failed.
    render(<ErrorState title="Could not load incidents." detail="Aegis is unreachable." />);

    expect(screen.getByRole('alert')).toHaveTextContent('Could not load incidents.');
  });
});

describe('QueryFailure', () => {
  const props = {
    title: 'Cannot load the diagnosis',
    source: 'Diagnosis',
    consequence: 'Whether Aegis reached a conclusion is unknown.',
  };

  test('an unreachable backend is a source that could not be consulted, not a failed request', () => {
    // A NetworkError means the request never arrived. Rendering it as a generic
    // error tells the operator Aegis answered and refused, which is the one
    // reading that would let them treat a blank panel as a finding.
    const { container } = render(
      <QueryFailure {...props} error={new NetworkError('Aegis is unreachable.')} />,
    );

    expect(screen.getByText('Diagnosis unavailable')).toBeInTheDocument();
    expect(screen.getByText('Aegis is unreachable.')).toBeInTheDocument();
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(screen.queryByText('Cannot load the diagnosis')).toBeNull();
  });

  test('a backend that reports its own dependency down is also "could not look"', () => {
    // SOURCE_UNAVAILABLE is Aegis saying it could not see, not that it saw
    // nothing. The HTTP status is incidental; the code is the claim.
    render(
      <QueryFailure
        {...props}
        error={new ApiError(503, 'SOURCE_UNAVAILABLE', 'Prometheus did not answer.')}
      />,
    );

    expect(screen.getByText('Diagnosis unavailable')).toBeInTheDocument();
    expect(screen.getByText('Prometheus did not answer.')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  test('an open circuit is reported as an outage, because that is what it is', () => {
    // Aegis stopped calling a source on purpose. The panel is empty for the
    // same reason as an outage, so it must not read as a healthy empty result.
    render(
      <QueryFailure
        {...props}
        error={new ApiError(503, 'CIRCUIT_OPEN', 'The circuit to Loki is open.')}
        unavailableConsequence="Log evidence is missing while the circuit is open."
      />,
    );

    expect(screen.getByText('Diagnosis unavailable')).toBeInTheDocument();
    expect(
      screen.getByText('Log evidence is missing while the circuit is open.'),
    ).toBeInTheDocument();
  });

  test('a genuine failure keeps the error treatment and quotes its identifiers', () => {
    // A 500 is Aegis answering and failing. Status, code and correlation id are
    // what an operator hands to whoever reads the logs, so they go on screen.
    render(
      <QueryFailure
        {...props}
        error={new ApiError(500, 'INTERNAL', 'Diagnosis assembly failed.', 'corr-9f2')}
      />,
    );

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Cannot load the diagnosis');
    expect(alert).toHaveTextContent('INTERNAL');
    expect(alert).toHaveTextContent('500');
    expect(alert).toHaveTextContent('corr-9f2');
    expect(screen.queryByText(/unavailable$/)).toBeNull();
  });

  test('an error of no known shape is an error, never a silent outage', () => {
    // Fail closed on the rendering side too: an unrecognised throw must not
    // inherit the treatment that tells the operator confidence was reduced.
    render(<QueryFailure {...props} error="something threw a string" />);

    expect(screen.getByRole('alert')).toHaveTextContent('something threw a string');
    expect(screen.queryByText('Diagnosis unavailable')).toBeNull();
  });

  test('retry is wired through to whichever treatment is rendered', () => {
    // The two branches render different components; a retry that only works on
    // one of them fails exactly when the source is down and retrying matters.
    const onRetry = vi.fn();
    const { unmount } = render(
      <QueryFailure {...props} error={new NetworkError('Connection refused.')} onRetry={onRetry} />,
    );
    screen.getByRole('button', { name: /retry/i }).click();
    unmount();

    render(
      <QueryFailure
        {...props}
        error={new ApiError(500, 'INTERNAL', 'Assembly failed.')}
        onRetry={onRetry}
      />,
    );
    screen.getByRole('button', { name: /retry/i }).click();

    expect(onRetry).toHaveBeenCalledTimes(2);
  });
});
