import { IsEnum, IsOptional, IsString, MaxLength, Min, IsInt } from 'class-validator';
import { Exclude, Expose, Type } from 'class-transformer';
import { DocumentStatus } from '@documents/shared';
import { ApiProperty, ApiPropertyOptional } from '@nestjs/swagger';

export class PresignUploadDto {
    @ApiProperty({ description: 'Original file name' })
    @IsString()
    @MaxLength(255)
    fileName!: string;

    @ApiProperty({ description: 'MIME type of the file' })
    @IsString()
    mimeType!: string;

    @ApiProperty({ description: 'File size in bytes' })
    @IsInt()
    @Min(1)
    fileSize!: number;

    @ApiPropertyOptional({ description: 'Optional display name' })
    @IsOptional()
    @IsString()
    @MaxLength(255)
    displayName?: string;
}

export class ListDocumentsDto {
    @ApiPropertyOptional({ enum: DocumentStatus })
    @IsOptional()
    @IsEnum(DocumentStatus)
    status?: DocumentStatus;

    @ApiPropertyOptional({ default: 100, minimum: 1 })
    @IsOptional()
    @Type(() => Number)
    @IsInt()
    @Min(1)
    limit?: number = 100;

    @ApiPropertyOptional({ description: 'Pagination cursor from previous response' })
    @IsOptional()
    @IsString()
    lastKey?: string;
}

@Exclude()
export class DocumentResponseDto {
    @Expose()
    @ApiProperty()
    id!: string;

    @Expose()
    @ApiProperty()
    fileName!: string;

    @Expose()
    @ApiProperty()
    originalName!: string;

    @Expose()
    @ApiProperty()
    mimeType!: string;

    @Expose()
    @ApiProperty()
    fileSize!: number;

    @Expose()
    @ApiProperty({ enum: DocumentStatus })
    status!: DocumentStatus;

    @Expose()
    @ApiPropertyOptional()
    category?: string;

    @Expose()
    @ApiPropertyOptional()
    summary?: string;

    @Expose()
    @ApiPropertyOptional()
    metadata?: Record<string, any>;

    @Expose()
    @ApiPropertyOptional()
    errorMessage?: string;

    @Expose()
    @ApiProperty()
    createdAt!: string;

    @Expose()
    @ApiProperty()
    updatedAt!: string;

    @Expose()
    @ApiPropertyOptional()
    processingCompletedAt?: string;

    @Expose()
    @ApiPropertyOptional({ description: 'Pre-signed URL for downloading the document' })
    downloadUrl?: string;

    @Expose()
    @ApiPropertyOptional({ description: 'Pre-signed URL for the document thumbnail' })
    thumbnailUrl?: string;
}
