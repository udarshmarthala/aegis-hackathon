import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { FlatCompat } from '@eslint/eslintrc';

const compat = new FlatCompat({
  baseDirectory: dirname(fileURLToPath(import.meta.url)),
});

/**
 * ESLint configuration.
 *
 * The repository had none, which meant `npm run lint` dropped into Next's
 * interactive setup prompt and therefore never ran - not locally, and not in
 * CI. A lint script that cannot execute is worse than no script at all,
 * because a green pipeline implies it passed.
 *
 * Beyond the Next presets, only rules that encode a real invariant of this
 * codebase are enabled. A rule nobody agrees with gets disabled in-line
 * everywhere, and then the config stops meaning anything.
 */
const config = [
  { ignores: ['.next/**', 'node_modules/**', 'next-env.d.ts', 'out/**'] },
  ...compat.extends('next/core-web-vitals', 'next/typescript'),
  {
    rules: {
      // `any` erases the contract between the console and the API. The typed
      // client exists precisely so a response shape cannot drift unnoticed.
      '@typescript-eslint/no-explicit-any': 'error',
      // An unused import is usually residue from a half-finished refactor. The
      // underscore escape hatch keeps deliberately-ignored parameters legal.
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
        },
      ],
      // Console noise in a production build buries the messages that matter
      // during an incident. warn and error are kept deliberately.
      'no-console': ['error', { allow: ['warn', 'error'] }],
    },
  },
];

export default config;
