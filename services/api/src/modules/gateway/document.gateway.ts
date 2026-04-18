import {
    WebSocketGateway,
    WebSocketServer,
    OnGatewayConnection,
    OnGatewayDisconnect
} from '@nestjs/websockets';
import { Logger } from '@nestjs/common';
import { Server, Socket } from 'socket.io';
import * as jwt from 'jsonwebtoken';
import { JwksClient } from 'jwks-rsa';
import { DocumentStatus } from '@documents/shared';

@WebSocketGateway({
    cors: {
        origin: process.env.CORS_ORIGIN || 'http://localhost:3000',
        credentials: true
    },
    namespace: '/documents'
})
export class DocumentGateway implements OnGatewayConnection, OnGatewayDisconnect {
    @WebSocketServer()
    server!: Server;

    private readonly logger = new Logger(DocumentGateway.name);
    private readonly userSockets = new Map<string, Set<string>>();

    private readonly jwksClient = new JwksClient({
        jwksUri: process.env.AUTH_JWKS_URI!,
        cache: true,
        rateLimit: true,
        jwksRequestsPerMinute: 5
    });

    async handleConnection(client: Socket) {
        const raw = client.handshake.auth?.token ?? client.handshake.query?.token;
        const token = Array.isArray(raw) ? raw[0] : raw;

        const userId = token ? await this.verifyToken(token) : null;

        if (!userId) {
            client.disconnect();
            return;
        }

        client.data.userId = userId;

        if (!this.userSockets.has(userId)) {
            this.userSockets.set(userId, new Set());
        }
        this.userSockets.get(userId)!.add(client.id);
        client.join(`user:${userId}`);

        this.logger.log(`Client ${client.id} connected (user: ${userId})`);
    }

    handleDisconnect(client: Socket) {
        const userId: string | undefined = client.data.userId;
        if (userId) {
            const sockets = this.userSockets.get(userId);

            if (sockets) {
                sockets.delete(client.id);

                if (sockets.size === 0) {
                    this.userSockets.delete(userId);
                }
            }
        }
        this.logger.log(`Client ${client.id} disconnected`);
    }

    notifyStatusChange(userId: string, documentId: string, status: DocumentStatus, document?: any) {
        this.server.to(`user:${userId}`).emit('document:status', {
            documentId,
            status,
            document,
            timestamp: new Date().toISOString()
        });
    }

    private verifyToken(token: string): Promise<string | null> {
        return new Promise((resolve) => {
            jwt.verify(
                token,
                (header, callback) => {
                    this.jwksClient.getSigningKey(header.kid, (err, key) => {
                        if (err || !key) return callback(err ?? new Error('No signing key'));
                        callback(null, key.getPublicKey());
                    });
                },
                {
                    algorithms: ['RS256'],
                    issuer: process.env.AUTH_ISSUER,
                    audience: process.env.AUTH_AUDIENCE
                },
                (err, decoded) => {
                    if (err || !decoded) {
                        if (err) {
                            this.logger.warn(`WS token rejected: ${err.message}`);
                        }

                        return resolve(null);
                    }
                    resolve((decoded as jwt.JwtPayload).sub ?? null);
                }
            );
        });
    }
}
