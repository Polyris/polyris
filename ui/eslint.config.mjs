import nextConfig from 'eslint-config-next/core-web-vitals';

/** @type {import('eslint').Linter.Config[]} */
export default [
  ...nextConfig,

  // Global ignores
  {
    ignores: [
      '.next/',
      'node_modules/',
      '*.config.js',
      '*.config.ts',
      '*.config.mjs',
      'public/',
    ],
  },

  // Project rules
  {
    files: ['src/**/*.{ts,tsx}'],
    rules: {
      // React Hooks v7 — new rules set to warn (fix incrementally)
      'react-hooks/purity': 'warn',
      'react-hooks/set-state-in-effect': 'warn',
      'react-hooks/refs': 'warn',
      'react-hooks/immutability': 'warn',
      'react-hooks/preserve-manual-memoization': 'warn',

      // TypeScript — relaxed for migration phase
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unused-vars': ['warn', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
      }],

      // Code quality
      'no-console': ['warn', { allow: ['warn', 'error', 'log'] }],

      // Best practices
      'no-debugger': 'warn',
      'prefer-const': 'warn',
      'eqeqeq': ['warn', 'always', { null: 'ignore' }],

      // React
      'react/react-in-jsx-scope': 'off',
      'react/prop-types': 'off',
    },
  },

  // shadcn/ui generated files — relaxed rules
  {
    files: ['src/components/ui/**/*.tsx'],
    rules: {
      '@typescript-eslint/ban-ts-comment': 'off',
      '@typescript-eslint/no-empty-object-type': 'off',
    },
  },

  // Tests legitimately use `any` for mocks/fixtures.
  {
    files: ['src/**/*.test.{ts,tsx}'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
];
