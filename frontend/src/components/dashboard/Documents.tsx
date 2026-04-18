'use client';

import { useState, useMemo } from 'react';
import { Table, TableBody, TableCell, TableHeader, TableRow } from '../ui/table';
import Badge from '../ui/badge/Badge';
import Link from 'next/link';
import { Document, DocumentStatus } from '@/types';
import { formatFileSize, relativeTime, statusLabel, mimeTypeIcon } from '@/lib/utils';
import ChartTab, { TabItem } from '@/components/common/ChartTab';
import { ArrowUp, ArrowDown, ArrowUpDown } from 'lucide-react';

type SortField = 'createdAt' | 'fileName' | 'fileSize' | 'category';
type SortDir = 'asc' | 'desc';
type FilterValue = DocumentStatus | 'ALL';

const STATUS_TABS: TabItem<FilterValue>[] = [
    { label: 'All', value: 'ALL' },
    { label: 'Pending', value: 'PENDING' },
    { label: 'Processing', value: 'PROCESSING' },
    { label: 'Completed', value: 'COMPLETED' },
    { label: 'Failed', value: 'FAILED' }
];

interface DocumentsProps {
    documents: Document[];
    loading: boolean;
    refreshing: boolean;
    connected: boolean;
    onRefresh: () => void;
}

export default function Documents({
    documents,
    loading,
    refreshing,
    connected,
    onRefresh
}: DocumentsProps) {
    const [filter, setFilter] = useState<FilterValue>('ALL');
    const [sortField, setSortField] = useState<SortField>('createdAt');
    const [sortDir, setSortDir] = useState<SortDir>('desc');

    const handleSort = (field: SortField) => {
        if (field === sortField) {
            setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
        } else {
            setSortField(field);
            setSortDir(field === 'createdAt' ? 'desc' : 'asc');
        }
    };

    const sortedDocuments = useMemo(() => {
        const filtered =
            filter === 'ALL' ? documents : documents.filter((d) => d.status === filter);
        return [...filtered].sort((a, b) => {
            let cmp = 0;
            switch (sortField) {
                case 'createdAt':
                    cmp = a.createdAt.localeCompare(b.createdAt);
                    break;
                case 'fileName':
                    cmp = a.fileName.localeCompare(b.fileName);
                    break;
                case 'fileSize':
                    cmp = a.fileSize - b.fileSize;
                    break;
                case 'category':
                    cmp = (a.category ?? '').localeCompare(b.category ?? '');
                    break;
            }
            return sortDir === 'asc' ? cmp : -cmp;
        });
    }, [documents, filter, sortField, sortDir]);

    return (
        <div className="overflow-hidden rounded-2xl border border-gray-200 bg-white px-4 pb-3 pt-4 dark:border-gray-800 dark:bg-white/[0.03] sm:px-6">
            <div className="flex flex-col gap-3 mb-4 sm:flex-row sm:items-center sm:justify-between">
                <div>
                    <h3 className="text-lg font-semibold text-gray-800 dark:text-white/90">
                        Documents
                    </h3>
                    <p className="mt-1 text-sm text-ink-3 dark:text-white/90">
                        {connected ? (
                            <span className="inline-flex items-center gap-1.5">
                                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 " />
                                Live updates active
                            </span>
                        ) : (
                            <span className="inline-flex items-center gap-1.5">
                                <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
                                Reconnecting...
                            </span>
                        )}
                    </p>
                </div>

                <div className="flex flex-wrap items-center gap-3">
                    <button
                        onClick={onRefresh}
                        disabled={refreshing}
                        className="inline-flex items-center gap-2 rounded-lg border border-gray-300 bg-white px-4 py-2.5 text-theme-sm font-medium text-gray-700 shadow-theme-xs hover:bg-gray-50 hover:text-gray-800 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-white/[0.03] dark:hover:text-gray-200"
                    >
                        Refresh
                    </button>
                    <Link
                        href="/upload"
                        className="inline-flex items-center gap-2 rounded-lg border border-gray-300 bg-white px-4 py-2.5 text-theme-sm font-medium text-gray-700 shadow-theme-xs hover:bg-gray-50 hover:text-gray-800 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-white/[0.03] dark:hover:text-gray-200"
                    >
                        Upload
                    </Link>
                </div>
            </div>
            <div className="flex flex-col gap-3 mb-4 sm:flex-row sm:items-center sm:justify-between">
                <ChartTab tabs={STATUS_TABS} selected={filter} onChange={setFilter} />
            </div>

            <div className="max-w-full overflow-x-auto">
                <Table>
                    <TableHeader className="border-gray-100 dark:border-gray-800 border-y">
                        <TableRow>
                            {(
                                [
                                    { label: 'Document', field: 'fileName' },
                                    { label: 'Category', field: 'category' },
                                    { label: 'Size', field: 'fileSize' },
                                    { label: 'Date', field: 'createdAt' }
                                ] as { label: string; field: SortField }[]
                            ).map(({ label, field }) => (
                                <TableCell
                                    key={field}
                                    isHeader
                                    className="py-3 text-theme-xs dark:text-gray-400"
                                >
                                    <button
                                        onClick={() => handleSort(field)}
                                        className="inline-flex items-center gap-1 font-medium text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-white"
                                    >
                                        {label}
                                        {sortField === field ? (
                                            sortDir === 'asc' ? (
                                                <ArrowUp size={12} />
                                            ) : (
                                                <ArrowDown size={12} />
                                            )
                                        ) : (
                                            <ArrowUpDown size={12} className="opacity-40" />
                                        )}
                                    </button>
                                </TableCell>
                            ))}
                            <TableCell
                                isHeader
                                className="py-3 font-medium text-gray-500 text-start text-theme-xs dark:text-gray-400"
                            >
                                Status
                            </TableCell>
                        </TableRow>
                    </TableHeader>

                    <TableBody className="divide-y divide-gray-100 dark:divide-gray-800">
                        {sortedDocuments.map((doc) => (
                            <TableRow key={doc.id}>
                                <TableCell className="py-3">
                                    <Link
                                        href={`/documents/${doc.id}`}
                                        className="flex items-center gap-3 hover:opacity-80"
                                    >
                                        <div className="flex h-[50px] w-[40px] shrink-0 items-center justify-center overflow-hidden rounded-md bg-gray-100 dark:bg-gray-800">
                                            {doc.thumbnailUrl ? (
                                                <img
                                                    width={40}
                                                    height={50}
                                                    src={doc.thumbnailUrl}
                                                    className="h-[50px] w-[40px] object-cover"
                                                    alt={doc.fileName}
                                                />
                                            ) : (
                                                <span className="text-lg">
                                                    {mimeTypeIcon(doc.mimeType)}
                                                </span>
                                            )}
                                        </div>
                                        <p className="font-medium text-gray-800 text-theme-sm dark:text-white/90 truncate max-w-[200px]">
                                            {doc.fileName}
                                        </p>
                                    </Link>
                                </TableCell>
                                <TableCell className="py-3 text-gray-500 text-theme-sm dark:text-gray-400">
                                    {doc.category || '-'}
                                </TableCell>
                                <TableCell className="py-3 text-gray-500 text-theme-sm dark:text-gray-400">
                                    {formatFileSize(doc.fileSize)}
                                </TableCell>
                                <TableCell className="py-3 text-gray-500 text-theme-sm dark:text-gray-400">
                                    {relativeTime(doc.createdAt)}
                                </TableCell>
                                <TableCell className="py-3">
                                    <Badge
                                        size="sm"
                                        color={
                                            doc.status === 'COMPLETED'
                                                ? 'success'
                                                : doc.status === 'PENDING'
                                                  ? 'warning'
                                                  : doc.status === 'FAILED'
                                                    ? 'error'
                                                    : 'info'
                                        }
                                    >
                                        {statusLabel(doc.status)}
                                    </Badge>
                                </TableCell>
                            </TableRow>
                        ))}
                    </TableBody>
                </Table>
            </div>
        </div>
    );
}
