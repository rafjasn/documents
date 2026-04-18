import type { Metadata } from 'next';
import { AuthProvider } from '@/hooks/useAuth';

export const metadata: Metadata = {
    title: 'Document Processing',
    description: 'Upload, process, and classify your documents automatically'
};

import { Outfit } from 'next/font/google';
import './globals.css';
import 'flatpickr/dist/flatpickr.css';
import { SidebarProvider } from '@/context/SidebarContext';
import { ThemeProvider } from '@/context/ThemeContext';

const outfit = Outfit({
    subsets: ['latin']
});

export default function RootLayout({
    children
}: Readonly<{
    children: React.ReactNode;
}>) {
    return (
        <html lang="en">
            <body className={`${outfit.className} dark:bg-gray-900`}>
                <ThemeProvider>
                    <AuthProvider>
                        <SidebarProvider>{children}</SidebarProvider>
                    </AuthProvider>
                </ThemeProvider>
            </body>
        </html>
    );
}
