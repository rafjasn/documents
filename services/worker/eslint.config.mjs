import eslint from '@eslint/js';
import tseslint from 'typescript-eslint';
import prettier from 'eslint-config-prettier';
import prettierPlugin from 'eslint-plugin-prettier';
import globals from 'globals';

export default tseslint.config(
    eslint.configs.recommended,
    ...tseslint.configs.recommended,
    prettier,
    {
        plugins: { prettier: prettierPlugin },
        languageOptions: {
            globals: globals.node
        },
        rules: {
            'prettier/prettier': 'error',
            'no-empty': ['error', { allowEmptyCatch: true }],
            '@typescript-eslint/no-explicit-any': 'warn',
            '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
            '@typescript-eslint/no-empty-function': 'warn'
        }
    },
    {
        ignores: ['dist/**', 'coverage/**']
    }
);
