'use client';

import { ReactNode, useEffect, useRef, useState } from 'react';
import { AlertCircle, ArrowUp, Check, ChevronRight, Loader2 } from 'lucide-react';
import AnswerText from './AnswerText';
import { ChatMessage, ChatStep } from '@/hooks/useChat';
import { ChatIntent } from '@/types';
import { formatCost } from '@/lib/utils';

export interface ActiveCitation {
    messageKey: string;
    citationId: number;
}

const INTENTS: Record<ChatIntent, string> = {
    smalltalk: 'Chat',
    retrieve: 'Searched the documents',
    aggregate: 'Calculated from extracted fields',
    mixed: 'Calculated and searched'
};

function Steps({ steps }: { steps: ChatStep[] }) {
    return (
        <ol className="space-y-1" data-testid="agent-steps">
            {steps.map((step, i) => (
                <li
                    key={i}
                    data-testid="agent-step"
                    className={`flex items-center gap-2 text-xs ${
                        step.done
                            ? 'text-gray-500 dark:text-gray-400'
                            : 'text-gray-800 dark:text-white/90'
                    }`}
                >
                    {step.done ? (
                        <Check size={13} className="shrink-0 text-emerald-500" />
                    ) : (
                        <Loader2 size={13} className="shrink-0 animate-spin text-brand-500" />
                    )}
                    {step.label}
                </li>
            ))}
        </ol>
    );
}

function Footer({ message }: { message: ChatMessage }) {
    const [open, setOpen] = useState(false);
    const facts = [
        message.intent && INTENTS[message.intent],
        message.usage &&
            `${message.usage.llmCalls} model call${message.usage.llmCalls === 1 ? '' : 's'}`,
        message.usage && formatCost(message.usage.costUsd),
        message.seconds !== undefined && `${message.seconds.toFixed(1)} s`
    ].filter(Boolean);
    if (facts.length === 0) return null;
    return (
        <div className="mt-2 text-xs text-gray-400 dark:text-gray-500">
            {message.steps.length > 0 ? (
                <button
                    type="button"
                    onClick={() => setOpen((o) => !o)}
                    className="inline-flex items-center gap-1 hover:text-gray-600 dark:hover:text-gray-300"
                >
                    <ChevronRight
                        size={12}
                        className={`transition-transform ${open ? 'rotate-90' : ''}`}
                    />
                    {facts.join(' · ')}
                </button>
            ) : (
                <span>{facts.join(' · ')}</span>
            )}
            {open && (
                <div className="mt-2 pl-4">
                    <Steps steps={message.steps} />
                </div>
            )}
        </div>
    );
}

interface ChatThreadProps {
    messages: ChatMessage[];
    loading: boolean;
    sending: boolean;
    onSend: (text: string) => void;
    active: ActiveCitation | null;
    onCite: (citation: ActiveCitation) => void;
    placeholder: string;
    /** Shown while the conversation has no messages. */
    empty?: ReactNode;
    /** Above the messages, e.g. the conversation's scope. */
    header?: ReactNode;
}

export default function ChatThread({
    messages,
    loading,
    sending,
    onSend,
    active,
    onCite,
    placeholder,
    empty,
    header
}: ChatThreadProps) {
    const [input, setInput] = useState('');
    const scroller = useRef<HTMLDivElement>(null);

    // Follow the answer as it streams.
    useEffect(() => {
        const element = scroller.current;
        if (element) element.scrollTop = element.scrollHeight;
    }, [messages]);

    const submit = () => {
        if (!input.trim() || sending) return;
        onSend(input);
        setInput('');
    };

    return (
        <div className="flex min-h-0 flex-1 flex-col">
            {header}
            <div ref={scroller} className="min-h-0 flex-1 space-y-5 overflow-y-auto px-1 py-4">
                {loading ? (
                    <div className="flex justify-center py-10">
                        <Loader2 size={20} className="animate-spin text-gray-400" />
                    </div>
                ) : messages.length === 0 ? (
                    empty
                ) : (
                    messages.map((message) =>
                        message.role === 'user' ? (
                            <div key={message.key} className="flex justify-end">
                                <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-brand-500 px-4 py-2.5 text-sm text-white">
                                    {message.content}
                                </div>
                            </div>
                        ) : (
                            <div
                                key={message.key}
                                className="flex justify-start"
                                data-testid="answer"
                                data-state={message.state}
                            >
                                <div className="max-w-[92%] rounded-2xl rounded-bl-md bg-gray-100 px-4 py-3 text-sm leading-relaxed text-gray-800 dark:bg-gray-800 dark:text-gray-200">
                                    {message.state === 'error' ? (
                                        <p className="flex items-start gap-2 text-red-600 dark:text-red-400">
                                            <AlertCircle size={16} className="mt-0.5 shrink-0" />
                                            {message.content}
                                        </p>
                                    ) : (
                                        message.content && (
                                            <div data-testid="answer-text">
                                                <AnswerText
                                                    text={message.content}
                                                    citations={message.citations}
                                                    activeId={
                                                        active?.messageKey === message.key
                                                            ? active.citationId
                                                            : null
                                                    }
                                                    onCite={(citationId) =>
                                                        onCite({
                                                            messageKey: message.key,
                                                            citationId
                                                        })
                                                    }
                                                />
                                            </div>
                                        )
                                    )}
                                    {message.state === 'streaming' && (
                                        <div className={message.content ? 'mt-3' : ''}>
                                            {message.steps.length > 0 ? (
                                                <Steps steps={message.steps} />
                                            ) : (
                                                <Loader2
                                                    size={14}
                                                    className="animate-spin text-gray-400"
                                                />
                                            )}
                                        </div>
                                    )}
                                    {message.state === 'done' && message.citations.length > 0 && (
                                        <div className="mt-3 flex flex-wrap gap-1.5">
                                            {message.citations.map((c) => (
                                                <button
                                                    key={c.citationId}
                                                    type="button"
                                                    data-testid="citation-chip"
                                                    onClick={() =>
                                                        onCite({
                                                            messageKey: message.key,
                                                            citationId: c.citationId
                                                        })
                                                    }
                                                    className={`max-w-full truncate rounded-md border px-2 py-0.5 text-left text-xs transition-colors ${
                                                        active?.messageKey === message.key &&
                                                        active.citationId === c.citationId
                                                            ? 'border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300'
                                                            : 'border-gray-200 bg-white text-gray-600 hover:border-brand-300 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300'
                                                    }`}
                                                >
                                                    <span className="font-semibold">
                                                        [{c.citationId}]
                                                    </span>{' '}
                                                    {c.title}
                                                    {c.page
                                                        ? ` · p. ${c.page}`
                                                        : ' · extracted fields'}
                                                </button>
                                            ))}
                                        </div>
                                    )}
                                    {message.state === 'done' && <Footer message={message} />}
                                </div>
                            </div>
                        )
                    )
                )}
            </div>
            <div className="flex items-end gap-2 border-t border-gray-100 pt-3 dark:border-gray-800">
                <textarea
                    data-testid="chat-input"
                    value={input}
                    rows={1}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === 'Enter' && !e.shiftKey) {
                            e.preventDefault();
                            submit();
                        }
                    }}
                    placeholder={placeholder}
                    className="max-h-40 min-h-[42px] flex-1 resize-none rounded-xl border border-gray-300 bg-white px-3.5 py-2.5 text-sm text-gray-800 placeholder-gray-400 outline-none focus:border-brand-500 focus:ring-1 focus:ring-brand-500 dark:border-gray-700 dark:bg-gray-900 dark:text-white/90 dark:placeholder-gray-500"
                />
                <button
                    type="button"
                    data-testid="chat-send"
                    onClick={submit}
                    disabled={sending || !input.trim()}
                    aria-label="Send"
                    className="inline-flex h-[42px] w-[42px] shrink-0 items-center justify-center rounded-xl bg-brand-500 text-white hover:bg-brand-600 disabled:opacity-50"
                >
                    {sending ? (
                        <Loader2 size={16} className="animate-spin" />
                    ) : (
                        <ArrowUp size={18} />
                    )}
                </button>
            </div>
        </div>
    );
}
