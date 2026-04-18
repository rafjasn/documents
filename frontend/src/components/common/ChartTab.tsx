import React from 'react';

export interface TabItem<T extends string = string> {
    label: string;
    value: T;
}

interface ChartTabProps<T extends string = string> {
    tabs: TabItem<T>[];
    selected: T;
    onChange: (value: T) => void;
}

function ChartTab<T extends string = string>({ tabs, selected, onChange }: ChartTabProps<T>) {
    return (
        <div className="flex items-center gap-0.5 rounded-lg bg-gray-100 p-0.5 dark:bg-gray-900">
            {tabs.map((tab) => (
                <button
                    key={tab.value}
                    onClick={() => onChange(tab.value)}
                    className={`px-3 py-2 font-medium w-full rounded-md text-theme-sm hover:text-gray-900 dark:hover:text-white ${
                        selected === tab.value
                            ? 'shadow-theme-xs text-gray-900 dark:text-white bg-white dark:bg-gray-800'
                            : 'text-gray-500 dark:text-gray-400'
                    }`}
                >
                    {tab.label}
                </button>
            ))}
        </div>
    );
}

export default ChartTab;
