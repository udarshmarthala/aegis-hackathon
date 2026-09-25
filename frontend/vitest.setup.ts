import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

/**
 * Unmount everything between tests.
 *
 * Testing Library renders into a container appended to `document.body`. Without
 * this, a query like `getByText` sees the previous test's DOM as well as its
 * own, and an assertion that two components are textually distinguishable would
 * pass or fail depending on the order the files happened to run in.
 */
afterEach(() => {
  cleanup();
  window.localStorage.clear();
});
