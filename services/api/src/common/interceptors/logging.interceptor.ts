import { Injectable, NestInterceptor, ExecutionContext, CallHandler, Logger } from '@nestjs/common';
import { Observable, tap } from 'rxjs';

@Injectable()
export class LoggingInterceptor implements NestInterceptor {
    private readonly logger = new Logger('HTTP');

    intercept(context: ExecutionContext, next: CallHandler): Observable<any> {
        const request = context.switchToHttp().getRequest();
        const { method, url } = request;
        const now = Date.now();

        return next.handle().pipe(
            tap({
                next: () => {
                    const response = context.switchToHttp().getResponse();
                    const duration = Date.now() - now;
                    this.logger.log(`${method} ${url} ${response.statusCode} - ${duration}ms`);
                },
                error: (error) => {
                    const duration = Date.now() - now;
                    this.logger.error(
                        `${method} ${url} ${error.status || 500} - ${duration}ms - ${error.message}`
                    );
                }
            })
        );
    }
}
