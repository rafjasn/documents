import { defineConfig, devices } from '@playwright/test';

/**
 * The docs/DEMO.md walkthrough against a running stack (docker compose up, the demo
 * account seeded). Not part of CI: it needs the full stack and makes Gemini calls.
 * DEMO_URL points it elsewhere, e.g. the AWS deployment.
 */
export default defineConfig({
    testDir: './e2e',
    outputDir: './e2e/.results',
    timeout: 15 * 60 * 1000,
    // The dev server compiles each page on its first visit, which took over 30 s on a fresh
    // stack (Stage 11); the waits end as soon as their condition holds.
    expect: { timeout: 90_000 },
    workers: 1,
    reporter: [['list']],
    use: {
        ...devices['Desktop Chrome'],
        baseURL: process.env.DEMO_URL ?? 'http://localhost',
        // Wide enough for the chat page's sources column.
        viewport: { width: 1600, height: 1000 },
        actionTimeout: 90_000,
        navigationTimeout: 120_000,
        trace: 'retain-on-failure',
        screenshot: 'only-on-failure'
    },
    projects: [
        { name: 'demo', testMatch: 'demo.spec.ts' },
        // The README's GIF (e2e/recording.spec.ts): run on its own, with `e2e:record`.
        { name: 'recording', testMatch: 'recording.spec.ts' }
    ]
});
