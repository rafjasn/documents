'use client';

import { MessageSquarePlus, Trash2 } from 'lucide-react';
import { describeScope } from './Scope';
import { Conversation } from '@/types';
import { relativeTime } from '@/lib/utils';

interface ConversationListProps {
    conversations: Conversation[];
    activeId: string | null;
    titles: Map<string, string>;
    onSelect: (id: string) => void;
    onNew: () => void;
    onDelete: (id: string) => void;
}

export default function ConversationList({
    conversations,
    activeId,
    titles,
    onSelect,
    onNew,
    onDelete
}: ConversationListProps) {
    return (
        <div className="flex h-full min-h-0 flex-col">
            <button
                type="button"
                onClick={onNew}
                data-testid="new-chat"
                className="mb-3 inline-flex w-full items-center justify-center gap-2 rounded-xl bg-brand-500 px-3 py-2.5 text-sm font-medium text-white hover:bg-brand-600"
            >
                <MessageSquarePlus size={16} />
                New chat
            </button>
            <ul
                className="min-h-0 flex-1 space-y-1 overflow-y-auto"
                data-testid="conversation-list"
            >
                {conversations.length === 0 && (
                    <li className="px-2 py-3 text-xs text-gray-400 dark:text-gray-500">
                        No conversations yet.
                    </li>
                )}
                {conversations.map((c) => (
                    <li key={c.id} className="group relative">
                        <button
                            type="button"
                            onClick={() => onSelect(c.id)}
                            className={`w-full rounded-lg px-3 py-2 pr-8 text-left transition-colors ${
                                c.id === activeId
                                    ? 'bg-brand-50 dark:bg-brand-500/15'
                                    : 'hover:bg-gray-50 dark:hover:bg-white/[0.04]'
                            }`}
                        >
                            <p
                                className={`truncate text-sm ${
                                    c.id === activeId
                                        ? 'font-medium text-brand-700 dark:text-brand-300'
                                        : 'text-gray-700 dark:text-gray-300'
                                }`}
                            >
                                {c.title || 'New conversation'}
                            </p>
                            <p className="mt-0.5 truncate text-xs text-gray-400 dark:text-gray-500">
                                {relativeTime(c.updatedAt)} ·{' '}
                                {describeScope(c.scope, titles).join(', ')}
                            </p>
                        </button>
                        <button
                            type="button"
                            aria-label="Delete conversation"
                            onClick={() => {
                                if (confirm('Delete this conversation?')) onDelete(c.id);
                            }}
                            className="absolute right-1.5 top-2 hidden rounded-md p-1 text-gray-400 hover:bg-red-50 hover:text-red-600 group-hover:block dark:hover:bg-red-900/20"
                        >
                            <Trash2 size={14} />
                        </button>
                    </li>
                ))}
            </ul>
        </div>
    );
}
