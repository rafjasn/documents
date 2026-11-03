'use client';

import { ReactNode } from 'react';
import {
    ContractFields,
    ExtractedFields as Fields,
    InvoiceFields,
    Party,
    ReceiptFields
} from '@/types';
import { formatDay, formatMoney } from '@/lib/utils';

function Field({ label, children, wide }: { label: string; children: ReactNode; wide?: boolean }) {
    return (
        <div className={wide ? 'sm:col-span-2' : ''}>
            <dt className="text-xs text-gray-500 dark:text-gray-400">{label}</dt>
            <dd className="mt-0.5 text-sm font-medium text-gray-800 dark:text-white/90">
                {children}
            </dd>
        </div>
    );
}

function PartyValue({ party }: { party?: Party }) {
    if (!party) return <>–</>;
    return (
        <>
            {party.name}
            {party.taxId && (
                <span className="ml-1.5 text-xs font-normal text-gray-500 dark:text-gray-400">
                    Tax ID {party.taxId}
                </span>
            )}
        </>
    );
}

function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
    if (rows.length === 0) return null;
    return (
        <div className="mt-5 overflow-x-auto">
            <table className="w-full text-left text-xs">
                <thead>
                    <tr className="border-b border-gray-200 text-gray-500 dark:border-gray-800 dark:text-gray-400">
                        {head.map((h, i) => (
                            <th
                                key={h}
                                className={`py-2 pr-3 font-medium ${i > 0 ? 'text-right' : ''}`}
                            >
                                {h}
                            </th>
                        ))}
                    </tr>
                </thead>
                <tbody>
                    {rows.map((row, r) => (
                        <tr
                            key={r}
                            className="border-b border-gray-100 last:border-0 dark:border-gray-800/60"
                        >
                            {row.map((cell, i) => (
                                <td
                                    key={i}
                                    className={`py-2 pr-3 text-gray-700 dark:text-gray-300 ${i > 0 ? 'whitespace-nowrap text-right tabular-nums' : ''}`}
                                >
                                    {cell ?? '–'}
                                </td>
                            ))}
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

const quantity = (value?: number) => (value === undefined ? '–' : value.toLocaleString('en-GB'));

function Invoice({ fields }: { fields: InvoiceFields }) {
    const money = (value?: number) => formatMoney(value, fields.currency);
    return (
        <>
            <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
                <Field label="Invoice number">{fields.invoiceNumber ?? '–'}</Field>
                <Field label="Issue date">{formatDay(fields.issueDate)}</Field>
                <Field label="Vendor">
                    <PartyValue party={fields.vendor} />
                </Field>
                <Field label="Buyer">
                    <PartyValue party={fields.buyer} />
                </Field>
                <Field label="Due date">{formatDay(fields.dueDate)}</Field>
                {fields.paidDate && <Field label="Paid">{formatDay(fields.paidDate)}</Field>}
            </dl>
            <div className="mt-5 grid grid-cols-3 gap-3 rounded-xl bg-gray-50 p-4 dark:bg-white/[0.03]">
                <Field label="Net">{money(fields.netTotal)}</Field>
                <Field label="VAT">{money(fields.taxTotal)}</Field>
                <Field label="Gross">
                    <span className="text-base">{money(fields.grossTotal)}</span>
                </Field>
            </div>
            {fields.reverseCharge && (
                <p className="mt-3 inline-flex rounded-full bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-700 dark:bg-amber-500/10 dark:text-amber-400">
                    Reverse charge: the buyer accounts for the VAT
                </p>
            )}
            <Table
                head={['Item', 'Qty', 'Unit price', 'Net', 'VAT']}
                rows={fields.lineItems.map((line) => [
                    line.description,
                    quantity(line.quantity),
                    money(line.unitPrice),
                    money(line.netAmount),
                    line.taxRate === undefined ? '–' : `${line.taxRate}%`
                ])}
            />
        </>
    );
}

function Receipt({ fields }: { fields: ReceiptFields }) {
    const money = (value?: number) => formatMoney(value, fields.currency);
    return (
        <>
            <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
                <Field label="Merchant">
                    <PartyValue party={fields.merchant} />
                </Field>
                <Field label="Date">{formatDay(fields.date)}</Field>
                {fields.receiptNumber && (
                    <Field label="Receipt number">{fields.receiptNumber}</Field>
                )}
                <Field label="Payment">{fields.paymentMethod ?? '–'}</Field>
            </dl>
            <div className="mt-5 grid grid-cols-2 gap-3 rounded-xl bg-gray-50 p-4 dark:bg-white/[0.03]">
                <Field label="Total">
                    <span className="text-base">{money(fields.total)}</span>
                </Field>
                <Field label="Tax">{money(fields.taxTotal)}</Field>
            </div>
            <Table
                head={['Item', 'Qty', 'Unit price', 'Amount']}
                rows={fields.items.map((item) => [
                    item.description,
                    quantity(item.quantity),
                    money(item.unitPrice),
                    money(item.amount)
                ])}
            />
        </>
    );
}

function Contract({ fields }: { fields: ContractFields }) {
    const days = (value?: number) => (value === undefined ? '–' : `${value} days`);
    return (
        <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
            <Field label="Parties" wide>
                <ul className="space-y-0.5">
                    {fields.parties.map((party, i) => (
                        <li key={i}>
                            <PartyValue party={party} />
                        </li>
                    ))}
                </ul>
            </Field>
            {fields.contractNumber && (
                <Field label="Contract number">{fields.contractNumber}</Field>
            )}
            <Field label="Effective">{formatDay(fields.effectiveDate)}</Field>
            <Field label="Ends">{formatDay(fields.endDate)}</Field>
            <Field label="Renews automatically">
                {fields.autoRenewal === undefined ? '–' : fields.autoRenewal ? 'Yes' : 'No'}
            </Field>
            <Field label="Notice period">{days(fields.noticePeriodDays)}</Field>
            <Field label="Payment terms">{days(fields.paymentTermsDays)}</Field>
            <Field label="Value cap">
                {fields.valueCap
                    ? formatMoney(fields.valueCap.amount, fields.valueCap.currency)
                    : '–'}
            </Field>
            <Field label="Governing law">{fields.governingLaw ?? '–'}</Field>
        </dl>
    );
}

/** The typed fields the ingestion worker extracted from an invoice, receipt or contract. */
export default function ExtractedFields({ fields }: { fields: Fields }) {
    return (
        <div data-testid="extracted-fields" data-kind={fields.kind}>
            {fields.kind === 'invoice' ? (
                <Invoice fields={fields} />
            ) : fields.kind === 'receipt' ? (
                <Receipt fields={fields} />
            ) : (
                <Contract fields={fields} />
            )}
        </div>
    );
}
