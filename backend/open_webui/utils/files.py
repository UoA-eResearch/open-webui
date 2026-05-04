from open_webui.routers.images import (
    get_image_data,
    upload_image,
)

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    UploadFile,
)
from typing import Optional
from pathlib import Path

from open_webui.storage.provider import Storage

from open_webui.models.chats import Chats
from open_webui.models.files import Files
from open_webui.routers.files import upload_file_handler
from open_webui.retrieval.web.utils import validate_url

import asyncio
import mimetypes
import base64
import io
import re

from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL, ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK
from open_webui.utils.session_pool import get_session

BASE64_IMAGE_URL_PREFIX = re.compile(r'data:image/\w+;base64,', re.IGNORECASE)
MARKDOWN_IMAGE_URL_PATTERN = re.compile(r'!\[(.*?)\]\((.+?)\)', re.IGNORECASE)

# Extension-based MIME fallback, only used when ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK is True.
_IMAGE_MIME_FALLBACK = {
    '.webp': 'image/webp',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml',
    '.bmp': 'image/bmp',
    '.tiff': 'image/tiff',
    '.tif': 'image/tiff',
    '.ico': 'image/x-icon',
    '.heic': 'image/heic',
    '.heif': 'image/heif',
    '.avif': 'image/avif',
}


async def get_image_base64_from_url(url: str) -> Optional[str]:
    try:
        if url.startswith('http'):
            # Validate URL to prevent SSRF attacks against local/private networks
            validate_url(url)
            # Download the image from the URL
            session = await get_session()
            async with session.get(url, ssl=AIOHTTP_CLIENT_SESSION_SSL) as response:
                response.raise_for_status()
                image_data = await response.read()
                encoded_string = base64.b64encode(image_data).decode('utf-8')
                content_type = response.headers.get('Content-Type', 'image/png')
                return f'data:{content_type};base64,{encoded_string}'
        else:
            file = await Files.get_file_by_id(url)

            if not file:
                return None

            file_path = await asyncio.to_thread(Storage.get_file, file.path)
            file_path = Path(file_path)

            if file_path.is_file():
                with open(file_path, 'rb') as image_file:
                    encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                    content_type = mimetypes.guess_type(file_path.name)[0] or (file.meta or {}).get('content_type')
                    if not content_type and ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK:
                        content_type = _IMAGE_MIME_FALLBACK.get(file_path.suffix.lower())
                    if not content_type:
                        return None
                    return f'data:{content_type};base64,{encoded_string}'
            else:
                return None

    except Exception as e:
        return None


async def get_image_url_from_base64(request, base64_image_string, metadata, user):
    if BASE64_IMAGE_URL_PREFIX.match(base64_image_string):
        image_url = ''
        # Extract base64 image data from the line
        image_data, content_type = await get_image_data(base64_image_string)
        if image_data is not None:
            _, image_url = await upload_image(
                request,
                image_data,
                content_type,
                metadata,
                user,
            )

        return image_url
    return None


async def convert_markdown_base64_images(request, content: str, metadata, user):
    MIN_REPLACEMENT_URL_LENGTH = 1024
    result_parts = []
    last_end = 0

    for match in MARKDOWN_IMAGE_URL_PATTERN.finditer(content):
        result_parts.append(content[last_end : match.start()])
        base64_string = match.group(2)
        if len(base64_string) > MIN_REPLACEMENT_URL_LENGTH:
            url = await get_image_url_from_base64(request, base64_string, metadata, user)
            if url:
                result_parts.append(f'![{match.group(1)}]({url})')
            else:
                result_parts.append(match.group(0))
        else:
            result_parts.append(match.group(0))
        last_end = match.end()

    result_parts.append(content[last_end:])
    return ''.join(result_parts)


def load_b64_audio_data(b64_str):
    try:
        if ',' in b64_str:
            header, b64_data = b64_str.split(',', 1)
        else:
            b64_data = b64_str
            header = 'data:audio/wav;base64'
        audio_data = base64.b64decode(b64_data)
        content_type = header.split(';')[0].split(':')[1] if ';' in header else 'audio/wav'
        return audio_data, content_type
    except Exception as e:
        print(f'Error decoding base64 audio data: {e}')
        return None, None


async def upload_audio(request, audio_data, content_type, metadata, user):
    audio_format = mimetypes.guess_extension(content_type)
    file = UploadFile(
        file=io.BytesIO(audio_data),
        filename=f'generated-{audio_format}',  # will be converted to a unique ID on upload_file
        headers={
            'content-type': content_type,
        },
    )
    file_item = await upload_file_handler(
        request,
        file=file,
        metadata=metadata,
        process=False,
        user=user,
    )
    url = request.app.url_path_for('get_file_content_by_id', id=file_item.id)
    return url


async def get_audio_url_from_base64(request, base64_audio_string, metadata, user):
    if 'data:audio/wav;base64' in base64_audio_string:
        audio_url = ''
        # Extract base64 audio data from the line
        audio_data, content_type = load_b64_audio_data(base64_audio_string)
        if audio_data is not None:
            audio_url = await upload_audio(
                request,
                audio_data,
                content_type,
                metadata,
                user,
            )
        return audio_url
    return None


async def get_file_url_from_base64(request, base64_file_string, metadata, user):
    if BASE64_IMAGE_URL_PREFIX.match(base64_file_string):
        return await get_image_url_from_base64(request, base64_file_string, metadata, user)
    elif 'data:audio/wav;base64' in base64_file_string:
        return await get_audio_url_from_base64(request, base64_file_string, metadata, user)
    return None


async def get_image_base64_from_file_id(id: str) -> Optional[str]:
    file = await Files.get_file_by_id(id)
    if not file:
        return None

    try:
        file_path = await asyncio.to_thread(Storage.get_file, file.path)
        file_path = Path(file_path)

        # Check if the file already exists in the cache
        if file_path.is_file():
            with open(file_path, 'rb') as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                content_type = mimetypes.guess_type(file_path.name)[0] or (file.meta or {}).get('content_type')
                if not content_type and ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK:
                    content_type = _IMAGE_MIME_FALLBACK.get(file_path.suffix.lower())
                if not content_type:
                    return None
                return f'data:{content_type};base64,{encoded_string}'
        else:
            return None
    except Exception as e:
        return None


async def get_media_base64_from_file_id(id: str) -> Optional[str]:
    """Get any file as a base64 data URL (used for audio/video direct injection)."""
    file = await Files.get_file_by_id(id)
    if not file:
        return None

    try:
        file_path = await asyncio.to_thread(Storage.get_file, file.path)
        file_path = Path(file_path)

        if file_path.is_file():
            with open(file_path, 'rb') as f:
                encoded_string = base64.b64encode(f.read()).decode('utf-8')
            content_type = (file.meta or {}).get('content_type') or mimetypes.guess_type(file_path.name)[0]
            if not content_type:
                return None
            return f'data:{content_type};base64,{encoded_string}'
        else:
            return None
    except Exception as e:
        return None


async def get_pdf_page_images(file_id: str, dpi: int = 150, max_pages: int = 50) -> list[str]:
    """Render PDF pages as base64 PNG data URLs using pymupdf (fitz).

    Each returned string is a ``data:image/png;base64,...`` data URL suitable
    for use as an ``image_url`` content item in an OpenAI-compatible request.

    Args:
        file_id: The Open WebUI file ID (UUID string).
        dpi: Resolution used for rendering (default 150 DPI).
        max_pages: Maximum number of pages to render.  Defaults to 50.
            Set to 0 to render all pages with no limit.
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        import logging as _log
        _log.getLogger(__name__).warning(
            'pymupdf is not installed; cannot render PDF pages as images. '
            'Install it with: pip install pymupdf'
        )
        return []

    file = await Files.get_file_by_id(file_id)
    if not file:
        return []

    try:
        file_path = await asyncio.to_thread(Storage.get_file, file.path)
        file_path = Path(file_path)

        if not file_path.is_file():
            return []

        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)

        def _render_pages():
            doc = fitz.open(str(file_path))
            try:
                n = len(doc)
                limit = n if (max_pages <= 0) else min(n, max_pages)
                pages = []
                for i in range(limit):
                    pix = doc.load_page(i).get_pixmap(matrix=matrix)
                    img_bytes = pix.tobytes('png')
                    b64 = base64.b64encode(img_bytes).decode('utf-8')
                    pages.append(f'data:image/png;base64,{b64}')
                return pages
            finally:
                doc.close()

        return await asyncio.to_thread(_render_pages)
    except Exception as e:
        import logging as _log
        _log.getLogger(__name__).error(f'Error rendering PDF pages for file {file_id}: {e}')
        return []

