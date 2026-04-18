'use client';

import { useEffect, useRef, useCallback, useState } from 'react';
import { io, Socket } from 'socket.io-client';
import { api } from '@/lib/api';
import { DocumentStatus } from '@/types';

interface StatusEvent {
    documentId: string;
    status: DocumentStatus;
    document?: any;
    timestamp: string;
}

type StatusCallback = (event: StatusEvent) => void;

export function useDocumentSocket(onStatusChange: StatusCallback) {
    const socketRef = useRef<Socket | null>(null);
    const [connected, setConnected] = useState(false);

    useEffect(() => {
        const token = api.getToken();
        if (!token) return;

        const wsUrl = process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:3001';

        const socket = io(`${wsUrl}/documents`, {
            auth: { token },
            transports: ['websocket', 'polling'],
            reconnection: true,
            reconnectionAttempts: 10,
            reconnectionDelay: 2000
        });

        socket.on('connect', () => {
            setConnected(true);
        });

        socket.on('disconnect', () => {
            setConnected(false);
        });

        socket.on('document:status', (event: StatusEvent) => {
            onStatusChange(event);
        });

        socketRef.current = socket;

        return () => {
            socket.disconnect();
            socketRef.current = null;
        };
    }, [onStatusChange]);

    return { connected };
}

export function useDocumentPolling(
    documentIds: string[],
    onUpdate: (id: string, doc: any) => void,
    intervalMs = 3000
) {
    const [polling, setPolling] = useState(false);

    useEffect(() => {
        if (documentIds.length === 0) return;

        setPolling(true);
        const interval = setInterval(async () => {
            for (const id of documentIds) {
                try {
                    const doc = await api.getDocument(id);
                    onUpdate(id, doc);
                } catch {
                    // ignore polling errors
                }
            }
        }, intervalMs);

        return () => {
            clearInterval(interval);
            setPolling(false);
        };
    }, [documentIds.join(','), intervalMs]);

    return { polling };
}
