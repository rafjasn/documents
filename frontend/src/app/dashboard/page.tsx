'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/hooks/useAuth';
import { useDocuments } from '@/hooks/useDocuments';
import Documents from '@/components/dashboard/Documents';
import { DocumentMetrics } from '@/components/dashboard/DocumentMetrics';
import { Loader2 } from 'lucide-react';

export default function DashboardPage() {
    const { user, loading: authLoading } = useAuth();
    const router = useRouter();
    const { documents, loading, refreshing, connected, refresh } = useDocuments();

    useEffect(() => {
        if (!authLoading && !user) router.replace('/login');
    }, [user, authLoading, router]);

    if (authLoading || !user) {
        return (
            <div className="flex h-screen items-center justify-center">
                <Loader2 className="h-8 w-8 animate-spin text-brand-600" />
            </div>
        );
    }

    return (
        <div className="mx-auto max-w-6xl space-y-6 p-4">
            <DocumentMetrics documents={documents} />
            <Documents
                documents={documents}
                loading={loading}
                refreshing={refreshing}
                connected={connected}
                onRefresh={refresh}
            />
        </div>
    );
}
