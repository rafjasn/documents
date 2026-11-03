import { expect, Page } from '@playwright/test';

/**
 * Sign in on /login and wait for the library. On a fresh stack the dev server compiles the
 * page on its first visit, and React's hydration can clear a field typed into before it
 * (Stage 11's fresh-clone check met this), so a sign-in that doesn't reach the library is
 * tried again.
 */
export async function signIn(page: Page, email: string, password: string): Promise<void> {
    await page.goto('/login');
    await expect(async () => {
        await page.getByPlaceholder('info@gmail.com').fill(email);
        await page.locator('input[autocomplete="current-password"]').fill(password);
        await page.getByRole('button', { name: 'Sign in' }).click();
        await page.waitForURL('**/dashboard', { timeout: 10_000 });
    }).toPass({ timeout: 90_000 });
}
