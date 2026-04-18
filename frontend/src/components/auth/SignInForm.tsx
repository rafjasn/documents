'use client';

import React, { useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/hooks/useAuth';
import { ApiError } from '@/lib/api';
import { ArrowRight, Loader2, Eye, EyeClosed } from 'lucide-react';
import Input from '@/components/form/input/InputField';
import Label from '@/components/form/Label';
import Button from '@/components/ui/button/Button';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const PASSWORD_MIN = 8;

export default function SignInForm() {
    const [showPassword, setShowPassword] = useState(false);
    const [isRegister, setIsRegister] = useState(false);
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [fieldErrors, setFieldErrors] = useState<{ email?: string; password?: string }>({});
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);
    const { login, register } = useAuth();
    const router = useRouter();

    const validate = (): { email?: string; password?: string } => {
        const errors: { email?: string; password?: string } = {};

        if (!email.trim()) {
            errors.email = 'Email is required.';
        } else if (!EMAIL_RE.test(email)) {
            errors.email = 'Enter a valid email address.';
        }

        if (!password) {
            errors.password = 'Password is required.';
        } else if (isRegister && password.length < PASSWORD_MIN) {
            errors.password = `Password must be at least ${PASSWORD_MIN} characters.`;
        }

        return errors;
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError('');

        const errors = validate();
        setFieldErrors(errors);

        if (Object.keys(errors).length > 0) {
            return;
        }

        setLoading(true);

        try {
            if (isRegister) {
                await register(email, password);
            } else {
                await login(email, password);
            }
            router.replace('/dashboard');
        } catch (err) {
            if (err instanceof ApiError) {
                setError(err.message);
            } else {
                setError('Something went wrong.');
            }
        } finally {
            setLoading(false);
        }
    };

    return (
        <div className="flex flex-col flex-1 lg:w-1/2 w-full">
            <div className="flex flex-col justify-center flex-1 w-full max-w-md mx-auto">
                <div>
                    <div className="mb-5 sm:mb-8">
                        <h1 className="mb-2 font-semibold text-gray-800 text-title-sm dark:text-white/90 sm:text-title-md">
                            {isRegister ? 'Create account' : 'Log In'}
                        </h1>
                        <p className="text-sm text-gray-500 dark:text-gray-400">
                            {isRegister
                                ? 'Enter your email and password to sign up!'
                                : 'Enter your email and password to sign in!'}
                        </p>
                    </div>
                    <div>
                        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 sm:gap-5"></div>

                        <form onSubmit={handleSubmit}>
                            {error && (
                                <div className="rounded-lg bg-red-50 px-4 py-3 text-sm text-red-600 ring-1 ring-red-200">
                                    {error}
                                </div>
                            )}
                            <div className="space-y-6">
                                <div>
                                    <Label>
                                        Email <span className="text-error-500">*</span>{' '}
                                    </Label>
                                    <Input
                                        placeholder="info@gmail.com"
                                        type="email"
                                        value={email}
                                        onChange={(e) => setEmail(e.target.value)}
                                        className="input"
                                        required
                                        autoComplete="email"
                                        error={!!fieldErrors.email}
                                        hint={fieldErrors.email}
                                    />
                                </div>
                                <div>
                                    <Label>
                                        Password <span className="text-error-500">*</span>{' '}
                                    </Label>
                                    <div className="relative">
                                        <Input
                                            type={showPassword ? 'text' : 'password'}
                                            value={password}
                                            onChange={(e) => setPassword(e.target.value)}
                                            className="input"
                                            placeholder={
                                                isRegister ? 'Min 8 characters' : '••••••••'
                                            }
                                            required
                                            minLength={isRegister ? PASSWORD_MIN : undefined}
                                            autoComplete={
                                                isRegister ? 'new-password' : 'current-password'
                                            }
                                            error={!!fieldErrors.password}
                                            hint={fieldErrors.password}
                                        />
                                        <span
                                            onClick={() => setShowPassword(!showPassword)}
                                            className="absolute z-30 -translate-y-1/2 cursor-pointer right-4 top-1/2 text-sm text-gray-400 dark:text-gray-300"
                                        >
                                            {showPassword ? <Eye /> : <EyeClosed />}
                                        </span>
                                    </div>
                                </div>
                                <div>
                                    <Button disabled={loading} className="btn-primary w-full">
                                        {loading ? (
                                            <Loader2 size={18} className="animate-spin" />
                                        ) : (
                                            <>
                                                {isRegister ? 'Create account' : 'Sign in'}
                                                <ArrowRight size={16} />
                                            </>
                                        )}
                                    </Button>
                                </div>
                            </div>
                        </form>

                        <div className="mt-5">
                            <p className="text-sm font-normal text-center text-gray-700 dark:text-gray-400 sm:text-start">
                                {isRegister ? 'Already have an account?' : "Don't have an account?"}{' '}
                                <button
                                    type="button"
                                    onClick={() => {
                                        setIsRegister(!isRegister);
                                        setError('');
                                        setFieldErrors({});
                                    }}
                                    className="text-brand-500 hover:text-brand-600 dark:text-brand-400"
                                >
                                    {isRegister ? 'Sign in' : 'Sign up'}
                                </button>
                            </p>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
