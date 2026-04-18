import {
    Controller,
    Get,
    Post,
    Delete,
    Param,
    Query,
    Body,
    UseGuards,
    ParseUUIDPipe,
    HttpCode,
    HttpStatus
} from '@nestjs/common';
import { ApiTags, ApiOperation, ApiBearerAuth } from '@nestjs/swagger';
import { plainToInstance } from 'class-transformer';
import { JwtAuthGuard } from '../auth/jwt-auth.guard';
import { CurrentUser } from '@common/decorators/current-user.decorator';
import { DocumentsService } from './documents.service';
import { PresignUploadDto, ListDocumentsDto, DocumentResponseDto } from './document.dto';

@ApiTags('documents')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard)
@Controller('documents')
export class DocumentsController {
    constructor(private readonly documentsService: DocumentsService) {}

    @Post('presign')
    @ApiOperation({ summary: 'Get a presigned S3 URL for direct browser upload' })
    async presign(
        @CurrentUser('userId') userId: string,
        @Body() dto: PresignUploadDto
    ): Promise<{ documentId: string; uploadUrl: string; fields: Record<string, string> }> {
        return this.documentsService.presignUpload(
            userId,
            dto.fileName,
            dto.mimeType,
            dto.fileSize,
            dto.displayName
        );
    }

    @Get()
    @ApiOperation({ summary: 'List documents for the current user' })
    async list(@CurrentUser('userId') userId: string, @Query() filters: ListDocumentsDto) {
        const result = await this.documentsService.listDocuments(userId, filters);
        const items = await Promise.all(
            result.items.map(async (item) => {
                const thumbnailUrl = item.thumbnailKey
                    ? await this.documentsService.getThumbnailUrl(userId, item.id)
                    : undefined;
                return plainToInstance(
                    DocumentResponseDto,
                    { ...item, thumbnailUrl },
                    { excludeExtraneousValues: true }
                );
            })
        );
        return { items, total: result.total, lastKey: result.lastKey };
    }

    @Get(':id')
    @ApiOperation({ summary: 'Get a document by ID' })
    async findOne(
        @CurrentUser('userId') userId: string,
        @Param('id', ParseUUIDPipe) id: string
    ): Promise<DocumentResponseDto> {
        const record = await this.documentsService.getDocument(userId, id);
        const thumbnailUrl = record.thumbnailKey
            ? await this.documentsService.getThumbnailUrl(userId, id)
            : undefined;

        return plainToInstance(
            DocumentResponseDto,
            { ...record, thumbnailUrl },
            { excludeExtraneousValues: true }
        );
    }

    @Get(':id/download')
    @ApiOperation({ summary: 'Get a pre-signed download URL' })
    async getDownloadUrl(
        @CurrentUser('userId') userId: string,
        @Param('id', ParseUUIDPipe) id: string
    ) {
        const url = await this.documentsService.getDownloadUrl(userId, id);

        return { downloadUrl: url };
    }

    @Delete(':id')
    @HttpCode(HttpStatus.NO_CONTENT)
    @ApiOperation({ summary: 'Delete a document' })
    async remove(
        @CurrentUser('userId') userId: string,
        @Param('id', ParseUUIDPipe) id: string
    ): Promise<void> {
        await this.documentsService.deleteDocument(userId, id);
    }
}
