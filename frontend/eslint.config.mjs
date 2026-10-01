import {defineConfig} from 'eslint/config';
import astro from 'eslint-plugin-astro';
import ts from 'typescript-eslint';

export default defineConfig(
  {ignores: ['dist/**', '.astro/**', 'src/lib/api.generated.ts']},
  ...ts.configs.recommended,
  ...astro.configs.recommended,
  {files: ['scripts/*.cjs'], rules: {'@typescript-eslint/no-require-imports': 'off'}},
);
