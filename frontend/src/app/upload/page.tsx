'use client';

import { useState, useCallback, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/hooks/useAuth';
import { api, ApiError } from '@/lib/api';
import PageBreadcrumb from '@/components/common/PageBreadCrumb';
import DocumentDropzone from '@/components/upload/DocumentDropzone';
import DocumentList, { UploadItem } from '@/components/upload/DocumentList';

const ACCEPTED_TYPES = [
    'application/pdf',
    'image/png',
    'image/jpeg',
    'image/webp',
    'text/plain',
    'text/csv',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
];

const MAX_SIZE_MB = 50;

export default function UploadPage() {
    const { user, loading: authLoading } = useAuth();
    const router = useRouter();
    const [items, setItems] = useState<UploadItem[]>([]);
    const [uploading, setUploading] = useState(false);

    useEffect(() => {
        if (!authLoading && !user) router.replace('/login');
    }, [user, authLoading, router]);

    const addFiles = useCallback((files: File[]) => {
        const newItems: UploadItem[] = files
            .filter((file) => {
                if (!ACCEPTED_TYPES.includes(file.type)) {
                    alert(`Unsupported file type: ${file.type}`);
                    return false;
                }

                if (file.size > MAX_SIZE_MB * 1024 * 1024) {
                    alert(`File too large: ${file.name} (max ${MAX_SIZE_MB}MB)`);
                    return false;
                }

                return true;
            })
            .map((file) => ({
                id: crypto.randomUUID(),
                file,
                displayName: file.name,
                status: 'queued' as const,
                progress: 0
            }));

        setItems((prev) => [...prev, ...newItems]);
    }, []);

    const removeItem = (id: string) => {
        setItems((prev) => prev.filter((item) => item.id !== id));
    };

    const updateItemName = (id: string, name: string) => {
        setItems((prev) =>
            prev.map((item) => (item.id === id ? { ...item, displayName: name } : item))
        );
    };

    const handleUploadAll = async () => {
        const queued = items.filter((i) => i.status === 'queued');
        if (queued.length === 0) return;

        setUploading(true);

        for (const item of queued) {
            setItems((prev) =>
                prev.map((i) => (i.id === item.id ? { ...i, status: 'uploading', progress: 0 } : i))
            );

            try {
                const { documentId, uploadUrl, fields } = await api.presignUpload(
                    item.file.name,
                    item.file.type,
                    item.file.size,
                    item.displayName !== item.file.name ? item.displayName : undefined
                );

                await api.uploadToS3(uploadUrl, fields, item.file, (progress) => {
                    setItems((prev) =>
                        prev.map((i) => (i.id === item.id ? { ...i, progress } : i))
                    );
                });

                setItems((prev) =>
                    prev.map((i) =>
                        i.id === item.id ? { ...i, status: 'done', progress: 100, documentId } : i
                    )
                );
            } catch (err) {
                const message = err instanceof ApiError ? err.message : 'Upload failed';
                setItems((prev) =>
                    prev.map((i) =>
                        i.id === item.id
                            ? { ...i, status: 'error', progress: 0, error: message }
                            : i
                    )
                );
            }
        }

        setUploading(false);
    };

    if (authLoading || !user) return null;

    return (
        <div className="mx-auto max-w-3xl">
            <PageBreadcrumb pageTitle="Upload documents" />

            <DocumentDropzone onFiles={addFiles} accept={ACCEPTED_TYPES} maxSizeMb={MAX_SIZE_MB} />

            <DocumentList
                items={items}
                uploading={uploading}
                onRemoveItem={removeItem}
                onUpdateItemName={updateItemName}
                onUploadAll={handleUploadAll}
            />
        </div>
    );
}
