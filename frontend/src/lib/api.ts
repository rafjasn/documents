import { AuthResponse, Document, PaginatedResponse } from '@/types';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:3001';

class ApiClient {
    private token: string | null = null;
    private refreshing: Promise<boolean> | null = null;
    private refreshTimer: ReturnType<typeof setTimeout> | null = null;

    setToken(token: string) {
        this.token = token;
        if (typeof window !== 'undefined') {
            localStorage.setItem('documents_token', token);
        }
        this.scheduleProactiveRefresh(token);
    }

    getToken(): string | null {
        if (this.token) return this.token;
        if (typeof window !== 'undefined') {
            const stored = localStorage.getItem('documents_token');
            if (stored) {
                this.token = stored;
                // Schedule proactive refresh on first read (e.g. after a page reload)
                // without going through setToken() which also writes back to localStorage.
                this.scheduleProactiveRefresh(stored);
            }
        }
        return this.token;
    }

    setRefreshToken(token: string) {
        if (typeof window !== 'undefined') {
            localStorage.setItem('documents_refresh_token', token);
        }
    }

    getRefreshToken(): string | null {
        if (typeof window !== 'undefined') {
            return localStorage.getItem('documents_refresh_token');
        }
        return null;
    }

    clearTokens() {
        this.token = null;
        if (this.refreshTimer !== null) {
            clearTimeout(this.refreshTimer);
            this.refreshTimer = null;
        }
        if (typeof window !== 'undefined') {
            localStorage.removeItem('documents_token');
            localStorage.removeItem('documents_refresh_token');
            localStorage.removeItem('documents_user');
        }
    }

    clearToken() {
        this.clearTokens();
    }

    private scheduleProactiveRefresh(token: string): void {
        if (typeof window === 'undefined') return;

        if (this.refreshTimer !== null) {
            clearTimeout(this.refreshTimer);
            this.refreshTimer = null;
        }

        try {
            const payload = JSON.parse(atob(token.split('.')[1])) as { exp?: number };

            if (!payload.exp) {
                return;
            }

            const msUntilRefresh = Math.max(0, payload.exp * 1000 - Date.now() - 60_000);

            this.refreshTimer = setTimeout(async () => {
                this.refreshTimer = null;
                const ok = await this.tryRefresh();

                if (!ok) {
                    this.redirectToLogin();
                }
            }, msUntilRefresh);
        } catch {
            // malformed token - leave reactive refresh as fallback
        }
    }

    private redirectToLogin() {
        this.clearTokens();
        if (typeof window !== 'undefined') {
            window.location.href = '/login';
        }
    }

    private async tryRefresh(): Promise<boolean> {
        // Deduplicate concurrent refresh attempts
        if (this.refreshing) return this.refreshing;

        this.refreshing = (async () => {
            const refreshToken = this.getRefreshToken();
            if (!refreshToken) return false;

            try {
                const response = await fetch(`${API_URL}/api/auth/refresh`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ refreshToken })
                });

                if (!response.ok) return false;

                const data = await response.json();
                if (!data.accessToken) return false;

                this.setToken(data.accessToken);
                if (data.refreshToken) this.setRefreshToken(data.refreshToken);
                return true;
            } catch {
                return false;
            } finally {
                this.refreshing = null;
            }
        })();

        return this.refreshing;
    }

    private async request<T>(path: string, options: RequestInit = {}, retry = true): Promise<T> {
        const token = this.getToken();
        const headers: Record<string, string> = {
            ...(options.headers as Record<string, string>)
        };

        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }

        if (!(options.body instanceof FormData)) {
            headers['Content-Type'] = 'application/json';
        }

        const response = await fetch(`${API_URL}/api${path}`, { ...options, headers });

        if (response.status === 401 && retry) {
            const refreshed = await this.tryRefresh();
            if (refreshed) {
                return this.request(path, options, false);
            }
            this.redirectToLogin();
            throw new ApiError(401, 'Session expired');
        }

        if (!response.ok) {
            const error = await response.json().catch(() => ({ message: 'Request failed' }));
            throw new ApiError(response.status, error.message || 'Request failed');
        }

        if (response.status === 204) return undefined as T;
        return response.json();
    }

    // Auth
    async register(email: string, password: string): Promise<AuthResponse> {
        return this.request('/auth/register', {
            method: 'POST',
            body: JSON.stringify({ email, password })
        });
    }

    async login(email: string, password: string): Promise<AuthResponse> {
        return this.request('/auth/login', {
            method: 'POST',
            body: JSON.stringify({ email, password })
        });
    }

    // Documents
    async listDocuments(
        status?: string,
        limit = 20,
        lastKey?: string
    ): Promise<PaginatedResponse<Document>> {
        const params = new URLSearchParams();
        if (status) params.set('status', status);
        params.set('limit', String(limit));
        if (lastKey) params.set('lastKey', lastKey);
        return this.request(`/documents?${params}`);
    }

    async getDocument(id: string): Promise<Document> {
        return this.request(`/documents/${id}`);
    }

    async presignUpload(
        fileName: string,
        mimeType: string,
        fileSize: number,
        displayName?: string
    ): Promise<{ documentId: string; uploadUrl: string; fields: Record<string, string> }> {
        return this.request('/documents/presign', {
            method: 'POST',
            body: JSON.stringify({ fileName, mimeType, fileSize, displayName })
        });
    }

    uploadToS3(
        uploadUrl: string,
        fields: Record<string, string>,
        file: File,
        onProgress: (percent: number) => void
    ): Promise<void> {
        return new Promise((resolve, reject) => {
            const form = new FormData();

            for (const [k, v] of Object.entries(fields)) form.append(k, v);

            form.append('file', file);

            const xhr = new XMLHttpRequest();
            xhr.open('POST', uploadUrl);
            xhr.upload.onprogress = (e) => {
                if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
            };
            xhr.onload = () => {
                if (xhr.status >= 200 && xhr.status < 300) resolve();
                else reject(new Error(`Upload failed: ${xhr.status} ${xhr.responseText}`));
            };
            xhr.onerror = () => reject(new Error('Upload failed'));
            xhr.send(form);
        });
    }

    async getDownloadUrl(id: string): Promise<{ downloadUrl: string }> {
        return this.request(`/documents/${id}/download`);
    }

    async getChatHistory(documentId: string): Promise<{ role: string; content: string }[]> {
        return this.request(`/ai/chat/${documentId}/history`);
    }

    async fetchWithAuth(url: string, options: RequestInit = {}, retry = true): Promise<Response> {
        const token = this.getToken();
        const headers: Record<string, string> = {
            ...(options.headers as Record<string, string>),
            ...(token ? { Authorization: `Bearer ${token}` } : {})
        };

        const res = await fetch(url, { ...options, headers });

        if (res.status === 401 && retry) {
            const refreshed = await this.tryRefresh();
            if (refreshed) return this.fetchWithAuth(url, options, false);
            this.redirectToLogin();
            throw new ApiError(401, 'Session expired');
        }

        return res;
    }

    async deleteDocument(id: string): Promise<void> {
        return this.request(`/documents/${id}`, { method: 'DELETE' });
    }

    // Health
    async getHealth(): Promise<any> {
        return this.request('/health');
    }
}

export class ApiError extends Error {
    constructor(
        public status: number,
        message: string
    ) {
        super(message);
        this.name = 'ApiError';
    }
}

export const api = new ApiClient();
