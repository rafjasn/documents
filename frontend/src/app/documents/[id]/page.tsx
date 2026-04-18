'use client';

import { useState, useEffect, useCallback } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { useAuth } from '@/hooks/useAuth';
import { useDocumentSocket } from '@/hooks/useDocumentSocket';
import { api } from '@/lib/api';
import { Document } from '@/types';
import DocumentDetail from '@/components/documents/DocumentDetail';

export default function DocumentDetailPage() {
    const { id } = useParams<{ id: string }>();
    const { user, loading: authLoading } = useAuth();
    const router = useRouter();
    const [doc, setDoc] = useState<Document | null>(null);
    const [loading, setLoading] = useState(true);
    const [deleting, setDeleting] = useState(false);

    const onStatusChange = useCallback(
        (event: any) => {
            if (event.documentId !== id) return;

            if (event.document?.thumbnailKey) {
                api.getDocument(id)
                    .then(setDoc)
                    .catch(() => {});
                return;
            }

            setDoc((prev) => (prev ? { ...prev, ...event.document, status: event.status } : prev));
        },
        [id]
    );
    useDocumentSocket(onStatusChange);

    useEffect(() => {
        if (!authLoading && !user) router.replace('/login');
    }, [user, authLoading, router]);

    useEffect(() => {
        if (!user || !id) return;
        setLoading(true);
        api.getDocument(id)
            .then(setDoc)
            .catch(() => router.replace('/dashboard'))
            .finally(() => setLoading(false));
    }, [user, id, router]);

    const handleDownload = async () => {
        if (!doc) return;
        try {
            const { downloadUrl } = await api.getDownloadUrl(doc.id);
            window.open(downloadUrl, '_blank');
        } catch (err) {
            console.error('Download failed:', err);
        }
    };

    const handleDelete = async () => {
        if (!doc || !confirm('Delete this document? This cannot be undone.')) return;
        setDeleting(true);
        try {
            await api.deleteDocument(doc.id);
            router.replace('/dashboard');
        } catch (err) {
            console.error('Delete failed:', err);
            setDeleting(false);
        }
    };

    if (authLoading || !user) return null;

    if (loading) {
        return (
            <div className="mx-auto max-w-4xl">
                <div className="mb-4 h-6 w-32 animate-pulse rounded-lg bg-gray-200 dark:bg-gray-800" />
                <div className="overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03] p-6">
                    <div className="mb-6 h-8 w-64 animate-pulse rounded-lg bg-gray-200 dark:bg-gray-800" />
                    <div className="space-y-3">
                        <div className="h-4 w-full animate-pulse rounded-lg bg-gray-200 dark:bg-gray-800" />
                        <div className="h-4 w-3/4 animate-pulse rounded-lg bg-gray-200 dark:bg-gray-800" />
                        <div className="h-4 w-1/2 animate-pulse rounded-lg bg-gray-200 dark:bg-gray-800" />
                    </div>
                </div>
            </div>
        );
    }

    if (!doc) return null;

    return (
        <DocumentDetail
            doc={doc}
            deleting={deleting}
            onDownload={handleDownload}
            onDelete={handleDelete}
        />
    );
}
