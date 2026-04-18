import { NestFactory } from '@nestjs/core';
import { CloudWatchLogger } from '@documents/shared';
import { WorkerModule } from './worker.module';

async function bootstrap() {
    const logger = new CloudWatchLogger('/documents/worker');
    logger.setupProcessHandlers();

    const app = await NestFactory.create(WorkerModule, { logger });
    const port = parseInt(process.env.PORT || '3002', 10);

    app.enableShutdownHooks();
    await app.listen(port);
    logger.log(`Worker service ready on :${port}`, 'Worker');
}

bootstrap();
