// ESLint 平面配置（Vue3 + TS）：中型团队基线——错误级规则零容忍
import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist/', 'dist-console/', 'node_modules/', 'coverage/', 'playwright-report/'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  {
    files: ['**/*.vue'],
    languageOptions: { parserOptions: { parser: tseslint.parser } },
  },
  {
    // 有意的 v-html：MessageBubble 走 DOMPurify.sanitize 的 markdown；WalletModal 是本仓静态 locale 字面量（自带 <strong>/<span.highlight>，非用户输入）
    files: ['src/components/MessageBubble.vue', 'src/components/WalletModal.vue'],
    rules: { 'vue/no-v-html': 'off' },
  },
  {
    rules: {
      'no-console': ['warn', { allow: ['warn', 'error'] }],
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      'vue/multi-word-component-names': 'off',
    },
  },
)
