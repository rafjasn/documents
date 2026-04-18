'use client';

import { useState, useEffect, useCallback } from 'react';
import { api } from '@/lib/api';
import { Document, DocumentStatus } from '@/types';
import { useDocumentSocket } from './useDocumentSocket';

export function useDocuments() {
    const [documents, setDocuments] = useState<Document[]>([]);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [lastKey, setLastKey] = useState<string | undefined>();

    const onStatusChange = useCallback((event: any) => {
        setDocuments((prev) => {
            const idx = prev.findIndex((doc) => doc.id === event.documentId);

            if (idx === -1) {
                api.getDocument(event.documentId)
                    .then((fetched) =>
                        setDocuments((current) => {
                            if (current.some((d) => d.id === fetched.id)) {
                                return current.map((d) =>
                                    d.id === fetched.id
                                        ? { ...d, ...event.document, status: event.status }
                                        : d
                                );
                            }
                            return [{ ...fetched, status: event.status }, ...current];
                        })
                    )
                    .catch(() => {});
                return prev;
            }

            const updated = [...prev];
            updated[idx] = { ...prev[idx], ...event.document, status: event.status };
            return updated;
        });
    }, []);

    const { connected } = useDocumentSocket(onStatusChange);

    const fetchDocuments = useCallback(async () => {
        try {
            const all: Document[] = [];
            let cursor: string | undefined;

            do {
                const result = await api.listDocuments(undefined, 100, cursor);
                all.push(...result.items);
                cursor = result.lastKey;
            } while (cursor);

            setDocuments(all);
            setLastKey(undefined);
        } catch (err) {
            console.error('Failed to fetch documents:', err);
        } finally {
            setLoading(false);
            setRefreshing(false);
        }
    }, []);

    useEffect(() => {
        setLoading(true);
        fetchDocuments();
    }, [fetchDocuments]);

    const refresh = () => {
        setRefreshing(true);
        fetchDocuments();
    };

    return {
        documents,
        loading,
        refreshing,
        connected,
        lastKey,
        refresh
    };
}
