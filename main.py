import asyncio
from collections import deque
import logging
import os
import random
import time
from typing import Any, Literal
import filetype
import fitz
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError
from prompt import CONTRACT_PROMPT, INVOICE_PROMPT, RECEIPT_PROMPT, UNIVERSAL_SYSTEM_PROMPT
from schemas import (
    GATE_FIELDS,
    ContractExtraction,
    ExtractionMetadata,
    FinalResponse,
    InvoiceExtraction,
    ReceiptExtraction,
)

load_dotenv()
logger = logging.getLogger("extractor")
app = FastAPI()
api_key = os.getenv("gemini_api_key")
client = genai.Client(api_key=api_key)

FLASH_MODEL = os.getenv("FLASH_MODEL", "gemini-3.8-flash")
PRO_MODEL = os.getenv("PRO_MODEL", "gemini-3.8-pro")

ENABLE_PRO_ESCALATION = os.getenv("ENABLE_PRO_ESCALATION", "false").lower() == "true"

RPM_LIMIT = int(os.getenv("GEMINI_RPM_LIMIT", "12"))
MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "3"))

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024
CONFIDENCE_THRESHOLD = 0.6
UNCERTAIN_RATIO_LIMIT = 0.5  
MIN_TEXT_CHARS = 20
MIN_PDF_TEXT_CHARS = 50


EXTRACTION_REGISTRY = {
    "invoice": {
        "prompt": INVOICE_PROMPT,
        "schema": InvoiceExtraction,
        "required": {"vendor_name", "invoice_number", "total"},
    },
    "contract": {
        "prompt": CONTRACT_PROMPT,
        "schema": ContractExtraction,
        "required": {"parties_involved", "start_date"},
    },
    "receipt": {
        "prompt": RECEIPT_PROMPT,
        "schema": ReceiptExtraction,
        "required": {"merchant", "total"},
    },
}


_request_timestamps: deque[float] = deque()
_rate_limit_lock = asyncio.Lock()


async def _acquire_rate_limit_slot() -> None:
    async with _rate_limit_lock:
        now = time.monotonic()
        while _request_timestamps and now - _request_timestamps[0] >= 60.0:
            _request_timestamps.popleft()

        if len(_request_timestamps) >= RPM_LIMIT:
            sleep_time = 60.0 - (now - _request_timestamps[0]) + 0.1
            logger.info(
                "Rate limit reached (%d RPM). Queuing request for %.2fs",
                RPM_LIMIT,
                sleep_time,
            )
            await asyncio.sleep(sleep_time)
            now = time.monotonic()
            while _request_timestamps and now - _request_timestamps[0] >= 60.0:
                _request_timestamps.popleft()

        _request_timestamps.append(time.monotonic())


async def safe_generate_content(
    model: str,
    contents: list | str,
    config: types.GenerateContentConfig,
):
    for attempt in range(MAX_RETRIES):
        await _acquire_rate_limit_slot()
        try:
            return await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except genai_errors.APIError as e:
            status_code = getattr(e, "code", None)
            if status_code in (429, 503) and attempt < MAX_RETRIES - 1:
                backoff = (2 ** attempt) * 2 + random.uniform(0.5, 1.5)
                logger.warning(
                    "Gemini API %s on %s (attempt %d/%d). Retrying in %.2fs...",
                    status_code,
                    model,
                    attempt + 1,
                    MAX_RETRIES,
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue
            raise


def detect_file_type(data: bytes) -> tuple[str, str]:
    head = data[:2048]
    kind = filetype.guess(head)
    if kind is not None:
        if kind.mime in {"image/jpeg", "image/png"}:
            return "IMAGE", kind.mime
        if kind.mime == "application/pdf":
            return "PDF", kind.mime
        raise HTTPException(status_code=400, detail=f"Type {kind.mime} not allowed")
    if b"\x00" not in head:
        try:
            data.decode("utf-8")
            return "TEXT", "text/plain"
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Text files must be UTF-8 encoded")
    raise HTTPException(status_code=400, detail="Cannot identify file type")


def parse_pdf(data: bytes) -> tuple[str, bool]:
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            text = "".join(page.get_text() for page in doc)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Could not open the PDF. It may be corrupted or encrypted.",
        )
    return text, len(text.strip()) >= MIN_PDF_TEXT_CHARS



async def generate_validated(contents: list, schema: type[BaseModel], model_name: str):
    config = types.GenerateContentConfig(
        system_instruction=UNIVERSAL_SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=0.0,
    )
    response = await safe_generate_content(model=model_name, contents=contents, config=config)
    raw = response.text or ""
    try:
        return schema.model_validate_json(raw)
    except ValidationError as e:
        correction_prompt = (
            "Your previous output failed schema validation. Fix the exact errors listed below "
            "and return corrected JSON only.\n\n"
            f"Validation Errors:\n{e}\n\nYour Broken Output:\n{raw}"
        )
        retry = await safe_generate_content(
            model=model_name,
            contents=contents + [correction_prompt],
            config=config,
        )
        try:
            return schema.model_validate_json(retry.text or "")
        except ValidationError:
            raise HTTPException(status_code=422, detail="Irrecoverable schema validation error on retry.")


def sort_extracted_fields(validated_schema: BaseModel, confidence_threshold: float = CONFIDENCE_THRESHOLD) -> dict:
    buckets: dict[str, list] = {"complete": [], "missing": [], "uncertain": []}

    def process_field(name: str, item: Any):
        if isinstance(item, dict) and "confidence" in item:
            val = item.get("value")
            conf = item.get("confidence", 0.0)
            if val is None:
                buckets["missing"].append(name)
            elif conf < confidence_threshold:
                buckets["uncertain"].append(name)
            else:
                buckets["complete"].append(name)
        elif isinstance(item, dict):
            for sub_name, sub_item in item.items():
                process_field(f"{name}.{sub_name}", sub_item)
        elif isinstance(item, list):
            for index, list_item in enumerate(item):
                process_field(f"{name}[{index}]", list_item)

    for key, value in validated_schema.model_dump().items():
        if key in GATE_FIELDS:
            continue
        process_field(key, value)
    return buckets


def is_poor_quality(result: BaseModel, required: set[str]) -> bool:
    q = sort_extracted_fields(result)
    total = sum(len(v) for v in q.values())
    if total == 0:
        return False
    required_missing = [n for n in q["missing"] if n.split("[")[0].split(".")[0] in required]
    return bool(required_missing) or len(q["uncertain"]) / total > UNCERTAIN_RATIO_LIMIT


def assert_document_ok(result: Any, expected_type: str) -> None:
    if not result.is_readable:
        raise HTTPException(
            status_code=400,
            detail=f"Document unreadable or corrupted: {result.rejection_reason}",
        )
    if result.detected_type != expected_type:
        raise HTTPException(
            status_code=400,
            detail=f"Document mismatch. Expected {expected_type}, detected {result.detected_type}.",
        )


async def extract_document(contents: list, claimed_type: str):
    spec = EXTRACTION_REGISTRY[claimed_type]
    result = await generate_validated(contents, spec["schema"], FLASH_MODEL)
    assert_document_ok(result, claimed_type)
    if not ENABLE_PRO_ESCALATION or not is_poor_quality(result, spec["required"]):
        return result, False
    try:
        pro_result = await generate_validated(contents, spec["schema"], PRO_MODEL)
        return pro_result, True
    except (HTTPException, genai_errors.APIError):
        logger.warning("Pro escalation failed; returning Flash result", exc_info=True)
        return result, False


@app.post("/extract", response_model=FinalResponse)
async def process_doc(
    file: UploadFile = File(...),
    claimed_type: Literal["invoice", "contract", "receipt"] = Form(...),
):
    timing: dict[str, float] = {}
    t0 = time.perf_counter()

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
        )

    try:
        category, mime_type = detect_file_type(data)
        extracted_text = ""
        use_text_pipeline = False
        if category == "PDF":
            extracted_text, use_text_pipeline = await asyncio.to_thread(parse_pdf, data)
        elif category == "TEXT":
            extracted_text, use_text_pipeline = data.decode("utf-8"), True
        timing["file_prep_and_parsing"] = round((time.perf_counter() - t0) * 1000, 2)

        spec = EXTRACTION_REGISTRY[claimed_type]
        if use_text_pipeline:
            if len(extracted_text.strip()) < MIN_TEXT_CHARS:
                raise HTTPException(
                    status_code=400,
                    detail="Document text is empty or too short to process.",
                )
            contents: list = [f"{spec['prompt']}\n\n--- DOCUMENT TEXT ---\n{extracted_text}"]
        else:
            contents = [spec["prompt"], types.Part.from_bytes(data=data, mime_type=mime_type)]

        t2 = time.perf_counter()
        extracted_data, escalated = await extract_document(contents, claimed_type)
        timing["llm_extraction"] = round((time.perf_counter() - t2) * 1000, 2)
        timing["total_processing"] = round((time.perf_counter() - t0) * 1000, 2)

        pipeline_name = f"{'text' if use_text_pipeline else 'vision'}_{'pro_escalated' if escalated else 'flash'}"
        metadata = ExtractionMetadata(
            document_type=claimed_type,
            pipeline_used=pipeline_name,
            file_size_bytes=len(data),
            processing_time_ms=timing,
        )
        return FinalResponse(
            status="success",
            metadata=metadata,
            quality_report=sort_extracted_fields(extracted_data),
            data=extracted_data.model_dump(exclude=GATE_FIELDS),
        )
    except HTTPException:
        raise
    except genai_errors.APIError as e:
        logger.exception("Gemini API error")
        raise HTTPException(
            status_code=502,
            detail=f"Gemini API error ({getattr(e, 'code', 'unknown')}): {e}",
        )
    except Exception as e:
        logger.exception("Unexpected error while processing document")
        raise HTTPException(
            status_code=500,
            detail=f"Internal error ({type(e).__name__}): {e}",
        )