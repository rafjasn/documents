'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import { ChatIntent, ChatStreamEvent, ChatUsage, Citation, Conversation } from '@/types';

export interface ChatStep {
    node: string;
    label: string;
    done: boolean;
}

export interface ChatMessage {
    key: string;
    role: 'user' | 'assistant';
    content: string;
    citations: Citation[];
    intent?: ChatIntent;
    usage?: ChatUsage;
    /** The agent's steps, as they streamed; stored messages have none. */
    steps: ChatStep[];
    state: 'streaming' | 'done' | 'error';
    seconds?: number;
}

const FAILED = 'Sorry, something went wrong. Please try again.';

/** Apply one streamed event to the answer being written. */
function applyEvent(message: ChatMessage, event: ChatStreamEvent, started: number): ChatMessage {
    switch (event.type) {
        case 'step':
            return {
                ...message,
                steps: [
                    ...message.steps.map((step) => ({ ...step, done: true })),
                    { node: event.node, label: event.label, done: false }
                ]
            };
        case 'token':
            return { ...message, content: message.content + event.text };
        case 'reset':
            // The answer is being regenerated after the grounding check.
            return { ...message, content: '', citations: [] };
        case 'citations':
            return { ...message, citations: event.citations };
        case 'done':
            return {
                ...message,
                steps: message.steps.map((step) => ({ ...step, done: true })),
                state: 'done',
                intent: event.intent,
                usage: event.usage,
                seconds: (performance.now() - started) / 1000
            };
        case 'error':
            return { ...message, content: event.message, citations: [], state: 'error' };
    }
}

/**
 * The messages of one conversation, and asking it a question with the answer streamed in.
 * With no conversation yet, the first question creates one (`create`), so empty
 * conversations are never stored; `onCreated` hands it to the page.
 */
export function useChat(
    conversationId: string | null,
    create: () => Promise<Conversation>,
    onCreated: (conversation: Conversation) => void
) {
    // The messages on screen, with the conversation they belong to (null: a new chat).
    const [thread, setThread] = useState<{ id: string | null; messages: ChatMessage[] }>({
        id: null,
        messages: []
    });
    const [sending, setSending] = useState(false);
    const stream = useRef<AbortController | null>(null);

    // Another conversation was chosen: stop the answer being streamed, load its messages.
    useEffect(() => {
        if (thread.id === conversationId) return;
        stream.current?.abort();
        if (!conversationId) return;
        let current = true;
        api.getConversationMessages(conversationId)
            .then((stored) => {
                if (!current) return;
                setThread({
                    id: conversationId,
                    messages: stored.map((m) => ({
                        key: m.id,
                        role: m.role,
                        content: m.content,
                        citations: m.citations ?? [],
                        intent: m.intent,
                        usage: m.usage,
                        steps: [],
                        state: 'done'
                    }))
                });
            })
            .catch(() => current && setThread({ id: conversationId, messages: [] }));
        return () => {
            current = false;
        };
    }, [conversationId, thread.id]);

    useEffect(() => () => stream.current?.abort(), []);

    const shown = thread.id === conversationId;
    const loading = !shown && conversationId !== null;

    const send = useCallback(
        async (text: string) => {
            const question = text.trim();
            if (!question || sending || loading) return;
            const key = `${Date.now()}`;
            const answerKey = `answer-${key}`;
            const update = (change: (message: ChatMessage) => ChatMessage) =>
                setThread((t) => ({
                    ...t,
                    messages: t.messages.map((m) => (m.key === answerKey ? change(m) : m))
                }));
            setThread((t) => ({
                id: conversationId,
                messages: [
                    ...(t.id === conversationId ? t.messages : []),
                    {
                        key: `question-${key}`,
                        role: 'user',
                        content: question,
                        citations: [],
                        steps: [],
                        state: 'done'
                    },
                    {
                        key: answerKey,
                        role: 'assistant',
                        content: '',
                        citations: [],
                        steps: [],
                        state: 'streaming'
                    }
                ]
            }));
            setSending(true);
            const controller = new AbortController();
            stream.current = controller;
            const started = performance.now();
            try {
                let id = conversationId;
                if (!id) {
                    const conversation = await create();
                    id = conversation.id;
                    // The messages on screen are the new conversation's.
                    setThread((t) => ({ ...t, id: conversation.id }));
                    onCreated(conversation);
                }
                await api.streamAnswer(
                    id,
                    question,
                    (event) => update((message) => applyEvent(message, event, started)),
                    controller.signal
                );
                // A stream that ends without `done` or `error` was cut off.
                update((message) =>
                    message.state === 'streaming'
                        ? { ...message, content: message.content || FAILED, state: 'error' }
                        : message
                );
            } catch {
                if (controller.signal.aborted) return;
                update((message) => ({
                    ...message,
                    content: FAILED,
                    citations: [],
                    state: 'error'
                }));
            } finally {
                if (stream.current === controller) {
                    stream.current = null;
                    setSending(false);
                }
            }
        },
        [conversationId, create, onCreated, sending, loading]
    );

    return { messages: shown ? thread.messages : [], loading, sending, send };
}
