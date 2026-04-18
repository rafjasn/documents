'use client';

import { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import { api } from '@/lib/api';
import { User } from '@/types';

interface AuthContextType {
    user: User | null;
    loading: boolean;
    login: (email: string, password: string) => Promise<void>;
    register: (email: string, password: string) => Promise<void>;
    logout: () => void;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
    const [user, setUser] = useState<User | null>(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        const token = api.getToken();
        const stored =
            typeof window !== 'undefined' ? localStorage.getItem('documents_user') : null;

        if (token && stored) {
            try {
                setUser(JSON.parse(stored));
            } catch {
                api.clearTokens();
            }
        }

        setLoading(false);
    }, []);

    const login = async (email: string, password: string) => {
        const res = await api.login(email, password);
        api.setToken(res.accessToken);

        if (res.refreshToken) {
            api.setRefreshToken(res.refreshToken);
        }

        const u: User = { userId: res.userId, email: res.email, accessToken: res.accessToken };
        setUser(u);
        localStorage.setItem('documents_user', JSON.stringify(u));
    };

    const register = async (email: string, password: string) => {
        const res = await api.register(email, password);
        api.setToken(res.accessToken);

        if (res.refreshToken) {
            api.setRefreshToken(res.refreshToken);
        }

        const u: User = { userId: res.userId, email: res.email, accessToken: res.accessToken };
        setUser(u);
        localStorage.setItem('documents_user', JSON.stringify(u));
    };

    const logout = () => {
        api.clearTokens();
        setUser(null);
    };

    return (
        <AuthContext.Provider value={{ user, loading, login, register, logout }}>
            {children}
        </AuthContext.Provider>
    );
}

export function useAuth() {
    const ctx = useContext(AuthContext);
    if (!ctx) throw new Error('useAuth must be used within AuthProvider');
    return ctx;
}
