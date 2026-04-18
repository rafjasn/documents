declare module 'mammoth' {
    interface Result {
        value: string;
        messages: { type: string; message: string }[];
    }
    export function extractRawText(input: { buffer: Buffer }): Promise<Result>;
    export function convertToHtml(input: { buffer: Buffer }): Promise<Result>;
}
