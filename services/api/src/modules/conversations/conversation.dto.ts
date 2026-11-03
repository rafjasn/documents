import { ApiPropertyOptional, ApiProperty } from '@nestjs/swagger';
import { Type } from 'class-transformer';
import {
    ArrayMaxSize,
    IsArray,
    IsIn,
    IsISO8601,
    IsOptional,
    IsString,
    IsUUID,
    Length,
    MaxLength,
    ValidateNested
} from 'class-validator';

export class ChatScopeDto {
    @ApiPropertyOptional({ description: 'Only these documents; empty means the whole library' })
    @IsOptional()
    @IsArray()
    @ArrayMaxSize(100)
    @IsUUID('all', { each: true })
    documentIds?: string[];

    @ApiPropertyOptional()
    @IsOptional()
    @IsArray()
    @IsIn(['INVOICE', 'RECEIPT', 'CONTRACT', 'OTHER'], { each: true })
    categories?: ('INVOICE' | 'RECEIPT' | 'CONTRACT' | 'OTHER')[];

    @ApiPropertyOptional({ description: 'Document languages, e.g. pl, en' })
    @IsOptional()
    @IsArray()
    @Length(2, 2, { each: true })
    languages?: string[];

    @ApiPropertyOptional()
    @IsOptional()
    @IsISO8601({ strict: true })
    dateFrom?: string;

    @ApiPropertyOptional()
    @IsOptional()
    @IsISO8601({ strict: true })
    dateTo?: string;
}

export class CreateConversationDto {
    @ApiPropertyOptional()
    @IsOptional()
    @IsString()
    @MaxLength(200)
    title?: string;

    @ApiPropertyOptional({ type: ChatScopeDto })
    @IsOptional()
    @ValidateNested()
    @Type(() => ChatScopeDto)
    scope?: ChatScopeDto;
}

export class ListConversationsDto {
    @ApiPropertyOptional({ description: 'Only conversations scoped to this one document' })
    @IsOptional()
    @IsUUID()
    documentId?: string;
}

export class StreamMessageDto {
    @ApiProperty()
    @IsString()
    @Length(1, 4000)
    message!: string;
}
