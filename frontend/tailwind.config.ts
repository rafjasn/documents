import type { Config } from 'tailwindcss';

const config: Config = {
    content: ['./src/**/*.{js,ts,jsx,tsx,mdx}'],
    theme: {
        extend: {
            colors: {
                brand: {
                    50: '#f0f4ff',
                    100: '#dbe4ff',
                    200: '#bac8ff',
                    300: '#91a7ff',
                    400: '#748ffc',
                    500: '#5c7cfa',
                    600: '#4c6ef5',
                    700: '#4263eb',
                    800: '#3b5bdb',
                    900: '#364fc7',
                    950: '#1e3a8a'
                },
                surface: {
                    0: '#ffffff',
                    1: '#f8f9fc',
                    2: '#f1f3f9',
                    3: '#e4e8f1',
                    4: '#d1d6e3'
                },
                ink: {
                    0: '#0f1729',
                    1: '#2d3548',
                    2: '#525b6e',
                    3: '#7c8496',
                    4: '#a6adb9'
                },
                status: {
                    pending: '#f59e0b',
                    processing: '#6366f1',
                    completed: '#10b981',
                    failed: '#ef4444'
                }
            },
            fontFamily: {
                sans: ['var(--font-geist-sans)', 'system-ui', 'sans-serif'],
                mono: ['var(--font-geist-mono)', 'ui-monospace', 'monospace']
            },
            animation: {
                'fade-in': 'fadeIn 0.3s ease-out',
                'slide-up': 'slideUp 0.4s ease-out',
                'pulse-slow': 'pulse 3s ease-in-out infinite'
            },
            keyframes: {
                fadeIn: {
                    '0%': { opacity: '0' },
                    '100%': { opacity: '1' }
                },
                slideUp: {
                    '0%': { opacity: '0', transform: 'translateY(12px)' },
                    '100%': { opacity: '1', transform: 'translateY(0)' }
                }
            }
        }
    },
    plugins: []
};

export default config;
