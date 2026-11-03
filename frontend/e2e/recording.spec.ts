import { Browser, BrowserContext, expect, Locator, Page, test } from '@playwright/test';
import { signIn } from './sign-in';

type State = Awaited<ReturnType<BrowserContext['storageState']>>;

// Records the README's demo GIF (docs/media/demo.gif) on the seeded demo account: a Polish
// invoice's extracted fields, then library chat: a Polish question answered by comparing
// invoices with a contract (computed in code, with the agent's steps), and a date read
// from a PAID stamp, with the cited page and the stamp highlighted.
// `npm run e2e:record -w @documents/frontend` writes e2e/.results/demo.webm; docs/DEMO.md
// has the command that turns it into the GIF. It asks 2 questions.
const EMAIL = process.env.DEMO_EMAIL ?? 'demo@documents.local';
const PASSWORD = process.env.DEMO_PASSWORD ?? 'demo-password';
// The size the chat page is laid out for (playwright.config.ts).
const SIZE = { width: 1600, height: 1000 };
const VIDEO = 'e2e/.results/demo.webm';
const INVOICE = /Faktura VAT FV\/2026\/07\/123/;
const QUESTIONS = [
    'Czy łączna wartość netto faktur wystawionych przez Herba Usługi Sp. z o.o. w 2026 r. przekroczyła limit wartości umowy?',
    "On what date was invoice INV-260082 from O'Donnell Consulting Limited paid?"
];

/** Type a question the way a person would and wait for the finished answer. */
async function ask(page: Page, question: string): Promise<Locator> {
    const answers = page.getByTestId('answer');
    const before = await answers.count();
    await page.getByTestId('chat-input').pressSequentially(question, { delay: 20 });
    await page.getByTestId('chat-send').click();
    const answer = answers.nth(before);
    await expect(answer).toHaveAttribute('data-state', 'done', { timeout: 120_000 });
    return answer;
}

/** Remove the recording's conversations, so the walkthrough's deletions stay unambiguous. */
async function cleanUp(browser: Browser, state: State) {
    const context = await browser.newContext({ storageState: state, viewport: SIZE });
    const page = await context.newPage();
    page.on('dialog', (dialog) => dialog.accept());
    await page.goto('/chat');
    const item = page
        .getByTestId('conversation-list')
        .locator('li')
        .filter({ hasText: QUESTIONS[0].slice(0, 30) });
    // The list loads after the page; none may exist if the recording failed early.
    await item
        .first()
        .waitFor({ timeout: 15_000 })
        .catch(() => undefined);
    while ((await item.count()) > 0) {
        const count = await item.count();
        await item.first().hover();
        await item.first().getByRole('button', { name: 'Delete conversation' }).click();
        await expect(item).toHaveCount(count - 1);
    }
    await context.close();
}

test('record the demo GIF', async ({ browser }) => {
    // Sign in off camera; the recording starts on the library.
    const setup = await browser.newContext({ viewport: SIZE });
    await signIn(await setup.newPage(), EMAIL, PASSWORD);
    const state = await setup.storageState();
    await setup.close();

    const context = await browser.newContext({
        storageState: state,
        viewport: SIZE,
        recordVideo: { dir: 'e2e/.results/video', size: SIZE }
    });
    // Hide the Next.js development badge.
    await context.addInitScript(() => {
        document.addEventListener('DOMContentLoaded', () => {
            const style = document.createElement('style');
            style.textContent = 'nextjs-portal { display: none !important; }';
            document.head.appendChild(style);
        });
    });
    const page = await context.newPage();
    try {
        await page.goto('/dashboard');
        await expect(page.getByRole('link', { name: INVOICE })).toBeVisible({ timeout: 60_000 });
        await page.waitForTimeout(1500);

        await page.getByRole('link', { name: INVOICE }).click();
        await expect(page.getByTestId('extracted-fields')).toContainText('FV/2026/07/123');
        await page.waitForTimeout(1500);
        await page.getByTestId('extracted-fields').scrollIntoViewIfNeeded();
        await page.waitForTimeout(2500);

        await page.goto('/chat');
        await page.getByTestId('new-chat').click();
        const cap = await ask(page, QUESTIONS[0]);
        await page.waitForTimeout(1000);
        await cap.getByRole('button', { name: /model call/ }).click();
        await page.waitForTimeout(3000);

        const paid = await ask(page, QUESTIONS[1]);
        await page.waitForTimeout(1000);
        await paid
            .getByTestId('citation-chip')
            .filter({ hasText: /INV-260082.*p\. 1/ })
            .first()
            .click();
        const preview = page.getByTestId('page-preview').filter({ visible: true });
        await expect(preview).toHaveAttribute('data-rendered', 'true', { timeout: 60_000 });
        await expect(preview.getByTestId('page-highlight')).toBeVisible();
        await page.waitForTimeout(4000);

        await page.close();
        await page.video()?.saveAs(VIDEO);
    } finally {
        await context.close();
        await cleanUp(browser, state);
    }
});
