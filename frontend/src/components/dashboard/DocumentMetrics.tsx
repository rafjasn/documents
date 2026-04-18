'use client';
import React, { useMemo } from 'react';
import { Document } from '@/types';
import { CheckCircle, Clock, List, RefreshCw, XCircle } from 'lucide-react';

interface DocumentMetricsProps {
    documents: Document[];
}

export const DocumentMetrics = ({ documents }: DocumentMetricsProps) => {
    const metricItems = useMemo(
        () => [
            {
                label: 'Total',
                value: documents.length,
                icon: List
            },
            {
                label: 'Pending',
                value: documents.filter((d) => d.status === 'PENDING').length,
                icon: Clock
            },
            {
                label: 'Processing',
                value: documents.filter((d) => d.status === 'PROCESSING').length,
                icon: RefreshCw
            },
            {
                label: 'Completed',
                value: documents.filter((d) => d.status === 'COMPLETED').length,
                icon: CheckCircle
            },
            {
                label: 'Failed',
                value: documents.filter((d) => d.status === 'FAILED').length,
                icon: XCircle
            }
        ],
        [documents]
    );

    return (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-5 md:gap-6 mb-6">
            {metricItems.map((item, index) => {
                const Icon = item.icon;

                return (
                    <div
                        key={index}
                        className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.03]"
                    >
                        <div className="flex items-center gap-3">
                            <div className="flex items-center justify-center w-10 h-10 shrink-0 bg-gray-100 rounded-xl dark:bg-gray-800">
                                <Icon className="text-gray-800 size-5 dark:text-white/90" />
                            </div>
                            <div>
                                <span className="text-xs text-gray-500 dark:text-gray-400">
                                    {item.label}
                                </span>
                                <h4 className="font-bold text-gray-800 text-title-sm dark:text-white/90">
                                    {item.value}
                                </h4>
                            </div>
                        </div>
                    </div>
                );
            })}
        </div>
    );
};
