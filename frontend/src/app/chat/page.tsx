'use client';

import { Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { Loader2, MessagesSquare, Sparkles } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth';
import { useChat } from '@/hooks/useChat';
import { api } from '@/lib/api';
import { ChatScope, Conversation, Document } from '@/types';
import ChatThread, { ActiveCitation } from '@/components/chat/ChatThread';
import ConversationList from '@/components/chat/ConversationList';
import SourcePanel from '@/components/chat/SourcePanel';
import { ScopeChips, ScopeEditor } from '@/components/chat/Scope';

const SUGGESTIONS = [
    'Which contracts renew automatically, and what notice do they need?',
    'What is the total of invoices issued in 2026, by currency?',
    'Which invoices are reverse-charge?'
];

function LibraryChat() {
    const router = useRouter();
    const params = useSearchParams();
    const [conversations, setConversations] = useState<Conversation[]>([]);
    const [documents, setDocuments] = useState<Document[] | null>(null);
    const [activeId, setActiveId] = useState<string | null>(params.get('c'));
    const [draftScope, setDraftScope] = useState<ChatScope>({});
    const [citation, setCitation] = useState<ActiveCitation | null>(null);
    const [drawer, setDrawer] = useState(false);
    const [listOpen, setListOpen] = useState(false);

    const titles = useMemo(
        () => new Map((documents ?? []).map((d) => [d.id, d.fileName])),
        [documents]
    );
    const active = conversations.find((c) => c.id === activeId) ?? null;

    const refreshConversations = useCallback(
        () =>
            api
                .listConversations()
                .then(setConversations)
                .catch(() => {
                    // Keep the list on screen; the next answer refreshes it.
                }),
        []
    );

    useEffect(() => {
        api.listAllDocuments('COMPLETED')
            .then(setDocuments)
            .catch(() => setDocuments([]));
    }, []);

    const open = useCallback(
        (id: string | null) => {
            setActiveId(id);
            setCitation(null);
            setListOpen(false);
            router.replace(id ? `/chat?c=${id}` : '/chat', { scroll: false });
        },
        [router]
    );

    const create = useCallback(() => api.createConversation(draftScope), [draftScope]);
    const onCreated = useCallback(
        (conversation: Conversation) => {
            setConversations((prev) => [conversation, ...prev]);
            setActiveId(conversation.id);
            router.replace(`/chat?c=${conversation.id}`, { scroll: false });
        },
        [router]
    );
    const chat = useChat(activeId, create, onCreated);

    // On load, and when an answer is stored: titles and order change.
    useEffect(() => {
        if (!chat.sending) refreshConversations();
    }, [chat.sending, refreshConversations]);

    const remove = async (id: string) => {
        await api.deleteConversation(id).catch(() => {
            // Already gone, or it stays listed after the next refresh.
        });
        setConversations((prev) => prev.filter((c) => c.id !== id));
        if (id === activeId) open(null);
    };

    const cite = (next: ActiveCitation) => {
        setCitation(next);
        setDrawer(true);
    };

    // The sources shown: the selected answer's, else the latest answer's.
    const sourceMessage =
        chat.messages.find((m) => m.key === citation?.messageKey) ??
        [...chat.messages].reverse().find((m) => m.role === 'assistant' && m.citations.length > 0);
    const sources = (
        <SourcePanel
            citations={sourceMessage?.citations ?? []}
            activeId={
                citation && sourceMessage?.key === citation.messageKey ? citation.citationId : null
            }
            onSelect={(citationId) =>
                sourceMessage && setCitation({ messageKey: sourceMessage.key, citationId })
            }
        />
    );

    const scope = active?.scope ?? draftScope;
    const card =
        'rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03]';

    return (
        <div className="flex flex-col gap-4 lg:h-[calc(100dvh-8.5rem)] lg:flex-row">
            <aside className={`${card} hidden w-64 shrink-0 p-3 lg:block`}>
                <ConversationList
                    conversations={conversations}
                    activeId={activeId}
                    titles={titles}
                    onSelect={open}
                    onNew={() => open(null)}
                    onDelete={remove}
                />
            </aside>

            <section className={`${card} flex min-h-[70vh] min-w-0 flex-1 flex-col p-4 lg:min-h-0`}>
                <div className="flex items-start justify-between gap-3 border-b border-gray-100 pb-3 dark:border-gray-800">
                    <div className="min-w-0">
                        <h1
                            className="truncate text-base font-semibold text-gray-800 dark:text-white/90"
                            data-testid="chat-title"
                        >
                            {active ? active.title || 'New conversation' : 'New chat'}
                        </h1>
                        <div className="mt-1.5">
                            <ScopeChips scope={scope} titles={titles} />
                        </div>
                    </div>
                    <button
                        type="button"
                        onClick={() => setListOpen((o) => !o)}
                        className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-gray-200 px-2.5 py-1.5 text-xs text-gray-600 lg:hidden dark:border-gray-700 dark:text-gray-300"
                    >
                        <MessagesSquare size={14} /> Conversations
                    </button>
                </div>
                {listOpen && (
                    <div className="max-h-80 border-b border-gray-100 py-3 lg:hidden dark:border-gray-800">
                        <ConversationList
                            conversations={conversations}
                            activeId={activeId}
                            titles={titles}
                            onSelect={open}
                            onNew={() => open(null)}
                            onDelete={remove}
                        />
                    </div>
                )}
                <ChatThread
                    messages={chat.messages}
                    loading={chat.loading}
                    sending={chat.sending}
                    onSend={chat.send}
                    active={citation}
                    onCite={cite}
                    placeholder="Ask about your documents…"
                    empty={
                        activeId ? null : (
                            <div className="mx-auto max-w-2xl space-y-4 py-2">
                                <ScopeEditor
                                    scope={draftScope}
                                    onChange={setDraftScope}
                                    documents={documents}
                                />
                                <div className="space-y-2">
                                    <p className="flex items-center gap-1.5 text-xs font-medium text-gray-500 dark:text-gray-400">
                                        <Sparkles size={13} /> Try asking
                                    </p>
                                    {SUGGESTIONS.map((s) => (
                                        <button
                                            key={s}
                                            type="button"
                                            onClick={() => chat.send(s)}
                                            className="block w-full rounded-xl border border-gray-200 px-3.5 py-2.5 text-left text-sm text-gray-700 hover:border-brand-300 hover:bg-brand-25 dark:border-gray-800 dark:text-gray-300 dark:hover:bg-white/[0.04]"
                                        >
                                            {s}
                                        </button>
                                    ))}
                                </div>
                            </div>
                        )
                    }
                />
            </section>

            <aside className={`${card} hidden w-[26rem] shrink-0 p-4 xl:block`}>{sources}</aside>

            {drawer && citation && (
                <div className="fixed inset-0 z-[100000] xl:hidden">
                    <div
                        className="absolute inset-0 bg-gray-900/40"
                        onClick={() => setDrawer(false)}
                    />
                    <div className="absolute inset-y-0 right-0 w-full max-w-md overflow-hidden bg-white p-4 shadow-xl dark:bg-gray-900">
                        <SourcePanel
                            citations={sourceMessage?.citations ?? []}
                            activeId={citation.citationId}
                            onSelect={(citationId) =>
                                sourceMessage &&
                                setCitation({ messageKey: sourceMessage.key, citationId })
                            }
                            onClose={() => setDrawer(false)}
                        />
                    </div>
                </div>
            )}
        </div>
    );
}

export default function ChatPage() {
    const { user, loading } = useAuth();
    const router = useRouter();

    useEffect(() => {
        if (!loading && !user) router.replace('/login');
    }, [user, loading, router]);

    if (loading || !user) {
        return (
            <div className="flex h-screen items-center justify-center">
                <Loader2 className="h-8 w-8 animate-spin text-brand-600" />
            </div>
        );
    }
    return (
        <Suspense fallback={null}>
            <LibraryChat />
        </Suspense>
    );
}
