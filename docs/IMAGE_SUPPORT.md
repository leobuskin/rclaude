# Image Support for Telegram to Claude

## Overview

rclaude now supports sending images from Telegram to Claude for vision analysis. Users can send photos with or without captions, and Claude will automatically analyze them.

## Features

### ✅ Image Analysis
- **Auto-analyze photos**: Send any photo and Claude automatically analyzes it
- **Image + text**: Combine images with captions or questions (like Claude Code)
- **Multiple formats**: JPEG, PNG, GIF, WebP, BMP, TIFF
- **Automatic cleanup**: Temp files are deleted after analysis

### How It Works

1. **User sends photo** (with optional caption)
   ```
   [Photo] + "What's in this image?" (optional)
   ```

2. **Image is downloaded** from Telegram servers
   ```
   /tmp/rclaude-images/{user_id}/{timestamp}.jpg
   ```

3. **Image is encoded** to base64
   ```
   Data URI format ready for Claude API
   ```

4. **Message sent to Claude** with mixed content
   ```python
   [
     {"type": "image", "source": {"type": "base64", ...}},
     {"type": "text", "text": "caption"}
   ]
   ```

5. **Claude analyzes** and responds
   ```
   "I see a [description]..."
   ```

6. **Temp file cleaned up** after analysis

## Architecture

### New Files

**`rclaude/image_handler.py`** - Image handling utilities
- `download_telegram_photo()` - Download from Telegram servers
- `prepare_image_for_claude()` - Encode to base64
- `cleanup_image_file()` - Delete temp files
- `get_image_mime_type()` - Detect MIME type

### Modified Files

**`rclaude/server.py`**
- Updated `tg_handle_message()` to detect photos
- Added `_handle_message_with_image()` for vision workflow
- Updated message handler filter to accept `filters.PHOTO`

**`rclaude/formatting.py`**
- Added `format_image_analysis_summary()` for display

## Usage Examples

### Simple Photo
User sends a photo without caption:
```
[Photo]
```
→ Bot: "📸 Image received, analyzing..."
→ Claude automatically describes the image

### Photo with Question
User sends a photo with caption:
```
[Photo]
"What's the sentiment of this meme?"
```
→ Bot: "📸 Image received, analyzing..."
→ Claude analyzes and answers the question

### Photo with Instructions
```
[Photo]
"Identify all objects in this image and classify by category"
```
→ Claude performs detailed analysis

## Technical Details

### Image Storage
- **Location**: `/tmp/rclaude-images/{user_id}/`
- **Naming**: `{unix_timestamp_ms}.jpg`
- **Cleanup**: Immediate after analysis, or after 1 hour if failed
- **Size limit**: Telegram enforces max ~20MB

### Supported Formats
| Format | MIME Type | Extension |
|--------|-----------|-----------|
| JPEG   | image/jpeg | .jpg, .jpeg |
| PNG    | image/png  | .png |
| GIF    | image/gif  | .gif |
| WebP   | image/webp | .webp |
| BMP    | image/bmp  | .bmp |
| TIFF   | image/tiff | .tiff |

### Base64 Encoding
- Photos are downloaded to temp directory
- Read as binary and encoded to base64 string
- Sent to Claude API as data URI format
- Original file deleted after encoding

### Error Handling
| Scenario | Behavior |
|----------|----------|
| Download fails | "❌ Failed to download image" |
| Encoding fails | "❌ Failed to process image" |
| Claude API error | Error message passed through |
| Network timeout | Graceful error with retry prompt |

## Integration with Claude Code

This mirrors Claude Code's image support:
- **Same behavior**: Auto-analyze on send
- **Same format**: Image + optional text
- **Same experience**: Seamless vision analysis

## Performance Considerations

### Memory
- Images downloaded to disk (not held in memory)
- Base64 encoding uses streaming where possible
- Temp cleanup prevents disk space issues

### Speed
- Download: ~1-3 seconds (network dependent)
- Encoding: <500ms for typical images
- Analysis: Depends on Claude's model response time

### Limits
- **Max file size**: ~20MB (Telegram limit)
- **Max base64 size**: ~26MB (~20MB × 4/3)
- **Concurrent images**: No limit (one per message)

## Future Enhancements

Potential improvements:
- [ ] Multiple images per message (album support)
- [ ] Video frame extraction
- [ ] OCR results integration
- [ ] Image caching for repeated analysis
- [ ] Batch image analysis
- [ ] Image sharing to terminal session

## Troubleshooting

### Image not analyzed
1. Check that image is attached (not just caption)
2. Verify Telegram bot has file permissions
3. Check disk space in `/tmp/`
4. Review logs for error messages

### Analysis is wrong
- Claude may need more context - add detailed caption
- Try different models via `/model` command
- Provide specific instructions in caption

### Slow responses
- Check network connectivity
- Try smaller/simpler images
- Consider switching to faster model (haiku)

## Implementation Notes

### Why Download Locally?
- **Requirements**: User requested local file handling
- **Benefits**:
  - File paths can be logged/debugged
  - Supports future file analysis tools
  - Allows temp file management
  - Matches Claude Code behavior

### Why Base64?
- **Alternative**: Telegram file URLs + File API
- **Chosen**: Simpler, no auth needed, stateless
- **Trade-off**: Slightly larger payload, but compatible

### Code Safety
- Type hints throughout
- Error handling with cleanup
- Logging at each stage
- No secrets in image handling
- Temp files cleaned immediately

## Logging

Image operations are logged with `[IMAGE]` prefix:
```
[IMAGE] Downloading photo from Telegram...
[IMAGE] Downloaded photo: /tmp/rclaude-images/123/1704067200000.jpg (45234 bytes)
[IMAGE] Encoded image to base64 (60312 chars)
[IMAGE] Image prepared for Claude: image/jpeg
[IMAGE] Sending to Claude...
[IMAGE] Cleaned up image: /tmp/rclaude-images/123/1704067200000.jpg
```

## Testing

### Manual Test Checklist
- [ ] Send photo without caption
- [ ] Send photo with caption
- [ ] Send photo with complex question
- [ ] Verify Claude responds with analysis
- [ ] Check temp files are cleaned up
- [ ] Test with different image formats
- [ ] Test with both fast (haiku) and capable (opus) models
- [ ] Verify error handling (send corrupt image, etc.)

### Automated Tests
Create tests for:
- Image download and encoding
- MIME type detection
- Cleanup functionality
- Error scenarios

## Related Commands

The following existing commands work with image analysis:
- `/model` - Switch Claude model for analysis
- `/mode` - Change permission settings
- `/cost` - View token usage from image analysis
- `/stop` - Interrupt long-running analysis
- `/cc` - Return to terminal with analysis in history

## See Also
- [Claude Code Image Support](https://claude.ai/docs/features/vision)
- [rclaude Architecture](./ARCHITECTURE.md)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Claude API Vision](https://platform.claude.com/docs/vision)
